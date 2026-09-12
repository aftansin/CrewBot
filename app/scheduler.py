"""Периодический опрос календарей.

Отличие от старой версии: в задачу передаётся только ``pilot_id``.
Раньше в job клался объект сессии из мидлвари, который к моменту запуска
был давно закрыт — отсюда пять коммитов "remove polling" подряд.
Сессию открывает сам SyncService, на каждый проход свою.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo
from app.sync.service import SyncService

logger = logging.getLogger(__name__)


class PollManager:
    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        sync_service: SyncService,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._scheduler = scheduler
        self._sync = sync_service
        self._session_factory = session_factory
        self._settings = settings

    @staticmethod
    def job_id(pilot_id: int) -> str:
        return f"poll:{pilot_id}"

    def schedule(self, pilot_id: int, interval_minutes: int) -> None:
        interval = max(
            self._settings.poll_interval_min_minutes,
            min(interval_minutes, self._settings.poll_interval_max_minutes),
        )
        self._scheduler.add_job(
            self._run,
            trigger=IntervalTrigger(minutes=interval),
            kwargs={"pilot_id": pilot_id},
            id=self.job_id(pilot_id),
            replace_existing=True,
            # Разводим пилотов во времени, чтобы не долбить провайдера залпом.
            jitter=self._settings.poll_jitter_seconds,
            coalesce=True,          # накопившиеся пропуски схлопываются в один
            max_instances=1,
            misfire_grace_time=600,
        )
        logger.info("Опрос пилота %s: раз в %s мин", pilot_id, interval)

    def unschedule(self, pilot_id: int) -> None:
        job = self._scheduler.get_job(self.job_id(pilot_id))
        if job is not None:
            job.remove()
            logger.info("Опрос пилота %s остановлен", pilot_id)

    async def restore_all(self) -> None:
        """Поднимает задачи для всех привязанных пилотов при старте."""
        async with self._session_factory() as session:
            pilots = await repo.list_pilots(session, only_linked=True)
            for pilot in pilots:
                self.schedule(pilot.id, pilot.poll_interval_minutes)
        logger.info("Восстановлено задач опроса: %s", len(pilots))

    async def _run(self, pilot_id: int) -> None:
        try:
            result = await self._sync.sync_pilot(pilot_id, notify=True)
            if not result.ok:
                logger.warning(
                    "Плановый опрос пилота %s: %s (%s)",
                    pilot_id,
                    result.status.value,
                    result.error,
                )
        except Exception:  # noqa: BLE001
            # Исключение из job не должно убивать планировщик.
            logger.exception("Плановый опрос пилота %s упал", pilot_id)
