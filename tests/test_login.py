import asyncio
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import main
import quark
import telegram


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_listens_before_display_and_refreshes_expired_qr(self):
        order = []
        async def expired(timeout):
            order.append("listen-first")
            raise asyncio.TimeoutError()
        async def scanned(timeout):
            order.append("listen-second")
        first = SimpleNamespace(url="tg://login?token=first", wait=expired)
        second = SimpleNamespace(url="tg://login?token=second", wait=scanned)
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                          qr_login=AsyncMock(side_effect=[first, second]))
        uploader.call = telegram.FloodGate(uploader.client)
        def display(service, url):
            order.append("display-first" if "first" in url else "display-second")
        with patch.object(telegram, "show_qr", side_effect=display), patch.object(telegram, "login_status") as status:
            await uploader.login()
        self.assertEqual(order, ["listen-first", "display-first", "listen-second", "display-second"])
        self.assertEqual(status.call_args.args, ("telegram", "ready"))

    async def test_login_instruction_only_touches_selected_platform(self):
        tg = SimpleNamespace(login=AsyncMock(), close=AsyncMock())
        with patch.object(telegram, "Telegram", return_value=tg), patch.object(quark, "Quark") as q, \
                redirect_stdout(StringIO()):
            code = await main.operate({}, login="telegram", force=True)
        self.assertEqual(code, 0)
        tg.login.assert_awaited_once_with(force=True)
        q.assert_not_called()

    async def test_cancellation_waits_for_active_sync_thread(self):
        started, release = threading.Event(), threading.Event()
        def work():
            started.set()
            release.wait(2)
            return "finished"
        with redirect_stdout(StringIO()):
            task = asyncio.create_task(main.settle(asyncio.to_thread(work)))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await task, ["finished"])


class QuarkLoginTests(unittest.TestCase):
    def test_forced_login_uses_official_oauth_not_private_qr(self):
        client = quark.Quark()
        client.cli = MagicMock()
        client.cli.run.return_value = (0, [{"code": 0, "msg": "成功", "action": "login",
                                            "type": "result", "data": {}}], "")
        client.login(force=True, prompt=lambda message: "")
        self.assertEqual([call.args[0][0] for call in client.cli.run.call_args_list], ["login"])

    def test_authorization_check_is_read_only(self):
        client = quark.Quark()
        client.cli = MagicMock()
        client.cli.run.return_value = (0, [{"code": 0, "msg": "成功", "action": "get-user-info",
                                            "type": "result", "data": {}}], "")
        self.assertTrue(client.authorized())
        self.assertEqual(client.cli.run.call_args.args[0], ["get-user-info"])


if __name__ == "__main__":
    unittest.main()
