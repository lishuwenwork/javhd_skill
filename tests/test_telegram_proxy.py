"""Telegram 异步代理依赖回归；只使用本地代理，不登录或上传到 Telegram。"""

import asyncio
from collections import defaultdict
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
import logging
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from telethon import TelegramClient
from telethon.network.connection import ConnectionTcpFull
from telethon.sessions import MemorySession

import common
import main
import quark
import telegram

HAS_PYTHON_SOCKS = importlib.util.find_spec("python_socks") is not None


def proxy_url(scheme="socks5"):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"{scheme}://127.0.0.1:{listener.getsockname()[1]}"


class ProxyDependencyTests(unittest.TestCase):
    def backend(self):
        return SimpleNamespace(Proxy=SimpleNamespace(create=MagicMock()))

    def config(self, url):
        return {"telegram_api_id": secrets.randbelow(1000000) + 1,
                "telegram_api_hash": secrets.token_hex(16), "telegram_proxy": url,
                "telegram_channel_id": 0, "http_proxy": "", "javhd_cookies_file": "",
                "upload_concurrency": 6, "notify_saved_messages": False}

    def test_direct_mode_does_not_require_optional_proxy_backend(self):
        with patch.object(telegram, "import_module", side_effect=ImportError):
            status = telegram.check_telegram_proxy("")
        self.assertEqual(status, {"telegram_proxy_enabled": False, "telegram_proxy_ready": True,
                                  "telegram_proxy_backend": "direct", "telegram_proxy_type": ""})

    def test_missing_async_backend_stops_before_telethon_client_creation(self):
        uploader = telegram.Telegram.__new__(telegram.Telegram)
        uploader.config = self.config(proxy_url())
        with patch.object(telegram, "import_module", side_effect=ImportError("missing")), \
                patch.object(telegram, "TelegramClient") as client, warnings.catch_warnings(record=True) as messages:
            with self.assertRaises(common.JobError) as caught:
                uploader._make_client()
        self.assertIn("python-socks[asyncio]", str(caught.exception))
        self.assertIn("不会回退直连", str(caught.exception))
        client.assert_not_called()
        self.assertEqual(messages, [])

    def test_installed_module_but_stale_telethon_cache_requires_restart(self):
        for module in (telegram.telegrambaseclient, telegram.telethon_connection):
            with self.subTest(module=module.__name__), \
                    patch.object(telegram, "import_module", return_value=self.backend()), \
                    patch.object(telegram.telegrambaseclient, "python_socks", object()), \
                    patch.object(telegram.telethon_connection, "python_socks", object()), \
                    patch.object(module, "python_socks", None):
                with self.assertRaisesRegex(common.JobError, "重启 Python 进程"):
                    telegram.check_telegram_proxy(proxy_url())

    def test_enabled_proxy_is_passed_to_client_and_check_exposes_no_endpoint(self):
        for scheme in ("socks5", "socks4", "http"):
            url = proxy_url(scheme)
            with self.subTest(scheme=scheme), \
                    patch.object(telegram, "import_module", return_value=self.backend()), \
                    patch.object(telegram.telegrambaseclient, "python_socks", object()), \
                    patch.object(telegram.telethon_connection, "python_socks", object()), \
                    patch.object(telegram, "TelegramClient") as client:
                status = telegram.check_telegram_proxy(url)
                uploader = telegram.Telegram.__new__(telegram.Telegram)
                uploader.config = self.config(url)
                uploader._make_client()
                self.assertEqual(client.call_args.kwargs["proxy"]["proxy_type"], scheme)
                self.assertTrue(status["telegram_proxy_enabled"])
                self.assertTrue(status["telegram_proxy_ready"])
                self.assertEqual(status["telegram_proxy_backend"], "python-socks")
                self.assertEqual(status["telegram_proxy_type"], scheme)
                self.assertNotIn(urlsplit_host(url), json.dumps(status))

    def test_invalid_port_does_not_expose_proxy_credentials(self):
        secret = secrets.token_hex(16)
        with self.assertRaises(common.JobError) as caught:
            telegram.telegram_proxy(f"socks5://{secret}@localhost:invalid")
        self.assertNotIn(secret, str(caught.exception))

    def test_tg_proxy_environment_still_overrides_local_configuration(self):
        local, override = proxy_url("http"), proxy_url("socks5")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_text(json.dumps(self.config(local)), encoding="utf-8")
            with patch.object(common, "ROOT", root), patch.dict("os.environ", {"TG_PROXY": override}, clear=True):
                loaded = common.load_config()
            self.assertEqual(loaded["telegram_proxy"], override)
            self.assertEqual(json.loads((root / "config.json").read_text())["telegram_proxy"], local)

    def test_check_reports_proxy_readiness_without_instantiating_client_or_network(self):
        config = self.config(proxy_url())
        with patch.object(telegram, "import_module", return_value=self.backend()), \
                patch.object(telegram.telegrambaseclient, "python_socks", object()), \
                patch.object(telegram.telethon_connection, "python_socks", object()), \
                patch.object(telegram, "TelegramClient") as client, \
                patch.object(quark, "prepare_runtime", return_value={"quark_cli_version": "1.0.20"}), \
                patch.object(common, "check_crypto", return_value={"native_enabled": True}), \
                redirect_stdout(StringIO()) as output:
            self.assertEqual(main.doctor(config), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["telegram_proxy_enabled"])
        self.assertTrue(result["telegram_proxy_ready"])
        self.assertEqual(result["telegram_proxy_backend"], "python-socks")
        client.assert_not_called()

    def test_check_missing_dependency_fails_before_other_checks(self):
        with patch.object(telegram, "import_module", side_effect=ImportError), \
                patch.object(quark, "prepare_runtime") as other_check:
            with self.assertRaisesRegex(common.JobError, "python-socks"):
                main.doctor(self.config(proxy_url()))
        other_check.assert_not_called()


