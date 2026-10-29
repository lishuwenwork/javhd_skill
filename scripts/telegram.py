"""Telethon：扫码、512 KiB 滑动窗口上传、带封面的视频相册。"""

from __future__ import annotations

import asyncio
from getpass import getpass
import hashlib
from importlib import import_module
import os
from pathlib import Path
import sys
import time
from urllib.parse import unquote, urlsplit

from telethon import TelegramClient, errors, functions, helpers, types, utils
from telethon.client import telegrambaseclient
from telethon.network.connection import connection as telethon_connection

from common import (STATE, JobError, caption_and_tags, check_crypto, emit, login_status,
                    show_qr)

PART_SIZE = 512 * 1024
DEFAULT_CONCURRENCY = 6
# 协议允许同时保留多个未确认分块；上限防止单文件占满连接。
MAX_IN_FLIGHT = 10
BIG_FILE_THRESHOLD = 10 * 1024 * 1024
FLOOD_ERRORS = (errors.FloodWaitError, errors.SlowModeWaitError, errors.FloodPremiumWaitError)


class FloodGate:
    """所有上传任务共享等待期限；只重试服务端明确要求等待的请求。"""

    def __init__(self, client):
        self.client = client
        self.until = 0.0

    async def __call__(self, request):
        return await self.run(lambda: self.client(request))

    async def run(self, action):
        loop = asyncio.get_running_loop()
        while True:
            delay = self.until - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            try:
                return await action()
            except FLOOD_ERRORS as exc:
                self.until = max(self.until, loop.time() + exc.seconds + 1)


async def upload_parts(call, path: Path, concurrency: int = DEFAULT_CONCURRENCY):
    """有界流水线：保持最多 min(并发数, 10) 个未确认分块，确认一块立刻补位。

    逐块读取并在工作线程中读盘，内存只保留有限分块；大文件用 InputFileBig，
    小文件仍计算 MD5。任一失败或取消都会清掉其余在途请求。
    """
    if not 1 <= concurrency <= 16:
        raise JobError("上传并发数必须在 1 到 16 之间")
    stat = await asyncio.to_thread(path.stat)
    size = stat.st_size
    if not size:
        raise JobError("不能上传空文件")
    count = (size + PART_SIZE - 1) // PART_SIZE
    big = size > BIG_FILE_THRESHOLD
    file_id = helpers.generate_random_long()
    digest = hashlib.md5()
    pending: set[asyncio.Task] = set()
    latencies: list[float] = []
    width = min(MAX_IN_FLIGHT, concurrency, count)
    started = time.monotonic()

    async def save_part(index: int, chunk: bytes):
        request = (functions.upload.SaveBigFilePartRequest(file_id, index, count, chunk) if big
                   else functions.upload.SaveFilePartRequest(file_id, index, chunk))
        before = time.monotonic()
        if not await call(request):
            raise JobError("Telegram 未确认上传分块")
        latencies.append(time.monotonic() - before)

    try:
        with path.open("rb") as source:
            for index in range(count):
                if len(pending) >= width:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done)
                chunk = await asyncio.to_thread(source.read, PART_SIZE)
                if len(chunk) != min(PART_SIZE, size - index * PART_SIZE):
                    raise JobError("上传过程中本地文件发生变化")
                if not big:
                    digest.update(chunk)
                pending.add(asyncio.create_task(save_part(index, chunk)))
            if pending:
                await asyncio.gather(*pending)
        current = await asyncio.to_thread(path.stat)
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise JobError("上传过程中本地文件发生变化")
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    elapsed = max(time.monotonic() - started, 0.001)
    # 只记录体积、耗时和并发，不输出文件名（可能是中文标题）。
    emit("transfer", bytes=size, seconds=round(elapsed, 3),
         kib_s=round(size / elapsed / 1024, 1), in_flight=width,
         mean_ack_ms=round(sum(latencies) / max(1, len(latencies)) * 1000, 1))
    if big:
        return types.InputFileBig(file_id, count, path.name)
    return types.InputFile(file_id, count, path.name, digest.hexdigest())


