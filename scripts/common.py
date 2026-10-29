"""输入、配置和二维码；这里不保存任何视频元数据。"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
FORMAT_HINT = "请输入：id:123 标签:#4k、#高清（标签可省略）"
_PRINT_LOCK = threading.Lock()


class JobError(Exception):
    """可以安全显示给用户的错误，不含 Cookie、直链或视频标题。"""


def parse_tags(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if not re.fullmatch(r"#\w+(?:[、,， \t]+#\w+)*", value):
        raise ValueError(FORMAT_HINT)
    return list(dict.fromkeys(re.findall(r"#\w+", value)))


def parse_input(value: str) -> tuple[str, list[str]]:
    match = re.fullmatch(r"id:[ \t]*([0-9]+)(?:[ \t]+标签:[ \t]*(.*))?", value.strip())
    if not match or int(match[1]) == 0:
        raise ValueError(FORMAT_HINT)
    return match[1], parse_tags(match[2] or "")


def tags_with_id(video_id: str, tags: list[str]) -> list[str]:
    """上传 Telegram 时总带上 #{ID}，其余标签保留原文并按顺序去重。"""
    return list(dict.fromkeys([f"#{video_id}", *tags]))


def emit(event: str, **data) -> None:
    with _PRINT_LOCK:
        print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
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
