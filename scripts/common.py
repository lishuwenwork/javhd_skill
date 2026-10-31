"""输入、配置、二维码与本地临时状态；上传业务记录由 D1 保存。"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
FORMAT_HINT = "请输入：ID：123,456 标签：#4k、#高清；有详情页时每行填写 ID：123 URL：https://javhd.com/详情页（标签可省略）"
_PRINT_LOCK = threading.Lock()


class JobError(Exception):
    """可以安全显示给用户的错误，不含 Cookie、直链或视频标题。"""


def normalize_tag(tag: str) -> str:
    return f"#ID{tag[1:]}" if re.fullmatch(r"#[0-9]+", tag) else tag


def parse_tags(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if not re.fullmatch(r"#\w+(?:[、,， \t]+#\w+)*", value):
        raise ValueError(FORMAT_HINT)
    return list(dict.fromkeys(normalize_tag(tag) for tag in re.findall(r"#\w+", value)))


def parse_input(value: str) -> tuple[str, list[str]]:
    match = re.fullmatch(r"id:[ \t]*([0-9]+)(?:[ \t]+标签:[ \t]*(.*))?", value.strip())
    if not match or int(match[1]) == 0:
        raise ValueError(FORMAT_HINT)
    return match[1], parse_tags(match[2] or "")


@dataclass
class VideoRequest:
    video_id: str
    tags: list[str]
    page_url: str = ""


def detail_page_url(value: str) -> str:
    """详情页可使用不同路由；保留原路径/查询，仅限制为本站的无凭据 HTTP(S) 地址。"""
    try:
        if not isinstance(value, str) or not value or re.search(r"[\s\\\x00-\x1f\x7f]", value):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme not in {"http", "https"} or not (host == "javhd.com" or host.endswith(".javhd.com")) \
                or parts.username is not None or parts.password is not None or parts.port not in (None, 80, 443):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(FORMAT_HINT) from None
    return value


def parse_requests(value: str) -> list[VideoRequest]:
    """整批先校验；支持 ID 列表和多行 ID/URL/标签，URL 始终绑定同组单个 ID。"""
    text = value.strip()
    fields = list(re.finditer(r"(?<!\S)(id|url|标签)\s*[:：][ \t]*", text, flags=re.I))
    if not fields or fields[0].start() != 0 or fields[0][1].lower() != "id":
        raise ValueError(FORMAT_HINT)
    groups, group = [], {}
    for index, match in enumerate(fields):
        name = match[1].lower()
        content = text[match.end():fields[index + 1].start() if index + 1 < len(fields) else len(text)].strip()
        if name == "id" and group:
            groups.append(group)
            group = {}
        if name in group:
            raise ValueError(FORMAT_HINT)
        group[name] = content
    groups.append(group)
    requests = {}
    for group in groups:
        ids = group.get("id", "")
        if not re.fullmatch(r"[0-9]+(?:[\s,，、]+[0-9]+)*", ids):
            raise ValueError(FORMAT_HINT)
        ids = re.split(r"[\s,，、]+", ids)
        if any(int(video_id) == 0 for video_id in ids):
            raise ValueError(FORMAT_HINT)
        tags = parse_tags(group.get("标签", ""))
        page_url = detail_page_url(group["url"]) if "url" in group else ""
        if page_url and len(ids) != 1:
            raise ValueError(FORMAT_HINT)
        for video_id in ids:
            if video_id not in requests:
                requests[video_id] = VideoRequest(video_id, list(tags), page_url)
            else:
                previous = requests[video_id]
                if previous.page_url and page_url and previous.page_url != page_url:
                    raise ValueError(FORMAT_HINT)
                previous.page_url = previous.page_url or page_url
                previous.tags = list(dict.fromkeys([*previous.tags, *tags]))
    return list(requests.values())


def tags_with_id(video_id: str, tags: list[str]) -> list[str]:
    """上传 Telegram 时总带上 #ID{ID}，纯数字标签使用大写 ID 前缀并按顺序去重。"""
    return list(dict.fromkeys([f"#ID{video_id}", *(normalize_tag(tag) for tag in tags)]))


