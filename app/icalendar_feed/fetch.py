"""Загрузка ленты подписки по HTTP.

Запросы к календарю могут идти через прокси — например, через московские
ноды, чтобы обращение к провайдеру шло с российского адреса, а не со
шведского сервера, где живёт бот. Прокси выбирается случайно из списка;
если список пуст, запрос идёт напрямую.

Поддерживаются HTTP- и SOCKS5-прокси (socks5:// требует пакета
aiohttp-socks). Прочие протоколы (VLESS/Reality и подобные) aiohttp
напрямую не умеет — на ноде под них поднимается отдельный SOCKS5- или
HTTP-порт, и его адрес кладётся в CALENDAR_PROXIES.

Условные запросы: бот запоминает Last-Modified/ETag ленты и в следующий
раз спрашивает «изменилось ли». Если нет — провайдер отвечает коротким
304 без тела. Это стандартное вежливое поведение HTTP-клиента: меньше
трафика и меньше нагрузки на провайдера.
"""

from __future__ import annotations

import asyncio
import logging
import random

import aiohttp

logger = logging.getLogger(__name__)


class FeedError(Exception):
    """Лента недоступна или ответ не похож на календарь."""


class _NotModified(Exception):
    """Провайдер ответил 304: лента не изменилась."""


class FeedFetcher:
    """Держит один ClientSession на всё приложение.

    В старой версии сессия создавалась на каждый запрос и таймаута не было
    вовсе — одно зависшее соединение подвешивало задачу навсегда.
    """

    def __init__(
        self,
        timeout_seconds: int = 30,
        retries: int = 3,
        proxies: list[str] | None = None,
        user_agent: str = "CrewApp/2.0 (+calendar subscription client)",
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retries = max(1, retries)
        # Список прокси. Пустой — ходим напрямую.
        self._proxies = [p.strip() for p in (proxies or []) if p.strip()]
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        # Кеш условных запросов: url -> {"etag": ..., "last_modified": ...}.
        self._validators: dict[str, dict[str, str]] = {}
        self._warned_socks = False

    @property
    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "text/calendar, text/plain",
            "Accept-Language": "ru-RU",
            "User-Agent": self._user_agent,
        }

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, headers=self._base_headers
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _proxy_order(self) -> list[str | None]:
        """Порядок обхода прокси на один запрос.

        Список перемешивается — пока живы оба узла, первым оказывается
        то один, то другой (то самое чередование). При этом фетчер идёт
        по порядку и на первом ответившем останавливается, поэтому мёртвый
        узел не тормозит: не ответил — сразу пробуется следующий.

        Пустой список означает прямой запрос без прокси (одна попытка).
        """
        if not self._proxies:
            return [None]
        order = list(self._proxies)
        random.shuffle(order)
        return order

    def _conditional_headers(self, url: str) -> dict[str, str]:
        cached = self._validators.get(url)
        if not cached:
            return {}
        headers: dict[str, str] = {}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
        return headers

    def _remember_validators(self, url: str, response: aiohttp.ClientResponse) -> None:
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if etag or last_modified:
            self._validators[url] = {
                "etag": etag or "",
                "last_modified": last_modified or "",
            }

    async def fetch(self, url: str, allow_not_modified: bool = False) -> str | None:
        """Загружает ленту.

        Возвращает текст календаря, либо None, если провайдер ответил 304
        Not Modified и allow_not_modified=True (значит, ничего не изменилось
        с прошлого раза — синхронизацию можно пропустить).
        """
        if self._session is None or self._session.closed:
            await self.start()

        request_headers = self._conditional_headers(url) if allow_not_modified else {}
        last_error: Exception | None = None

        for attempt in range(1, self._retries + 1):
            # На каждую попытку — свежий перемешанный порядок узлов.
            # Внутри попытки обходим их подряд: первый рабочий побеждает,
            # мёртвый узел не съедает всю попытку.
            for proxy in self._proxy_order():
                try:
                    return await self._attempt(
                        url, proxy, request_headers, allow_not_modified
                    )
                except _NotModified:
                    logger.debug("Лента не изменилась (304): %s", _mask(url))
                    return None
                except FeedError as exc:
                    if "HTTP 4" in str(exc):
                        raise
                    last_error = exc
                    logger.warning(
                        "Узел %s: %s", _mask_proxy(proxy) or "напрямую", exc
                    )
                except (TimeoutError, aiohttp.ClientError) as exc:
                    last_error = exc
                    logger.warning(
                        "Узел %s недоступен: %s",
                        _mask_proxy(proxy) or "напрямую", exc,
                    )
                # этот узел не ответил — пробуем следующий в порядке

            # все узлы в этой попытке отпали — ждём и пробуем заново
            if attempt < self._retries:
                await asyncio.sleep(2**attempt)

        raise FeedError(f"Лента недоступна после {self._retries} попыток: {last_error}")

    async def _attempt(
        self, url: str, proxy: str | None, extra_headers: dict[str, str], allow_304: bool
    ) -> str:
        assert self._session is not None

        # SOCKS-прокси требует собственного коннектора и своей сессии;
        # HTTP-прокси передаётся аргументом в обычную сессию.
        socks_connector = None
        proxy_arg: str | None = None
        if proxy:
            if proxy.startswith("socks"):
                socks_connector = self._socks_connector(proxy)
            else:
                proxy_arg = proxy

        kwargs: dict = {}
        if extra_headers:
            kwargs["headers"] = extra_headers
        if proxy_arg:
            kwargs["proxy"] = proxy_arg

        session = self._session
        temp_session: aiohttp.ClientSession | None = None
        if socks_connector is not None:
            temp_session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers=self._base_headers,
                connector=socks_connector,
            )
            session = temp_session

        try:
            async with session.get(url, **kwargs) as response:
                if response.status == 304 and allow_304:
                    raise _NotModified
                if response.status != 200:
                    raise FeedError(f"HTTP {response.status}")
                text = await response.text()
                self._remember_validators(url, response)
        finally:
            if temp_session is not None:
                await temp_session.close()

        if "BEGIN:VCALENDAR" not in text:
            raise FeedError("Ответ не является календарём iCalendar")
        return text

    def _socks_connector(self, proxy: str):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError as exc:  # pragma: no cover
            if not self._warned_socks:
                logger.error(
                    "Для SOCKS-прокси нужен пакет aiohttp-socks. "
                    "Запрос пойдёт напрямую."
                )
                self._warned_socks = True
            raise FeedError("aiohttp-socks не установлен") from exc
        return ProxyConnector.from_url(proxy)


def _mask(url: str) -> str:
    """Прячет UUID подписки в логах."""
    parts = url.rsplit("/", 1)
    if len(parts) == 2 and len(parts[1]) > 8:
        return f"{parts[0]}/{parts[1][:4]}\u2026{parts[1][-4:]}"
    return url


def _mask_proxy(proxy: str | None) -> str:
    if not proxy:
        return ""
    if "@" in proxy:
        scheme, _, tail = proxy.partition("://")
        return f"{scheme}://\u2026@{tail.rsplit('@', 1)[-1]}"
    return proxy
