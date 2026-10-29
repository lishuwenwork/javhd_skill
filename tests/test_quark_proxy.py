"""夸克代理离线测试：真实 Node + 本地 TLS/CONNECT 服务，不访问夸克或真实出口代理。"""

import base64
from contextlib import contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import quark


class ProxyConfigurationTests(unittest.TestCase):
    def test_priority_and_explicit_direct_are_local_to_quark(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            value = f"http://127.0.0.1:{listener.getsockname()[1]}"
        values = {name: value for name in quark.PROXY_VARIABLES}
        for name in quark.PROXY_VARIABLES:
            self.assertEqual(quark.proxy_settings(values), (value, name))
            values.pop(name)
        self.assertEqual(quark.proxy_settings({"https_proxy": value})[0], value)
        env = {"QUARK_PROXY": "direct", "HTTPS_PROXY": value, "NO_PROXY": "*"}
        child, proxy, source = quark.child_environment(env)
        self.assertEqual(proxy, "")
        self.assertEqual(source, "QUARK_PROXY")
        self.assertNotIn("HTTPS_PROXY", child)
        self.assertEqual(env["HTTPS_PROXY"], value)

    def test_invalid_proxy_is_not_silently_ignored(self):
        for value in ("missing-scheme", "socks5://localhost", "http://localhost:0",
                      "https://localhost:99999", "http://localhost/path", "http://localhost?token=x"):
            with self.subTest(value=value), self.assertRaises(quark.ProxyConnectionError):
                quark.proxy_settings({"QUARK_PROXY": value})

    def test_proxy_passwords_are_not_echoed_or_written_to_metadata(self):
        username, password = secrets.token_hex(12), secrets.token_hex(12)
        proxy = f"http://{username}:{password}@localhost"
        with patch.object(quark.subprocess, "run", return_value=MagicMock(returncode=0, stdout="v22.0.0")):
            info = quark.verify_runtime(env={"QUARK_PROXY": proxy})
        self.assertEqual(info["quark_network_mode"], "proxy")
        self.assertEqual(info["quark_proxy_source"], "QUARK_PROXY")
        self.assertNotIn(password, json.dumps(info))
        for message in (proxy, f"error password {password}", f"Proxy-Authorization: {password}"):
            self.assertNotIn(password, quark.redact_proxy_message(message, proxy))
        self.assertNotIn(password, quark.error_text({"code": 1, "msg": proxy}))

    def test_login_network_failure_never_opens_browser_or_requests_code(self):
        for proxied, expected in ((False, quark.DirectConnectionError), (True, quark.ProxyConnectionError)):
            cli = MagicMock()
            cli.proxy = "configured" if proxied else ""
            cli.run.return_value = (1, [{"code": -108, "action": "login", "type": "result",
                                        "msg": "write EPROTO SSL wrong version number"}], "")
            prompt = MagicMock()
            with self.assertRaises(expected):
                quark.Quark(cli=cli).login(force=True, prompt=prompt)
            prompt.assert_not_called()
            self.assertEqual(cli.run.call_count, 1)

    def test_failure_before_authorization_is_not_misreported_as_auth_required(self):
        cli = MagicMock()
        cli.proxy = ""
        cli.run.return_value = (1, [{"code": -108, "action": "login", "type": "result",
                                    "msg": "获取授权页地址失败"}], "")
        prompt = MagicMock()
        with self.assertRaises(quark.JobError) as caught:
            quark.Quark(cli=cli).login(force=True, prompt=prompt)
        self.assertNotIsInstance(caught.exception, quark.AuthRequired)
        prompt.assert_not_called()
        with self.assertRaises(quark.AuthRequired):
            quark.raise_for({"code": -107, "msg": "授权已失效", "action": "login"})

    def test_distribution_contains_proxy_adapter_but_no_environment_credentials(self):
        import package_skill
        secret = secrets.token_hex(24)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            for name in package_skill.FILES:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            with patch.dict(os.environ, {"QUARK_PROXY": secret}):
                package_skill.build_archive(root, Path(temp) / "bundle.zip")
            with zipfile.ZipFile(Path(temp) / "bundle.zip") as archive:
                names = archive.namelist()
                data = b"".join(archive.read(name) for name in names)
            self.assertIn("javhd/scripts/quark_proxy.cjs", names)
            self.assertIn("javhd/tests/quark_network_check.py", names)
            self.assertFalse(any("/.state/" in name for name in names))
            self.assertNotIn(secret.encode(), data)

    def test_exit_failure_is_not_overridden_by_success_payload(self):
        with self.assertRaises(quark.JobError):
            quark.command_result(1, [{"code": 0, "type": "result", "data": {"fids": ["not-confirmed"]}}])

    def test_list_success_without_final_fids_is_not_upload_success(self):
        cli = MagicMock()
        cli.proxy = ""
        lines = [{"code": 0, "type": "list", "data": {"fileId": "not-final"}},
                 {"code": 0, "type": "result", "data": {"fids": []}}]
        def run(args, **kwargs):
            for line in lines:
                kwargs["on_line"](line)
            return 0, lines, ""
        cli.run.side_effect = run
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.bin"
            path.write_bytes(secrets.token_bytes(16))
            with self.assertRaises(quark.UploadUnconfirmed):
                quark.Quark(cli=cli).upload_file(path)


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Node 退出时会关闭测试的 keep-alive 连接。

    def reply(self, status, data):
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(shutil.which("node") and shutil.which("openssl"), "需要 Node.js 和 OpenSSL CLI")
class ProxyTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert, cls.key = cls.root / "test-ca.pem", cls.root / "test-key.pem"
        subprocess.run([
            shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=quark-proxy-test.invalid", "-addext",
            "subjectAltName=DNS:quark-proxy-test.invalid,DNS:open-api-drive.quark.cn,DNS:bucket-proxy-test.invalid,IP:127.0.0.1",
            "-keyout", str(cls.key), "-out", str(cls.cert),
        ], cwd=ROOT, capture_output=True, check=True, timeout=30)
        cls.fixture = cls.root / "fixture" / "scripts" / "cli.cjs"
        cls.fixture.parent.mkdir(parents=True)
        cls.fixture.write_text("""
const fs = require('node:fs');
const https = require('node:https');
const http = require('node:http');
const cmd = process.argv[2];
const body = cmd === 'upload' ? fs.readFileSync(process.argv[3]) : Buffer.alloc(0);
const host = cmd === 'upload' ? 'bucket-proxy-test.invalid' : 'open-api-drive.quark.cn';
const endpoint = process.env.FIXTURE_URL || `https://${host}/${cmd}`;
const transport = endpoint.startsWith('http:') ? http : https;
const options = {method: body.length ? 'PUT' : 'GET', headers: {'Content-Length': body.length},
  agent: new transport.Agent({keepAlive: true})};
const request = transport.request(new URL(endpoint), options, response => {
  let data = '';
  response.on('data', chunk => data += chunk);
  response.on('end', () => {
    console.log(data);
    process.exit(response.statusCode === 200 ? 0 : 1);
  });
});
request.once('error', error => {
  console.log(JSON.stringify({code: process.env.HIDE_NETWORK_CAUSE ? -107 : -108, action: 'login', type: 'result',
    msg: process.env.HIDE_NETWORK_CAUSE ? 'authorization expired' : error.message}));
  process.exit(1);
});
request.end(body);
""", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @contextmanager
    def servers(self, *, tls_proxy=False, reject_auth=False):
        origin_requests, proxy_requests = [], []
        username = secrets.token_hex(12) + "+"
        password = secrets.token_hex(12) + "@:/ "
        expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

        class Origin(QuietHandler):
            def do_GET(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                origin_requests.append((self.path, body, dict(self.headers)))
                if self.path == "/reset":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                data = {"fid": "JAVHD-folder"} if "create-folder" in self.path else {"fids": ["uploaded|opaque-id"]}
                self.reply(200, json.dumps({"code": 0, "action": self.path.lstrip("/"), "type": "result", "data": data}).encode())
            do_PUT = do_GET

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        origin.socket = context.wrap_socket(origin.socket, server_side=True)
        plain = ThreadingHTTPServer(("127.0.0.1", 0), Origin)

        class Proxy(QuietHandler):
            def allowed(self):
                proxy_requests.append((self.command, self.path, self.headers.get("Proxy-Authorization")))
                if reject_auth or self.headers.get("Proxy-Authorization") != expected:
                    self.reply(407, b"")
                    return False
                return True

            def do_CONNECT(self):
                if not self.allowed():
                    return
                remote = socket.create_connection(origin.server_address, timeout=5)
                try:
                    self.send_response(200)
                    self.end_headers()
                    peers = [self.connection, remote]
                    while True:
                        pending = [peer for peer in peers if isinstance(peer, ssl.SSLSocket) and peer.pending()]
                        readable = pending or select.select(peers, [], [], 5)[0]
                        if not readable:
                            return
                        for peer in readable:
                            data = peer.recv(65536)
                            if not data:
                                return
                            (remote if peer is self.connection else self.connection).sendall(data)
                except (OSError, ssl.SSLError):
                    pass
                finally:
                    remote.close()

            def do_GET(self):
                if not self.allowed():
                    return
                target = urlsplit(self.path)
                headers = {key: value for key, value in self.headers.items() if key.lower() != "proxy-authorization"}
                connection = http.client.HTTPConnection(*plain.server_address, timeout=5)
                try:
                    connection.request(self.command, target.path, headers=headers)
                    response = connection.getresponse()
                    self.reply(response.status, response.read())
                finally:
                    connection.close()

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        if tls_proxy:
            proxy.socket = context.wrap_socket(proxy.socket, server_side=True)
        running = [origin, plain, proxy]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in running]
        for thread in threads:
            thread.start()
        env = dict(os.environ)
        for name in (*quark.PROXY_VARIABLES, "NO_PROXY", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "FIXTURE_URL"):
            env.pop(name, None)
            env.pop(name.lower(), None)
        scheme = "https" if tls_proxy else "http"
        env["QUARK_PROXY"] = f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:{proxy.server_address[1]}"
        env["NODE_EXTRA_CA_CERTS"] = str(self.cert)
        env["NO_PROXY"] = "*"  # 必须仍统一走所选代理，不允许 OSS 绕过。
        try:
            with patch.object(quark, "RUNTIME", self.root / "runtime" / "scripts"):
                yield env, origin_requests, proxy_requests, origin, proxy
        finally:
            for server in running:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()

    def cli(self, env):
        return quark.QuarkCLI(script=self.fixture, timeout=15, env=env)

    def test_http_and_https_proxy_route_all_commands_and_upload_body(self):
        for secure_proxy in (False, True):
            with self.subTest(tls_proxy=secure_proxy), self.servers(tls_proxy=secure_proxy) as setup:
                env, origin, proxy, _, _ = setup
                cli = self.cli(env)
                cli.call(["login"])
                client = quark.Quark(cli=cli)
                self.assertTrue(client.authorized())
                self.assertEqual(client.folder("JAVHD"), "JAVHD-folder")
                data = secrets.token_bytes(2 * 1024 * 1024 + 3)
                path = self.root / "upload.bin"
                path.write_bytes(data)
                self.assertEqual(client.upload_file(path, parent_folder_id="JAVHD-folder"), "uploaded|opaque-id")
                self.assertEqual([entry[0] for entry in origin], ["/login", "/get-user-info", "/create-folder", "/upload"])
                self.assertEqual(hashlib.sha256(origin[-1][1]).digest(), hashlib.sha256(data).digest())
                self.assertGreaterEqual(len(proxy), 4)
                self.assertTrue(all(entry[0] == "CONNECT" and entry[2] for entry in proxy))
                self.assertTrue(all(not any(key.lower() == "proxy-authorization" for key in entry[2]) for entry in origin))
                argv = " ".join(cli._argv(["upload", str(path)]))
                self.assertNotIn(urlsplit(env["QUARK_PROXY"]).password, argv)

    def test_http_forward_proxy_does_not_leak_proxy_auth_to_origin(self):
        for secure_proxy in (False, True):
            with self.subTest(tls_proxy=secure_proxy), self.servers(tls_proxy=secure_proxy) as setup:
                env, origin, proxy, _, _ = setup
                env["FIXTURE_URL"] = "http://quark-proxy-test.invalid/plain"
                self.cli(env).call(["get-user-info"])
                self.assertEqual(origin[0][0], "/plain")
                self.assertTrue(proxy[0][1].startswith("http://"))
                self.assertNotIn("Proxy-Authorization", origin[0][2])

    def test_proxy_auth_failure_is_not_oauth_failure_and_does_not_fall_back(self):
        with self.servers(reject_auth=True) as setup:
            env, origin, _, _, _ = setup
            prompt = MagicMock()
            with self.assertRaises(quark.ProxyConnectionError) as caught:
                quark.Quark(cli=self.cli(env)).login(force=True, prompt=prompt)
            self.assertIn("QUARK_PROXY_AUTH", str(caught.exception))
            self.assertNotIn(urlsplit(env["QUARK_PROXY"]).password, str(caught.exception))
            prompt.assert_not_called()
            self.assertEqual(origin, [])

    def test_wrong_proxy_scheme_is_classified_as_proxy_failure(self):
        with self.servers() as setup:
            env, origin, _, _, _ = setup
            env["QUARK_PROXY"] = env["QUARK_PROXY"].replace("http:", "https:", 1)
            with self.assertRaises(quark.ProxyConnectionError):
                self.cli(env).call(["login"])
            self.assertEqual(origin, [])

    def test_invalid_tls_certificate_is_not_accepted(self):
        with self.servers() as setup:
            env, _, _, _, _ = setup
            env.pop("NODE_EXTRA_CA_CERTS")
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
            with self.assertRaises(quark.ProxyConnectionError):
                self.cli(env).call(["login"])

    def test_explicit_direct_skips_proxy_without_changing_parent_environment(self):
        with self.servers() as setup:
            env, origin, proxy, server, _ = setup
            env["QUARK_PROXY"] = "direct"
            env["FIXTURE_URL"] = f"https://127.0.0.1:{server.server_address[1]}/direct"
            self.cli(env).call(["get-user-info"])
            self.assertEqual(origin[0][0], "/direct")
            self.assertEqual(proxy, [])
            self.assertEqual(env["QUARK_PROXY"], "direct")

    def test_post_tls_disconnect_is_not_hidden_by_sdk_auth_error(self):
        for direct in (False, True):
            with self.subTest(direct=direct), self.servers() as setup:
                env, origin, _, server, _ = setup
                env["HIDE_NETWORK_CAUSE"] = "1"
                if direct:
                    env["QUARK_PROXY"] = "direct"
                    env["FIXTURE_URL"] = f"https://127.0.0.1:{server.server_address[1]}/reset"
                else:
                    env["FIXTURE_URL"] = "https://quark-proxy-test.invalid/reset"
                prompt = MagicMock()
                expected = quark.DirectConnectionError if direct else quark.ProxyConnectionError
                with self.assertRaises(expected) as caught:
                    quark.Quark(cli=self.cli(env)).login(force=True, prompt=prompt)
                self.assertIn("ECONNRESET", str(caught.exception))
                prompt.assert_not_called()
                self.assertEqual(origin[0][0], "/reset")

    def test_captured_cli_diagnostics_do_not_expose_proxy_credentials(self):
        with self.servers() as setup:
            env, _, _, _, _ = setup
            script = self.root / "fixture" / "scripts" / "diagnostic.cjs"
            script.write_text("""
const proxy = process.env.JAVHD_QUARK_PROXY;
const parsed = new URL(proxy);
const basic = Buffer.from(decodeURIComponent(parsed.username) + ':' + decodeURIComponent(parsed.password)).toString('base64');
console.error(proxy);
console.log(JSON.stringify({code: -108, type: 'result', action: 'login', msg: proxy + ' Basic ' + basic}));
process.exit(1);
""", encoding="utf-8")
            cli = quark.QuarkCLI(script=script, env=env)
            code, lines, stderr = cli.run(["login"])
            self.assertEqual(code, 1)
            self.assertEqual(stderr, "")
            self.assertEqual(lines[-1]["msg"], "代理诊断消息已脱敏")
            self.assertNotIn(urlsplit(env["QUARK_PROXY"]).password, json.dumps(lines))

    def test_timeout_does_not_prompt_for_oauth_or_retry(self):
        with self.servers() as setup:
            env, _, _, _, _ = setup
            script = self.root / "fixture" / "scripts" / "timeout.cjs"
            script.write_text("setInterval(() => {}, 1000);", encoding="utf-8")
            cli = quark.QuarkCLI(script=script, env=env, timeout=0.2)
            with self.assertRaisesRegex(quark.JobError, "命令超时.*不会自动整项重传") as caught:
                cli.call(["upload"])
            self.assertNotIsInstance(caught.exception, quark.AuthRequired)

    def test_readonly_probe_uses_same_proxy_and_does_not_load_official_cli(self):
        with self.servers() as setup:
            env, origin, _, _, _ = setup
            result = quark.check_network(env=env)
            self.assertEqual(result["quark_network_mode"], "proxy")
            self.assertEqual(result["http_status"], 200)
            self.assertTrue(result["tls_verified"])
            self.assertFalse(result["authorization_checked"])
            self.assertEqual(origin[0][0], "/")
            self.assertFalse((quark.RUNTIME.parent / "claudecode").exists())


if __name__ == "__main__":
    unittest.main()