def emit(event: str, **data) -> None:
    with _PRINT_LOCK:
        print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def save_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows 上索引器或防护软件可能短暂占用目标；只重试本地原子替换，不重试上传。
        for attempt in range(4):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def load_config() -> dict:
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JobError("缺少 config.json，请在 skill 根目录复制 config.example.json 为 config.json，"
                       "并在本地填写配置；不要覆盖已有配置") from None
    except (ValueError, UnicodeError):
        raise JobError("config.json 不是有效的 UTF-8 JSON 配置") from None
    if not isinstance(config, dict):
        raise JobError("config.json 必须是 JSON 对象")
    for name in ("ACCOUNT_ID", "DATABASE_ID", "API_TOKEN"):
        config.setdefault(name, "")
    config.setdefault("SNOWFLAKE_INSTANCE_ID", 0)
    from records import validate_config
    validate_config(config)
    config.setdefault("telegram_premium", False)
    if type(config["telegram_premium"]) is not bool:
        raise JobError("telegram_premium 必须是 JSON 布尔值 true 或 false")
    config.setdefault("telegram_split_direction", "tail")
    if config["telegram_split_direction"] not in ("head", "tail"):
        raise JobError("telegram_split_direction 必须是 head 或 tail")
    for key, env in (("telegram_api_id", "TG_API_ID"), ("telegram_api_hash", "TG_API_HASH"),
                     ("telegram_proxy", "TG_PROXY"), ("http_proxy", "HTTP_PROXY_URL"),
                     ("javhd_cookies_file", "JAVHD_COOKIES_FILE")):
        if os.environ.get(env):
            config[key] = os.environ[env]
    config["telegram_api_id"] = int(config["telegram_api_id"])
    channel = str(config["telegram_channel_id"]).strip()
    if channel != "0" and not re.fullmatch(r"-100[0-9]+", channel):
        raise JobError("telegram_channel_id 必须是 -100 开头的频道 ID，未配置时填 0")
    config["telegram_channel_id"] = int(channel)
    config["upload_concurrency"] = int(config["upload_concurrency"])
    if not 1 <= config["upload_concurrency"] <= 16:
        raise JobError("upload_concurrency 必须在 1 到 16 之间")
    return config


def check_crypto() -> dict:
    import cryptg
    import inspect
    from telethon.crypto import aes

    if aes.cryptg is not cryptg or not inspect.isbuiltin(cryptg.encrypt_ige):
        raise JobError("Telethon 没有启用 cryptg 原生加速")
    plain, key, iv = bytes(range(32)), bytes(range(32)), bytes(range(32))
    encrypted = aes.AES.encrypt_ige(plain, key, iv)
    if aes.AES.decrypt_ige(encrypted, key, iv) != plain:
        raise JobError("cryptg 加解密自检失败")
    return {"cryptg": importlib.metadata.version("cryptg"), "native_enabled": True}


def show_qr(service: str, url: str) -> Path:
    import qrcode

    path = STATE / "qr" / f"{service}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    qrcode.make(url).save(temp, format="PNG")
    temp.replace(path)
    save_json(STATE / f"login-{service}.json", {"status": "scan", "qr": str(path)})
    emit("qr", service=service, path=str(path))
    return path


def login_status(service: str, status: str) -> None:
    save_json(STATE / f"login-{service}.json", {"status": status})
    if status == "ready":
        (STATE / "qr" / f"{service}.png").unlink(missing_ok=True)
    emit("login", service=service, status=status)


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def caption_and_tags(title: str, tags: list[str]):
    from telethon.types import MessageEntityHashtag

    tail = " ".join(tags)
    budget = 1024 - utf16_length(tail) - 2
    if budget < 1:
        raise JobError("标签总长度超过 Telegram 视频说明上限")
    title = title.encode("utf-16-le")[:budget * 2].decode("utf-16-le", errors="ignore")
    caption = title + "\n\n" + tail
    offset = utf16_length(title + "\n\n")
    entities = []
    for tag in tags:
        size = utf16_length(tag)
        entities.append(MessageEntityHashtag(offset=offset, length=size))
        offset += size + 1
    return caption, entities
