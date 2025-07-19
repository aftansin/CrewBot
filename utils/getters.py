from aiogram.enums import ContentType
from aiogram_dialog import DialogManager
from aiogram_dialog.api.entities import MediaAttachment

from db.db_requests import get_pilot_events


async def pilot_data_getter(dialog_manager: DialogManager, **middleware_data):
    db_pilot = middleware_data.get('db_pilot')
    return {'username': db_pilot.username,
            'last_name': db_pilot.last_name,
            'first_name': db_pilot.first_name,
            'middle_name': db_pilot.middle_name,
            'registration_date': db_pilot.registration_date.date(),
            'subscription_date': None if not db_pilot.subscription_date else db_pilot.subscription_date.date(),
            'ics_url': db_pilot.ics_url}


async def qr_code_getter(**kwargs):
    img = MediaAttachment(type=ContentType.PHOTO, path='db/QR_Code.jpg')
    return {'qr_code': img}


async def user_events_getter(dialog_manager: DialogManager, **middleware_data):
    pilot_id = middleware_data.get('db_pilot').id
    return {'events': await get_pilot_events(middleware_data.get('session'), pilot_id)}
