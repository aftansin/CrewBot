# Следующая мидлварь обязательно должна быть зарегистрирована после DatabaseMiddleware
from typing import Callable, Awaitable, Dict, Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from db.db_requests import insert_pilot, get_db_pilot


class TrackAllUsersMiddleware(BaseMiddleware):

    async def __call__(
            self,
            handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: Dict[str, Any],
    ) -> Any:
        bot = data.get('bot')
        session = data.get('session')
        admin_id = data.get('admin_id')
        chat_id = int(data.get('event_from_user').id)
        pilot = await get_db_pilot(session, chat_id)

        # Если нет в бд пользователя, то отправим админу сообщение
        if not pilot:
            admin_msg = (f'<b>New pilot</b> @{data.get("event_from_user").username}\n'
                         f'/start to check user data')
            await bot.send_message(admin_id, admin_msg)
            user_name = data.get("event_from_user").username or 'Stranger'
            user_msg = f'Welcome, {user_name}!\nProvide ics link to load data.'
            await bot.send_message(chat_id, user_msg)

            # И добавим данные пользователя в бд
            await insert_pilot(
                session=session,
                telegram_id=data.get("event_from_user").id,
                username=data.get("event_from_user").username,
                first_name=data.get("event_from_user").first_name,
                last_name=data.get("event_from_user").last_name,
            )

        # И добавим ОБЪЕКТ юзера в Middleware Data для дальнейшего использования
        data['db_pilot'] = pilot
        return await handler(event, data)