def urlsplit_host(url):
    from urllib.parse import urlsplit
    return urlsplit(url).hostname


@unittest.skipUnless(HAS_PYTHON_SOCKS, "需从 requirements 安装 python-socks[asyncio]")
class ActualTelethonProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_initialization_emits_no_proxy_ignored_warning(self):
        parsed = telegram.telegram_proxy(proxy_url())
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            client = TelegramClient(MemorySession(), secrets.randbelow(1000000) + 1,
                                    secrets.token_hex(16), proxy=parsed)
        self.assertFalse(any("proxy argument will be ignored" in str(item.message) for item in captured))
        self.assertEqual(client._proxy, parsed)
        await client.disconnect()

    async def test_telethon_async_proxy_connect_reaches_local_proxy_not_direct_destination(self):
        requests = []
        finished = asyncio.Event()
        payload = secrets.token_bytes(32)

        async def handler(reader, writer):
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                requests.append(header.split(b"\r\n", 1)[0])
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                data = await reader.readexactly(len(payload))
                writer.write(data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        parsed = telegram.telegram_proxy(f"http://127.0.0.1:{port}")
        connection = ConnectionTcpFull("telegram-proxy-test.invalid", 443, 2,
                                       loggers=defaultdict(lambda: logging.getLogger("proxy-test")), proxy=parsed)
        error_module = telegram.telethon_connection.python_socks._errors
        original_errors = {name: getattr(error_module, name) for name in
                           ("ProxyError", "ProxyConnectionError", "ProxyTimeoutError")}
        sock = None
        try:
            sock = await connection._proxy_connect(timeout=3)
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(sock, payload)
            received = await asyncio.wait_for(loop.sock_recv(sock, len(payload)), timeout=3)
            self.assertEqual(received, payload)
            self.assertEqual(requests, [b"CONNECT telegram-proxy-test.invalid:443 HTTP/1.1"])
        finally:
            if sock is not None:
                sock.close()
                await asyncio.wait_for(finished.wait(), timeout=3)
            for name, value in original_errors.items():
                setattr(error_module, name, value)
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
