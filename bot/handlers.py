from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message, Update
from asgiref.sync import sync_to_async
from django.utils import timezone

from bot.texts import REPLIES, START_TEXT
from modules.delivery.application import process_event
from modules.delivery.outbox import drain


async def _handle(message, kind, *, event_update, payload=None):
    user = message.from_user
    event = dict(bot_id=message.bot.id, update_id=event_update.update_id,
                 chat_id=message.chat.id, chat_type=message.chat.type,
                 message_id=message.message_id, kind=kind, payload=payload,
                 text=message.text,
                 reply_id=message.reply_to_message.message_id if message.reply_to_message else None,
                 user=user.model_dump(include={'id', 'first_name', 'last_name', 'username', 'is_bot'}) if user else None)
    await sync_to_async(process_event, thread_sensitive=True)(event, now=timezone.now())
    await drain(message.bot, chat_id=message.chat.id)


async def start(message: Message, command: CommandObject, event_update: Update):
    await _handle(message, 'start', event_update=event_update, payload=command.args)


async def review(message: Message, event_update: Update):
    await _handle(message, 'review', event_update=event_update)


async def answer(message: Message, event_update: Update):
    await _handle(message, 'answer', event_update=event_update)


async def unsupported(message: Message, event_update: Update):
    await _handle(message, 'unsupported', event_update=event_update)


def create_router():
    router = Router(name='study')
    router.message.register(start, CommandStart())
    router.message.register(review, Command('review'))
    router.message.register(unsupported, F.text.lstrip().startswith('/'))
    router.message.register(answer, F.text)
    router.message.register(unsupported)
    return router
