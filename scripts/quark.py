"""夸克上传：调用固定的官方网盘 CLI（quarkclouddrive 1.0.20），不自行实现私有上传协议。

官方 CLI 以逐行 JSON 输出结果；本模块只负责启动、解析、取消和错误归类，
分片、哈希、授权签名、合并和令牌刷新全部交给官方运行时。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import string
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

from common import ROOT, STATE, JobError

VENDOR = ROOT / "vendor" / "quarkclouddrive" / "scripts"
# 官方 CLI 把授权保存在其脚本目录的上两级；从 .state 运行，授权才不会落进分发包内容。
RUNTIME = STATE / "quark-cli" / "scripts"
CLI_NAME = "quark-drive.cjs"
CLI_VERSION = "1.0.20"
# 固定版本及校验值；升级前必须重新审查并跑通真实上传。
CHECKSUMS = {
    "quark-drive.cjs": "39aa12702181ef3736f61bfb3159a04dfe88371163fc1f53f1b9c22efa38be9f",
    "hash-worker.cjs": "c9e76fa0fd418e731980636c13bfe1379bdb60e117f1d6ed7dcf83ff2187a690",
}
# 官方 CLI 通过宿主环境变量识别调用方；本机在 MuseAI 中以 Claude Code 身份运行。
AGENT_ENV = {"CLAUDECODE": "1"}
READY_TIMEOUT = 60
COMMAND_TIMEOUT = 1800
MAX_LINE = 1024 * 1024
PROXY_NAME = "quark_proxy.cjs"
PROXY_SOURCE = Path(__file__).resolve().with_name(PROXY_NAME)
PROXY_VARIABLES = ("QUARK_PROXY", "HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY")
NETWORK_REASON = re.compile(
    r"QUARK_(?:PROXY|DIRECT)_[A-Z_]+|EPROTO|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|"
    r"EAI_AGAIN|EHOSTUNREACH|ENETUNREACH|ERR_TLS_[A-Z_]+|ERR_SSL_[A-Z_]+|"
    r"CERT_[A-Z_]+|DEPTH_ZERO_SELF_SIGNED_CERT|UNABLE_TO_[A-Z_]+", re.I)
AUTH_CODES = {-102, -103, -107, -1408}


class AuthRequired(JobError):
    """官方 CLI 明确要求重新授权。"""


class UploadUnconfirmed(JobError):
    """上传请求异常且不能安全判定为成功或失败。"""


class ProxyConnectionError(JobError):
    """代理配置、连接、认证或代理链路 TLS 出错；不得回退直连。"""


class DirectConnectionError(JobError):
    """直连网络出错，不代表 OAuth 授权失效。"""


def proxy_settings(env=None) -> tuple[str, str]:
    """一条代理覆盖 CLI 全部请求；返回代理值和来源变量名，后者可安全显示。"""
    env = os.environ if env is None else env
    for name in PROXY_VARIABLES:
        value = str(env.get(name) or "").strip() or str(env.get(name.lower()) or "").strip()
        if not value:
            continue
        if name == "QUARK_PROXY" and value.lower() == "direct":
            return "", "QUARK_PROXY"
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.port == 0 or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                    or re.search(r"[\r\n\x00]", value) or re.search(r"%(?![0-9a-fA-F]{2})", value)):
                raise ValueError
            unquote(parsed.username or "", errors="strict")
            unquote(parsed.password or "", errors="strict")
        except (ValueError, UnicodeError):
            raise ProxyConnectionError(f"夸克代理配置无效（来自 {name}）：仅支持 HTTP/HTTPS 代理 URL；"
                                       "请核对协议、端口和凭据编码，不会回退直连") from None
        return value, name
    return "", ""


def child_environment(env=None) -> tuple[dict, str, str]:
    child = dict(os.environ if env is None else env)
    proxy, source = proxy_settings(child)
    for name in (*PROXY_VARIABLES, "NO_PROXY", "NODE_USE_ENV_PROXY"):
        child.pop(name, None)
        child.pop(name.lower(), None)
    child.update(AGENT_ENV)
    child.pop("QUARK_CONFIG_DIR", None)
    child.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
    child.pop("JAVHD_QUARK_PROXY", None)
    if proxy:
        child["JAVHD_QUARK_PROXY"] = proxy
    return child, proxy, source


def redact_proxy_message(text, proxy="") -> str:
    text = str(text)
    # 对登录错误链接仍保留无代理凭据的授权 URL；不能回显代理地址或 Basic 凭据。
    if re.search(r"proxy-authorization", text, re.I):
        return "代理诊断消息已脱敏"
    if proxy:
        parsed = urlsplit(proxy)
        secrets = [proxy, parsed.username, parsed.password]
        secrets += [unquote(value) for value in (parsed.username, parsed.password) if value]
        if parsed.username or parsed.password:
            basic = f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}"
            secrets.append(base64.b64encode(basic.encode()).decode())
        if any(value and value in text for value in secrets):
            return "代理诊断消息已脱敏"
    return re.sub(r"(?:https?|socks5h?)://[^/\s?#<>\"']+@[^/\s?#<>\"']+[^\s<>\"']*", "[代理地址已隐藏]", text)


def transport_failure(reason, proxied):
    # reason 只能是枚举化的错误码，不含远端响应、代理 URL 或用户输入。
    match = NETWORK_REASON.search(str(reason))
    code = match[0].upper() if match else "CONNECTION_FAILED"
    if proxied:
        return ProxyConnectionError(f"夸克代理链路失败（{code}）：请检查代理协议、出口连通性、"
                                    "代理认证和 TLS 证书；不会回退直连，也不会要求重新授权")
    return DirectConnectionError(f"夸克直连失败（{code}）：请检查 DNS、TLS 和直连出口；"
                                 "需要出口代理时设置 QUARK_PROXY 或 HTTPS_PROXY，"
                                 "不要通过重新授权解决网络错误")


def cli_path() -> Path:
    return RUNTIME / CLI_NAME


def verify_runtime(*, env=None) -> dict:
    """校验运行时、Node 与代理配置；不登录、不联网，也不显示代理值。"""
    env = os.environ if env is None else env
    proxy, source = proxy_settings(env)
    if not PROXY_SOURCE.is_file():
        raise JobError("缺少夸克代理适配器，请重新安装本 skill")
    for name, expected in CHECKSUMS.items():
        path = VENDOR / name
        if not path.is_file():
            raise JobError(f"缺少官方夸克运行时文件：{name}；请重新安装本 skill")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise JobError(f"官方夸克运行时文件 {name} 校验失败；请重新安装本 skill")
    node = env.get("QUARK_NODE") or shutil.which("node")
    if not node:
        raise JobError("夸克上传需要 Node.js 16 或更高版本，请安装后重试")
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=READY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise JobError("无法执行 Node.js，请检查安装") from None
    text = (version.stdout or "").strip()
    if version.returncode != 0 or not text.startswith("v"):
        raise JobError("无法确认 Node.js 版本，请检查安装")
    try:
        major = int(text[1:].split(".", 1)[0])
    except ValueError:
        raise JobError("无法确认 Node.js 版本，请检查安装") from None
    if major < 16:
        raise JobError("夸克上传需要 Node.js 16 或更高版本")
    return {"quark_cli_version": CLI_VERSION, "node": text,
            "quark_network_mode": "proxy" if proxy else "direct", "quark_proxy_source": source}


def prepare_runtime(*, env=None) -> dict:
    """把校验过的运行时复制到 .state 再运行，官方授权就会落在运行目录而不是分发包里。"""
    info = verify_runtime(env=env)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    sources = {name: VENDOR / name for name in CHECKSUMS}
    sources[PROXY_NAME] = PROXY_SOURCE
    for name, source in sources.items():
        target = RUNTIME / name
        data = source.read_bytes()
        if not target.is_file() or target.read_bytes() != data:
            temporary = target.with_name(f".{name}.tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
    return info


def error_text(payload: dict) -> str:
    """只保留官方返回的安全 msg，不输出令牌、授权串或本地凭据路径。"""
    message = payload.get("msg")
    if not isinstance(message, str) or not message.strip():
        return f"错误码 {payload.get('code')}"
    text = redact_proxy_message(" ".join(message.split()))
    for word in ("bearer ", "basic ", "access_token", "refresh_token", "agent_auth_code", "proxy-authorization"):
        if word in text.lower():
            return f"错误码 {payload.get('code')}（消息含凭据字段，已省略）"
    return text[:300]


class QuarkCLI:
    """官方 CLI 子进程封装：一次调用只产生一行可解析结果，长任务按行流式读取。"""

    def __init__(self, *, node=None, script=None, timeout=COMMAND_TIMEOUT, env=None):
        source_env = dict(os.environ if env is None else env)
        self.node = node or source_env.get("QUARK_NODE") or shutil.which("node") or ""
        prepare_runtime(env=source_env)
        self.script = Path(script or cli_path())
        self.preload = RUNTIME / PROXY_NAME
        self.timeout = timeout
        self.env, self.proxy, self.proxy_source = child_environment(source_env)
        self.session_id = f"{int(time.time())}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"

    def _argv(self, args):
        return [self.node, "--require", str(self.preload), str(self.script), *args,
                "--session-id", self.session_id]

    def run(self, args, *, timeout=None, on_line=None) -> tuple[int, list[dict], str]:
        """所有命令均通过同一个预加载器；网络故障先于 OAuth 错误归类。"""
        if not self.node:
            raise JobError("夸克上传需要 Node.js，请安装后重试")
        if not self.script.is_file() or not self.preload.is_file():
            raise JobError("缺少官方夸克运行时或代理适配器，请重新安装本 skill")
        process = subprocess.Popen(
            self._argv(args), cwd=str(self.script.parent.parent), env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        lines, reasons, callback_errors = [], [], []
        expired = threading.Event()

        def kill():
            try:
                process.kill()
            except OSError:
                pass

        def read_stdout():
            try:
                with process.stdout:
                    for line in process.stdout:
                        if len(line) > MAX_LINE:
                            continue
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(payload, dict):
                            continue
                        message = payload.get("msg", "")
                        match = NETWORK_REASON.search(str(message))
                        if match:
                            reasons.append(match[0])
                        if isinstance(message, str):
                            payload["msg"] = redact_proxy_message(message, self.proxy)
                        lines.append(payload)
                        if on_line is not None:
                            on_line(payload)
            except Exception as exc:
                callback_errors.append(exc)
                kill()

        def read_stderr():
            with process.stderr:
                for line in process.stderr:
                    if line.startswith(("JAVHD_QUARK_NETWORK_ERROR ", "JAVHD_QUARK_PROXY_ERROR ")):
                        try:
                            detail = json.loads(line.split(" ", 1)[1])
                            if detail.get("critical"):
                                match = NETWORK_REASON.search(str(detail.get("reason", "")))
                                reasons.append(match[0] if match else detail.get("code", "QUARK_PROXY_CONNECT"))
                        except (ValueError, AttributeError):
                            pass
                    else:
                        match = NETWORK_REASON.search(line)
                        if match:
                            reasons.append(match[0])
                    # stderr 只提取枚举化错误码；不保存或转发可能含代理密码的原始堆栈。

        def expire():
            expired.set()
            kill()

        threads = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        timer = threading.Timer(timeout or self.timeout, expire)
        timer.daemon = True
        for thread in threads:
            thread.start()
        timer.start()
        try:
            code = process.wait()
        except BaseException:
            kill()
            process.wait()
            raise
        finally:
            timer.cancel()
            for thread in threads:
                thread.join()
        if callback_errors:
            raise callback_errors[0]
        if expired.is_set():
            mode = "代理链路" if self.proxy else "直连链路"
            raise JobError(f"夸克命令超时（{mode}），结果未确认；不会自动整项重传")
        final = next((line for line in reversed(lines) if line.get("type") == "result"), None)
        if reasons and (code != 0 or final is None or str(final.get("code")) != "0"):
            raise transport_failure(reasons[-1], bool(self.proxy))
        return code, lines, ""

    def call(self, args, **kwargs) -> dict:
        code, lines, stderr = self.run(args, **kwargs)
        return command_result(code, lines, stderr, proxied=bool(self.proxy))


def raise_for(payload: dict, *, proxied=False):
    message = str(payload.get("msg", ""))
    match = NETWORK_REASON.search(message)
    if match:
        raise transport_failure(match[0], proxied)
    text = error_text(payload)
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        code = None
    if code in AUTH_CODES:
        raise AuthRequired(f"夸克授权失败：{text}")
    raise JobError(f"夸克操作失败：{text}")


def command_result(code, lines, stderr="", *, proxied=False):
    payload = next((line for line in reversed(lines) if line.get("type") == "result"), None)
    if payload is None:
        match = NETWORK_REASON.search(stderr)
        if match:
            raise transport_failure(match[0], proxied)
        raise JobError(f"夸克命令行没有返回结果（退出码 {code}），结果未确认")
    if str(payload.get("code")) != "0":
        raise_for(payload, proxied=proxied)
    if code != 0:
        raise JobError(f"夸克进程异常退出（退出码 {code}），结果未确认；不会自动重传")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def check_network(*, env=None, timeout=30) -> dict:
    """仅探测官方 API 的 TLS/HTTP 可达性，不加载 CLI、不读授权或上传文件。"""
    source = dict(os.environ if env is None else env)
    info = prepare_runtime(env=source)
    child, proxy, _ = child_environment(source)
    node = source.get("QUARK_NODE") or shutil.which("node")
    try:
        completed = subprocess.run([node, str(RUNTIME / PROXY_NAME)], cwd=str(RUNTIME.parent),
                                   env=child, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise transport_failure("ETIMEDOUT", bool(proxy)) from None
    except OSError:
        raise JobError("无法启动夸克网络自检，请检查 Node.js 安装") from None
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise transport_failure("CONNECTION_FAILED", bool(proxy)) from None
    if completed.returncode != 0 or result.get("code") != 0:
        raise transport_failure(result.get("reason", "CONNECTION_FAILED"), bool(proxy))
    return {**info, "http_status": result.get("http_status"), "tls_verified": result.get("tls_verified"),
            "authorization_checked": False}


class Quark:
    def __init__(self, *, cli=None):
        self.cli = cli or QuarkCLI()

    def _result(self, args, **kwargs) -> dict:
        code, lines, stderr = self.cli.run(args, **kwargs)
        return command_result(code, lines, stderr, proxied=bool(getattr(self.cli, "proxy", "")))

    def authorized(self) -> bool:
        """用只读账号查询确认现有授权；只有明确的认证失败才算未授权。"""
        try:
            self._result(["get-user-info"])
            return True
        except AuthRequired:
            return False

    def login(self, force=False, *, prompt=None):
        """复用已保存授权；需要时启动官方浏览器 OAuth，失败时可由调用方补充授权码。"""
        if not force and self.authorized():
            return
        code, lines, stderr = self.cli.run(["login"], timeout=180)
        payload = next((line for line in reversed(lines) if line.get("type") == "result"), None)
        if payload is not None and payload.get("code") == -118:
            return  # 官方明确表示当前账号已有授权。
        if payload is not None and str(payload.get("code")) == "0":
            command_result(code, lines, stderr)
            return
        raw_message = str(payload.get("msg", "")) if payload else ""
        match = NETWORK_REASON.search(raw_message + " " + stderr)
        if match:
            raise transport_failure(match[0], bool(getattr(self.cli, "proxy", "")))
        browser_pending = payload is not None and (payload.get("code") in (-106, -114) or re.search(
            r"浏览器.*失败|还没完成授权|未完成授权|等待.*授权", raw_message))
        if not browser_pending:
            command_result(code, lines, stderr, proxied=bool(getattr(self.cli, "proxy", "")))
        message = error_text(payload) if payload else "浏览器授权未完成"
        if prompt is None:
            raise AuthRequired(f"夸克授权未完成：{message}")
        token = str(prompt(message) or "").strip()
        if not token:
            raise AuthRequired("没有收到夸克授权码，上传已取消")
        if not token.replace("-", "").isalnum() or len(token) > 512:
            raise AuthRequired("夸克授权码格式不正确；请重新复制链接中 code 参数的值")
        self._result(["login", "--token", token], timeout=180)

    def folder(self, path="JAVHD") -> str:
        """逐级创建或复用目录；create-folder 对同名目录幂等。"""
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if any(part in (".", "..") or "\0" in part for part in parts):
            raise JobError("网盘目录不能包含 .、.. 或空字符")
        parent = "0"
        for name in parts:
            data = self._result(["create-folder", "--dir-path", name, "--parent-fid", parent])
            fid = data.get("fid")
            if not isinstance(fid, str) or not fid:
                raise JobError("夸克未返回目录 ID")
            parent = fid
        return parent

    def upload(self, media) -> str:
        return self.upload_file(media.video, parent_folder_id=self.folder())

    def upload_file(self, path: Path, *, parent_folder_id="0", progress_callback=None,
                    transfer_callback=None) -> str:
        # 按调用方 cwd 解析一次；CLI 的 cwd 是独立运行目录，不能再传相对路径。
        try:
            path = Path(path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            raise JobError("本地文件路径无效，无法解析为绝对路径") from None
        try:
            before = (path.stat().st_size, path.stat().st_mtime_ns)
        except FileNotFoundError:
            raise JobError(f"本地文件不存在：{path}") from None
        except OSError:
            raise JobError(f"无法读取本地文件：{path}") from None
        if not path.is_file():
            raise JobError(f"本地路径不是文件：{path}")
        if before[0] <= 0:
            raise JobError(f"本地文件为空：{path}")
        finished, failures, final = {}, [], {}

        def on_line(payload):
            kind = payload.get("type")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if kind == "progress":
                current, total = data.get("current"), data.get("total")
                if isinstance(current, int) and isinstance(total, int) and total > 0 and transfer_callback:
                    transfer_callback(min(current, total), total)
            elif kind == "list":
                if str(payload.get("code")) == "0":
                    finished[data.get("fileId") or ""] = bool(data.get("instantUpload"))
                else:
                    failures.append(f"[{payload.get('code')}] {error_text(payload)}")
            elif kind == "result" and str(payload.get("code")) == "0":
                final.update(data)

        def unchanged():
            try:
                return (path.stat().st_size, path.stat().st_mtime_ns) == before
            except OSError:
                return False

        try:
            self._result(["upload", str(path), "--parent-fid", parent_folder_id], on_line=on_line)
        except (AuthRequired, ProxyConnectionError, DirectConnectionError):
            raise
        except JobError as exc:
            if not unchanged():
                raise JobError("上传期间本地文件发生变化，结果未确认；请先检查网盘目标目录") from None
            detail = "；".join(failures) or str(exc)
            raise UploadUnconfirmed(f"夸克上传未确认：{detail}；请先检查网盘，不要直接重传") from None
        if not unchanged():
            raise JobError("上传期间本地文件发生变化，结果未确认；请先检查网盘目标目录")
        instant = any(finished.values()) or bool(final.get("instantUpload"))
        if progress_callback is not None:
            progress_callback(100, "秒传完成，正在核验远端文件..." if instant else "上传完成，正在核验远端文件...")
        # 以官方最终结果的 fids 为准；单文件 list 行只补充秒传标记。
        ids = final.get("fids")
        if not isinstance(ids, list) or not ids or not all(isinstance(fid, str) and fid for fid in ids):
            detail = "；".join(failures) or "官方最终结果没有返回文件 ID"
            raise UploadUnconfirmed(f"夸克上传未确认：{detail}；请先检查网盘，不要直接重传")
        return ids[0]

    def close(self):
        return None
