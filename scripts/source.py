"""解析当前视频、流式下载、准备封面和可用于相册的动态预览。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
import httpx
import imageio_ffmpeg
from PIL import Image, ImageOps

from common import JobError, ROOT, detail_page_url

SITE = "https://javhd.com"
HAN = re.compile(r"[㐀-鿿]")
KANA = re.compile(r"[぀-ヿ]")


@dataclass
class VideoInfo:
    video_id: str
    title: str
    page_url: str
    cover_url: str
    preview_url: str
    video_url: str
    expected_duration: float = 0
    title_en: str = ""


@dataclass
class MediaFiles:
    info: VideoInfo
    video: Path
    cover: Path
    thumb: Path
    preview: Path
    video_meta: dict
    preview_meta: dict


def web_url(value, base: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    url = urljoin(base, value.strip().replace("\\/", "/"))
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return ""
    return url


def video_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from video_objects(item)
    elif isinstance(value, dict):
        if "VideoObject" in ([value.get("@type")] if isinstance(value.get("@type"), str)
                             else value.get("@type") or []):
            yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from video_objects(item)


def inspect_detail(html: str, page: str, video_id: str) -> dict:
    """独立提取各项元数据，便于诊断部分成功；不将预告片视为完整源。"""
    soup = BeautifulSoup(html, "html.parser")
    player = soup.select_one("player-component[content-path]")
    endpoint = web_url(player.get("content-path"), page) if player else ""
    player_error = ""
    if player is None:
        player_error = "详情页没有播放器；请检查登录状态或视频是否已下架"
    elif (not endpoint or urlsplit(endpoint).netloc != urlsplit(page).netloc
          or not re.fullmatch(r"/[a-z]{2}/(?:player_api|player/\d+)/?", urlsplit(endpoint).path)):
        player_error = "无法识别网站提供的播放接口"
        endpoint = ""
    trailer = parse_qs(urlsplit(endpoint).query).get("is_trailer", [""])[0].lower() in {"1", "true"}

    structured, durations = [], []
    for node in soup.select('script[type="application/ld+json"]'):
        text = node.get_text()
        durations.extend(re.findall(r'"duration"\s*:\s*"(P?T[0-9HMS.]+)"', text))
        try:
            structured.extend(video_objects(json.loads(text)))
        except (ValueError, TypeError):
            continue
    data = structured[0] if structured else {}
    h1 = soup.select_one("h1")
    meta_title = soup.select_one('meta[property="og:title"]')
    titles = [h1.get_text(" ", strip=True) if h1 else "", data.get("name", ""),
              meta_title.get("content", "") if meta_title else ""]
    title = next((re.sub(r"\s+", " ", str(value)).strip() for value in titles
                  if HAN.search(str(value)) and not KANA.search(str(value))), "")
    title_en = next((english_title(value) for value in titles if english_title(value)), "")

    meta_cover = soup.select_one('meta[property="og:image"]')
    cover = (meta_cover.get("content", "") if meta_cover else "") or data.get("thumbnailUrl", "")
    if isinstance(cover, list):
        cover = cover[0] if cover else ""
    if isinstance(cover, dict):
        cover = cover.get("url", "")
    cover = cover or (player.get("poster", "") or player.get("url-thumb", "") if player else "")
    if not cover:
        image = next((n for n in soup.select("img[src]")
                      if not n.find_parent(["thumb-component", "header", "footer", "nav"])), None)
        cover = image.get("src", "") if image else ""

    preview = ((player.get("video-preview") or player.get("gif") or "") if player else "")
    preview = preview or data.get("previewUrl") or ""
    # 不能拿详情页中推荐视频的动态图冒充当前视频。
    for card in soup.select("thumb-component[video-preview]"):
        link = web_url(card.get("link-content"), page)
        if link == page or re.search(rf"/id/{re.escape(video_id)}(?:/|$)", urlsplit(link).path):
            preview = preview or card.get("video-preview", "")
    # 页面 JSON-LD 可能整体有语法错误，但唯一的 duration 仍可独立读取。
    duration = str(data.get("duration") or (durations[0] if len(set(durations)) == 1 else ""))
    match = re.fullmatch(r"P?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", duration)
    seconds = sum(float(n or 0) * unit for n, unit in zip(match.groups(), (3600, 60, 1))) if match else 0
    return {"title": title, "title_en": title_en, "cover_url": web_url(cover, page), "preview_url": web_url(preview, page),
            "expected_duration": seconds, "player_endpoint": endpoint, "player_error": player_error,
            "is_trailer": trailer, "player_role": player.get("role", "") if player else ""}


def english_title(value) -> str:
    if not isinstance(value, str) or HAN.search(value) or KANA.search(value) or not re.search(r"[A-Za-z]", value):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_detail(html: str, page: str, video_id: str) -> tuple[dict, str]:
    data = inspect_detail(html, page, video_id)
    if data["player_error"]:
        raise JobError(data["player_error"])
    if not data["title"]:
        raise JobError("该中文详情页没有提供中文标题；未擅自翻译或使用外文标题")
    if not data["cover_url"]:
        raise JobError("详情页没有提供当前视频的封面")
    fields = {key: data[key] for key in ("title", "title_en", "cover_url", "preview_url", "expected_duration")}
    return fields, data["player_endpoint"]


def full_player_endpoint(endpoint: str) -> str:
    return player_mode_endpoint(endpoint, trailer=False)


def player_mode_endpoint(endpoint: str, *, trailer: bool) -> str:
    # 保留页面提供的内部 videoId、路由和其他参数，仅切换完整源/预告模式。
    parts = urlsplit(endpoint)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "is_trailer"]
    query.append(("is_trailer", "1" if trailer else "0"))
    return parts._replace(query=urlencode(query)).geturl()


async def load_playback(client: httpx.AsyncClient, endpoint: str, page: str) -> tuple[dict, str]:
    body, final = await small_response(client, full_player_endpoint(endpoint), ajax=True)
    if urlsplit(final).netloc != urlsplit(page).netloc:
        raise JobError("播放接口跳转到了其他站点")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise JobError("播放接口返回格式不正确")
    return data, final


def best_source(data: dict, base: str) -> str:
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        raise JobError("播放接口未返回视频 sources")
    candidates = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        url = web_url(item.get("src"), base)
        path = urlsplit(url).path.lower()
        if (not url or not path.endswith(".mp4") or path.endswith("/black_cap.mp4")
                or re.search(r"/trailers?/", path)):
            continue
        resolution = re.search(r"\d+", str(item.get("res") or item.get("label") or ""))
        candidates.append((int(resolution[0]) if resolution else 0, url))
    if not candidates:
        raise JobError("播放接口没有可用完整 MP4，可能仅返回了预告片、占位视频或非 MP4 源")
    return max(candidates, key=lambda item: item[0])[1]


async def resolve_preview(client: httpx.AsyncClient, video_id: str, page: str,
                          preview: str = "", playback: dict | None = None, *, player_endpoint: str = "") -> str:
    if preview:
        return preview
    for key in ("gif", "gif_url", "previewUrl", "video-preview"):
        preview = web_url((playback or {}).get(key), page)
        if preview:
            return preview
    # 普通页面可按公开 ID 定位；工作室的公开 ID、播放器 ID、媒体目录 ID 可能各不相同。
    studio = re.search(r"/studio(?:/|$)", urlsplit(page).path, flags=re.I)
    if not studio and re.fullmatch(r"[0-9]+", video_id) and int(video_id) > 0:
        candidate = f"https://c4.cdnjhd.com/share2/trailers/{video_id}/5sec_1000_n.mp4"
        if await checked_preview(client, candidate, page):
            return candidate
    try:
        endpoint = urlsplit(player_endpoint)
    except ValueError:
        return ""
    if (endpoint.netloc != urlsplit(page).netloc or endpoint.scheme not in {"http", "https"}
            or endpoint.username or endpoint.password
            or not re.fullmatch(r"/[a-z]{2}/(?:player_api|player/\d+)/?", endpoint.path)):
        return ""
    try:
        body, final = await small_response(client, player_mode_endpoint(player_endpoint, trailer=True), ajax=True)
        if urlsplit(final).netloc != urlsplit(page).netloc:
            return ""
        data = json.loads(body)
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, list):
            return ""
    except (httpx.HTTPError, JobError, ValueError):
        return ""
    short, supplied = [], []
    for item in sources:
        if not isinstance(item, dict):
            continue
        try:
            candidate = web_url(item.get("src"), final)
            parts = urlsplit(candidate)
        except ValueError:
            continue
        # 仅从当前视频的预告接口取目录；完整源即使接口忽略 is_trailer 也不能冒充预览。
        match = re.fullmatch(r"(.*/trailers?/[0-9]+)/[^/]+\.mp4", parts.path, flags=re.I)
        if not candidate or not match:
            continue
        short.append(parts._replace(path=match[1] + "/5sec_1000_n.mp4").geturl())
        supplied.append(candidate)
        if len(supplied) == 3:
            break
    # 优先短预览，其次接口提供的预告；候选去重，每次只读 MP4 文件头。
    for candidate in dict.fromkeys([*short, *supplied]):
        if await checked_preview(client, candidate, page):
            return candidate
    return ""


async def checked_preview(client: httpx.AsyncClient, candidate: str, page: str) -> bool:
    """只读前 64 字节；CDN 忽略 Range 时也不继续读取整段预览。"""
    try:
        async with client.stream("GET", candidate, headers={
            "Range": "bytes=0-63", "Referer": page, "Accept-Encoding": "identity",
        }) as response:
            response.raise_for_status()
            if response.status_code not in (200, 206):
                return False
            prefix = bytearray()
            async for chunk in response.aiter_raw(64):
                prefix.extend(chunk[:64 - len(prefix)])
                if len(prefix) == 64:
                    break
            if prefix[4:8] == b"ftyp":
                return True
    except httpx.HTTPError:
        pass
    return False


def mp4_duration(data: bytes) -> float:
    """读取 moov/mvhd 时长；支持文件头或文件尾的一段数据，不扫描媒体帧。"""
    for match in re.finditer(b"moov", data):
        start = match.start() - 4
        if start < 0:
            continue
        size = int.from_bytes(data[start:start + 4], "big")
        header = 8
        if size == 1:
            if start + 16 > len(data):
                continue
            size = int.from_bytes(data[start + 8:start + 16], "big")
            header = 16
        if size and size < header:
            continue
        end = min(start + size, len(data)) if size else len(data)
        position = start + header
        while position + 8 <= end:
            length = int.from_bytes(data[position:position + 4], "big")
            kind = data[position + 4:position + 8]
            if length < 8:
                break
            if kind == b"mvhd" and position + length <= end:
                if length < 12:
                    break
                payload = position + 8
                version = data[payload]
                if version not in (0, 1):
                    break
                offset = payload + (12 if version == 0 else 20)
                width = 4 if version == 0 else 8
                if offset + 4 + width > position + length:
                    break
                scale = int.from_bytes(data[offset:offset + 4], "big")
                duration = int.from_bytes(data[offset + 4:offset + 4 + width], "big")
                if scale and duration not in (0, (1 << (width * 8)) - 1):
                    return duration / scale
                break
            position += length
    return 0


async def inspect_video_url(client: httpx.AsyncClient, url: str, referer: str) -> dict:
    """限量读取头尾：最多 64 KiB + 512 KiB，不下载整段视频。"""
    async def read_range(value, limit):
        async with client.stream("GET", url, headers={
            "Range": value, "Referer": referer, "Accept-Encoding": "identity",
        }) as response:
            response.raise_for_status()
            if response.status_code not in (200, 206):
                raise JobError("视频响应不是普通文件响应")
            size = response.headers.get("content-length", "")
            match = re.fullmatch(r"bytes \d+-\d+/(\d+)", response.headers.get("content-range", ""))
            total = int(match[1]) if match else (int(size) if size.isdigit() and response.status_code == 200 else 0)
            content = bytearray()
            async for chunk in response.aiter_raw(min(limit, 65536)):
                content.extend(chunk[:limit - len(content)])
                if len(content) == limit:
                    break
            return bytes(content), {"http_status": response.status_code,
                                    "content_type": response.headers.get("content-type", ""),
                                    "total_bytes": total, "range_supported": response.status_code == 206}

    head, result = await read_range("bytes=0-65535", 65536)
    if head[4:8] != b"ftyp":
        raise JobError("视频链接没有返回可识别的 MP4 文件头")
    seconds = mp4_duration(head)
    result["bytes_read"] = len(head)
    if not seconds:
        tail, _ = await read_range("bytes=-524288", 524288)
        result["bytes_read"] += len(tail)
        seconds = mp4_duration(tail)
    result["duration_seconds"] = seconds or None
    return result


async def small_response(client: httpx.AsyncClient, url: str, *, ajax=False) -> tuple[bytes, str]:
    headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else {}
    async with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 10 * 1024 * 1024:
                raise JobError("网页或接口响应过大")
        return bytes(chunks), str(response.url)


async def resolve(client: httpx.AsyncClient, video_id: str, *, page_url: str = "") -> VideoInfo:
    try:
        requested = detail_page_url(page_url) if page_url else f"{SITE}/zh/id/{video_id}"
    except ValueError:
        raise JobError("详情页 URL 无效，请提供 JAVHD 的完整 HTTP(S) 详情页地址") from None
    body, page = await small_response(client, requested)
    if not (urlsplit(page).hostname == "javhd.com" or (urlsplit(page).hostname or "").endswith(".javhd.com")):
        raise JobError("详情页跳转到了其他站点")
    fields, endpoint = parse_detail(body.decode("utf-8", errors="replace"), page, video_id)
    data, final = await load_playback(client, endpoint, page)
    fields["title_en"] = english_title(data.get("title")) or fields["title_en"]
    video_url = best_source(data, final)
    fields["preview_url"] = await resolve_preview(client, video_id, page, fields["preview_url"], data,
                                                   player_endpoint=endpoint)
    return VideoInfo(video_id, page_url=page, video_url=video_url, **fields)


async def download(client: httpx.AsyncClient, url: str, path: Path, *, referer: str,
                   limit: int = 0, video: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=False)
    partial = path.with_suffix(path.suffix + ".part")
    try:
        async with client.stream("GET", url, headers={"Referer": referer, "Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            mime = response.headers.get("content-type", "").lower()
            if video and (mime.startswith(("text/", "application/json")) or "mpegurl" in mime):
                raise JobError("视频链接返回的不是完整视频文件")
            length = response.headers.get("content-length", "")
            declared = int(length) if length.isdigit() else 0
            if limit and declared > limit:
                raise JobError("封面或动态预览超过大小上限")
            size = 0
            with partial.open("wb") as output:
                async for chunk in response.aiter_bytes(512 * 1024):
                    size += len(chunk)
                    if limit and size > limit:
                        raise JobError("封面或动态预览超过大小上限")
                    output.write(chunk)
            if not size or (declared and not response.headers.get("content-encoding") and size != declared):
                raise JobError("下载文件不完整")
        partial.replace(path)
        return path
    finally:
        partial.unlink(missing_ok=True)


def media_metadata(path: Path) -> dict:
    parser = createParser(str(path))
    if parser is None:
        raise JobError("无法识别下载的视频")
    try:
        metadata = extractMetadata(parser)
        if metadata is None:
            raise JobError("无法读取视频信息")
        duration = metadata.get("duration", None)
        result = {"width": metadata.get("width", 0), "height": metadata.get("height", 0),
                  "duration": duration.total_seconds() if duration else 0}
        if not all(result.values()):
            raise JobError("视频缺少宽高或时长，可能没有完整下载")
        return result
    finally:
        parser.close()


async def ffmpeg(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-y", *map(str, args),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, _ = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise JobError("视频完整性检查或动态预览转换失败")


def prepare_cover(source: Path) -> tuple[Path, Path]:
    cover, thumb = source.with_name("cover.jpg"), source.with_name("thumb.jpg")
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1280, 1280))
        image.save(cover, "JPEG", quality=88)
        image.thumbnail((320, 320))
        image.save(thumb, "JPEG", quality=75)
    return cover, thumb


def chinese_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:70].rstrip(" .")
    if not name:
        raise JobError("中文标题无法用作文件名")
    return name + ".mp4"


def website_client(config: dict) -> httpx.AsyncClient:
    cookies = None
    if config["javhd_cookies_file"]:
        path = Path(config["javhd_cookies_file"]).expanduser()
        path = path if path.is_absolute() else ROOT / path
        cookies = MozillaCookieJar(str(path))
        cookies.load(ignore_discard=True, ignore_expires=False)
    return httpx.AsyncClient(
        cookies=cookies, proxy=config["http_proxy"] or None, follow_redirects=True,
        timeout=httpx.Timeout(90, connect=20), headers={"User-Agent": "Mozilla/5.0"},
    )


async def prepare_media(video_id: str, work: Path, config: dict, *, page_url: str = "", on_resolved=None) -> MediaFiles:
    async with website_client(config) as client:
        info = await resolve(client, video_id, **({"page_url": page_url} if page_url else {}))
        if on_resolved is not None:
            await on_resolved(info)
        video = await download(client, info.video_url, work / "video" / chinese_filename(info.title),
                               referer=info.page_url, video=True)
        # 无 Content-Length 时也检查容器是否截断；不对整个视频重新编码。
        await ffmpeg("-xerror", "-err_detect", "explode", "-i", video, "-map", "0:v:0",
                     "-c", "copy", "-f", "null", "-")
        meta = await asyncio.to_thread(media_metadata, video)
        # 页面标注时长不可靠；完整性由源类型、下载长度及容器检查确认。
        cover_source = await download(client, info.cover_url, work / "cover" / "source.img",
                                      referer=info.page_url, limit=20 * 1024 * 1024)
        cover, thumb = await asyncio.to_thread(prepare_cover, cover_source)
        if info.preview_url:
            preview_source = await download(client, info.preview_url, work / "preview" / "source.bin",
                                            referer=info.page_url, limit=100 * 1024 * 1024)
        else:
            # 站点未给当前视频的动态图时，只使用同一视频生成预览。
            directory = work / "preview"
            directory.mkdir()
            preview_source = directory / "preview.gif"
            await ffmpeg("-i", video, "-t", "5", "-an", "-vf", "fps=8,scale=480:-2", preview_source)
        preview = preview_source.with_name("preview.mp4")
        # 不添加 DocumentAttributeAnimated：真正的 Telegram GIF 动画不能组成视频相册。
        await ffmpeg("-i", preview_source, "-t", "10", "-an", "-vf", "fps=12,scale=480:-2",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", preview)
        preview_meta = await asyncio.to_thread(media_metadata, preview)
    return MediaFiles(info, video, cover, thumb, preview, meta, preview_meta)