def telegram_proxy(url: str):
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"socks5", "socks4", "http"} or not parts.hostname or not parts.port:
            raise ValueError
    except ValueError:
        raise JobError("telegram_proxy 必须是 socks5/socks4/http 代理 URL，包含有效的地址和端口") from None
    try:
        backend = import_module("python_socks.async_.asyncio")
        if not callable(backend.Proxy.create):
            raise ImportError
    except (ImportError, AttributeError):
        raise JobError("Telegram 已配置代理，但缺少或无法加载 python-socks[asyncio] 异步代理依赖。"
                       "请使用当前虚拟环境解释器执行 -m pip install -r requirements.txt，"
                       "然后重启进程；PySocks 不能替代此依赖，不会回退直连") from None
    # Telethon 在导入时缓存可选依赖；运行中安装包不代表现有客户端已启用代理。
    if (getattr(telegrambaseclient, "python_socks", None) is None
            or getattr(telethon_connection, "python_socks", None) is None):
        raise JobError("Telegram 已配置代理，但 Telethon 未加载 python-socks。"
                       "请确认当前虚拟环境已安装 python-socks[asyncio] 并重启 Python 进程；不会回退直连")
    return {"proxy_type": parts.scheme, "addr": parts.hostname, "port": parts.port, "rdns": True,
            "username": unquote(parts.username) if parts.username else None,
            "password": unquote(parts.password) if parts.password else None}


def check_telegram_proxy(url: str) -> dict:
    """只检查代理选择及异步后端，不连接 Telegram、不显示地址或凭据。"""
    proxy = telegram_proxy(url)
    return {"telegram_proxy_enabled": proxy is not None, "telegram_proxy_ready": True,
            "telegram_proxy_backend": "python-socks" if proxy else "direct",
            "telegram_proxy_type": proxy["proxy_type"] if proxy else ""}


