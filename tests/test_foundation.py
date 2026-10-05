import asyncio
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Message, Update
from django.conf import settings
from django.test import Client, TransactionTestCase
from django.utils import timezone

from bot.__main__ import main, run
from bot.handlers import START_TEXT, create_router
from config.environment import BASE_DIR, env_bool, load_environment
from config.logging import SafeFormatter, log_failure

TOKEN = "123456789:" + "a" * 35


class FoundationTests(unittest.TestCase):
    def test_admin_login_and_protection(self):
        client = Client()
        response = client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContainsLogin(response.content)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def assertContainsLogin(self, content):
        self.assertIn(b'name="username"', content)
        self.assertIn(b'name="password"', content)

    def test_timezone(self):
        self.assertEqual(settings.TIME_ZONE, "Europe/Moscow")
        self.assertTrue(timezone.is_aware(timezone.now()))

    def test_environment_priority_and_literal_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("EXAMPLE=from-file\nSECOND=${EXAMPLE}\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXAMPLE": "from-process"}, clear=True):
                load_environment(path)
                self.assertEqual(os.environ["EXAMPLE"], "from-process")
                self.assertEqual(os.environ["SECOND"], "${EXAMPLE}")

    def test_invalid_boolean_does_not_echo_value(self):
        with patch.dict(os.environ, {"DJANGO_DEBUG": "private-value"}):
            with self.assertRaisesRegex(ValueError, "DJANGO_DEBUG: ожидается true или false"):
                env_bool("DJANGO_DEBUG")

    def test_logs_redact_secrets_and_traceback(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(SafeFormatter("%(message)s"))
        log = logging.Logger("test")
        log.addHandler(handler)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "DJANGO_SECRET_KEY": "private-secret"}):
            try:
                raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getMe private-secret")
            except RuntimeError:
                log.exception("Failure %s", TOKEN)
        output = stream.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("private-secret", output)
        self.assertNotIn("api.telegram.org", output)

    def test_missing_token_cli(self):
        result = subprocess.run([sys.executable, "-m", "bot"], cwd=BASE_DIR,
                                env={**os.environ, "TELEGRAM_BOT_TOKEN": "", "PYTHONIOENCODING": "utf-8"},
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Не задан TELEGRAM_BOT_TOKEN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_failure_diagnostics_include_location_but_no_exception_data(self):
        secret = "private-invitation-payload"
        with self.assertLogs("review.diagnostics", level="ERROR") as logs:
            try:
                raise RuntimeError(secret)
            except RuntimeError as error:
                log_failure(logging.getLogger("review.diagnostics"), "Failure", error)
        output = "\n".join(logs.output)
        self.assertIn("RuntimeError", output)
        self.assertIn("test_foundation.py:", output)
        self.assertNotIn(secret, output)

    def test_configuration_failure_has_specific_diagnostics(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}), \
             patch.object(sys, "argv", ["bot"]), \
             patch("bot.__main__.load_environment"), \
             patch("bot.__main__.logging.config.dictConfig"), \
             patch("django.setup", side_effect=RuntimeError("private-config")), \
             self.assertLogs("bot", level="ERROR") as logs:
            self.assertEqual(main(), 1)
        output = "\n".join(logs.output)
        self.assertIn("Ошибка настройки Django", output)
        self.assertNotIn("private-config", output)
        self.assertNotIn("сеть", output)

    def test_runtime_failures_have_specific_diagnostics(self):
        from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
        from aiogram.methods import GetMe
        from aiogram.utils.token import TokenValidationError

        cases = [
            (TokenValidationError("private-data"), "Проверьте TELEGRAM_BOT_TOKEN"),
            (TelegramUnauthorizedError(method=GetMe(), message="private-data"), "Проверьте TELEGRAM_BOT_TOKEN"),
            (TelegramNetworkError(method=GetMe(), message="private-data"), "Проверьте сеть"),
            (RuntimeError("private-data"), "Проверьте указанное место в коде"),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), \
                 patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}), \
                 patch.object(sys, "argv", ["bot"]), \
                 patch("bot.__main__.load_environment"), \
                 patch("bot.__main__.logging.config.dictConfig"), \
                 patch("django.setup"), \
                 patch("bot.__main__.run", new_callable=AsyncMock, side_effect=error), \
                 self.assertLogs("bot", level="ERROR") as logs:
                self.assertEqual(main(), 1)
            output = "\n".join(logs.output)
            self.assertIn(expected, output)
            self.assertNotIn("private-data", output)


class StartRoutingTests(TransactionTestCase):
    async def test_start_without_invitation_routed_offline(self):
        bot = Bot(TOKEN)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        update = Update.model_validate({
            "update_id": 1,
            "message": {"message_id": 1, "date": 0,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]},
        })
        with patch.object(bot.session, "make_request", new_callable=AsyncMock, return_value=Message.model_validate({"message_id": 2, "date": 0, "chat": {"id": 42, "type": "private"}})) as request:
            await dispatcher.feed_update(bot, update)
        request.assert_awaited_once()
        method = request.call_args.args[1]
        self.assertIsInstance(method, SendMessage)
        self.assertEqual(method.text, START_TEXT)
        self.assertEqual(method.chat_id, 42)
        await bot.session.close()


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_closed_on_success_failure_and_cancel(self):
        for failure in (None, RuntimeError("failure"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                session = MagicMock()
                session.__aenter__ = AsyncMock(return_value=session)
                session.__aexit__ = AsyncMock(return_value=False)
                dispatcher = MagicMock()
                dispatcher.start_polling = AsyncMock(side_effect=failure)
                with patch("bot.__main__.AiohttpSession", return_value=session), \
                     patch("bot.__main__.Bot"), \
                     patch("bot.__main__.Dispatcher", return_value=dispatcher):
                    if failure is None:
                        await run(TOKEN)
                    else:
                        with self.assertRaises(type(failure)):
                            await run(TOKEN)
                session.__aexit__.assert_awaited_once()

    async def test_getme_only(self):
        with patch("bot.__main__.Bot") as bot_class, patch("bot.__main__.Dispatcher") as dispatcher:
            bot_class.return_value.get_me = AsyncMock()
            await run(TOKEN, check=True)
            bot_class.return_value.get_me.assert_awaited_once()
            dispatcher.assert_not_called()