class Telegram:
    def __init__(self, config: dict):
        check_crypto()
        self.config = config
        if config["telegram_api_id"] <= 0 or not config["telegram_api_hash"]:
            raise JobError("请先配置 telegram_api_id 和 telegram_api_hash（my.telegram.org）")
        STATE.mkdir(parents=True, exist_ok=True)
        self._make_client()

    def _make_client(self):
        self.client = TelegramClient(
            str(STATE / "telegram"), self.config["telegram_api_id"], self.config["telegram_api_hash"],
            proxy=telegram_proxy(self.config["telegram_proxy"]), flood_sleep_threshold=0,
            raise_last_call_error=True,
            # 不在协议层静默重试：失败交给本次任务如实报告，限流由 FloodGate 统一等待。
            request_retries=0, connection_retries=2, timeout=30,
        )
        self.call = FloodGate(self.client)

    async def login(self, force=False):
        login_status("telegram", "connecting")
        await self.client.connect()
        if force and await self.client.is_user_authorized():
            if not await self.client.log_out():
                raise JobError("Telegram 原会话退出失败")
            self._make_client()
            await self.client.connect()
        if await self.client.is_user_authorized():
            login_status("telegram", "ready")
            return
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            qr = await self.call.run(self.client.qr_login)
            waiter = asyncio.create_task(qr.wait(timeout=30))
            try:
                # 先注册扫码监听，防止用户很快扫码时漏掉登录事件。
                await asyncio.sleep(0)
                show_qr("telegram", qr.url)
                await waiter
                login_status("telegram", "ready")
                return
            except (asyncio.TimeoutError, errors.AuthTokenExpiredError):
                continue
            except errors.SessionPasswordNeededError:
                password = os.environ.get("TG_PASSWORD", "")
                if not password and sys.stdin.isatty():
                    password = await asyncio.to_thread(getpass, "Telegram 两步验证密码：")
                if not password:
                    login_status("telegram", "password_required")
                    raise JobError("账号需要两步验证，请在本地终端登录或设置 TG_PASSWORD；不要在聊天中发送密码")
                await self.call.run(lambda: self.client.sign_in(password=password))
                login_status("telegram", "ready")
                return
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        raise JobError("Telegram 扫码超时，请发送：电报重登")

    async def peer(self):
        channel = self.config["telegram_channel_id"]
        if not channel:
            raise JobError("请在 config.json 中配置 telegram_channel_id，再上传视频")
        try:
            return await self.client.get_input_entity(channel)
        except ValueError:
            async for dialog in self.client.iter_dialogs():
                if dialog.id == channel:
                    return dialog.input_entity
        raise JobError("找不到配置的频道；请确认账号已加入并拥有发布权限")

    async def upload(self, media, tags: list[str]) -> list[int]:
        peer = await self.peer()
        me = await self.call.run(self.client.get_me)
        max_parts = 8000 if getattr(me, "premium", False) else 4000
        if media.video.stat().st_size > max_parts * PART_SIZE:
            raise JobError("视频超过当前 Telegram 账号的单文件上限；夸克上传仍独立进行")
        concurrency = self.config["upload_concurrency"]
        caption, entities = caption_and_tags(media.info.title, tags)
        cover_file = await upload_parts(self.call, media.cover, concurrency)
        photo = await self.call(functions.messages.UploadMediaRequest(
            peer, types.InputMediaUploadedPhoto(file=cover_file)))
        cover = utils.get_input_photo(photo.photo)
        thumb = await upload_parts(self.call, media.thumb, concurrency)

        async def document(path, metadata, *, preview=False):
            file = await upload_parts(self.call, path, concurrency)
            attributes = [types.DocumentAttributeFilename(file_name=path.name),
                          types.DocumentAttributeVideo(
                              duration=metadata["duration"], w=metadata["width"], h=metadata["height"],
                              supports_streaming=True, nosound=True if preview else None)]
            uploaded = types.InputMediaUploadedDocument(
                file=file, mime_type="video/mp4", attributes=attributes,
                nosound_video=True if preview else None, thumb=None if preview else thumb,
                video_cover=None if preview else cover,
            )
            result = await self.call(functions.messages.UploadMediaRequest(peer, uploaded))
            return types.InputMediaDocument(
                id=utils.get_input_document(result.document), video_cover=None if preview else cover)

        preview = await document(media.preview, media.preview_meta, preview=True)
        video = await document(media.video, media.video_meta)
        # 原始 GIF/Animated 文档不能进入视频相册，所以预览以无声视频发送。
        # random_id 让发送结果能按条目对上，避免只凭条数猜成功。
        random_ids = [helpers.generate_random_long(), helpers.generate_random_long()]
        request = functions.messages.SendMultiMediaRequest(peer=peer, multi_media=[
            types.InputSingleMedia(media=preview, message="", random_id=random_ids[0]),
            types.InputSingleMedia(media=video, message=caption, entities=entities,
                                   random_id=random_ids[1]),
        ])
        result = await self.call(request)
        updates = getattr(result, "updates", [])
        mapping = {update.random_id: update.id for update in updates
                   if isinstance(update, types.UpdateMessageID)}
        messages = {update.message.id: update.message for update in updates
                    if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage))}
        if any(random_id not in mapping for random_id in random_ids):
            raise JobError("相册发布结果无法确认；请先检查频道，不要直接重复发布")
        sent = [messages.get(mapping[random_id]) for random_id in random_ids]
        if any(message is None or not message.grouped_id for message in sent) \
                or sent[0].grouped_id != sent[1].grouped_id:
            raise JobError("相册发布结果无法确认；请先检查频道，不要直接重复发布")
        return [message.id for message in sent]

    async def notify(self, text: str) -> None:
        if not await self.client.is_user_authorized():
            raise JobError("Telegram 未登录，通知仅保存在本地结果中")
        await self.call(functions.messages.SendMessageRequest(
            peer=types.InputPeerSelf(), message=text, random_id=helpers.generate_random_long()))

    async def close(self):
        await self.client.disconnect()
