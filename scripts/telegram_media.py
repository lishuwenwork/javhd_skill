"""仅为电报生成限额内的独立 MP4；不改写、移动或删除原视频。"""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from contextlib import asynccontextmanager
from pathlib import Path
import re
import tempfile

import imageio_ffmpeg

from common import JobError, emit
from source import ffmpeg, media_metadata

# 普通账号采用 4000 个 512 KiB 分块；会员采用 8000 个分块并跳过切割。
MAX_VIDEO_SIZE = 4000 * 512 * 1024
PREMIUM_MAX_VIDEO_SIZE = 8000 * 512 * 1024


def _tail_split_points(keyframes: list[tuple[float, int]], total: int, byte_budget: int) -> list[float]:
    """从结尾向前尽量填满每段，余量留在开头；返回时间升序的切点。"""
    points = []
    end = total
    upper = len(keyframes)
    while end > byte_budget and upper:
        index = bisect_left(keyframes, end - byte_budget, hi=upper, key=lambda frame: frame[1])
        # 单个关键帧区间可能大于预留预算，最终仍由实际 MP4 大小检查决定是否可用。
        index = min(index, upper - 1)
        timestamp, offset = keyframes[index]
        points.append(timestamp)
        end = offset
        upper = index
    return list(reversed(points))


async def split_points(video: Path, byte_budget: int, *, direction: str = "tail") -> list[float]:
    """按压缩包字节规划切点；从尾规划只保存关键帧累计位置，不缓存全部包或视频。"""
    if direction not in ("head", "tail"):
        raise JobError("电报分段方向必须是 head 或 tail")
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-v", "error",
        "-copyts", "-start_at_zero", "-i", str(video), "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-f", "framecrc", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    points = []
    time_base = None
    payload = 0
    last_boundary = 0.0
    candidate = None
    keyframes = []
    video_packets = 0
    try:
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("#"):
                match = re.fullmatch(r"#tb\s+0:\s*(\d+)/(\d+)", line)
                if match:
                    time_base = int(match[1]) / int(match[2])
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) < 6:
                raise JobError("无法读取电报分段所需的媒体包信息")
            stream, size = int(fields[0]), int(fields[4])
            if size < 0:
                raise JobError("无法读取电报分段所需的媒体包信息")
            if stream == 0:
                video_packets += 1
                if time_base is None:
                    raise JobError("视频缺少电报分段所需的时间基准")
                # framecrc 省略 F 字段时 flags=KEY；音频包不作为视频切点。
                flags = next((int(field[2:], 0) for field in fields[6:] if field.startswith("F=")), 1)
                if flags & 1:
                    timestamp = int(fields[2]) * time_base
                    if direction == "tail":
                        if video_packets > 1 and timestamp > 0:
                            keyframes.append((timestamp, payload))
                    elif timestamp > last_boundary:
                        candidate = (timestamp, payload)
            # 同时计算音频，并为每个样本的 MP4 索引预留空间。
            payload += size + 32
            if direction == "head" and payload > byte_budget and candidate is not None:
                timestamp, before = candidate
                if timestamp > last_boundary:
                    points.append(timestamp)
                    payload -= before
                    last_boundary = timestamp
                    candidate = None
        await process.wait()
        if process.returncode or not video_packets:
            raise JobError("无法分析视频关键帧，未开始电报发布")
    except BaseException as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if isinstance(exc, (ValueError, ZeroDivisionError)):
            raise JobError("无法读取电报分段所需的时间戳或媒体包信息") from None
        raise
    return _tail_split_points(keyframes, payload, byte_budget) if direction == "tail" else points


@asynccontextmanager
async def _split_directory(parent: Path, retain: bool):
    if retain:
        yield Path(tempfile.mkdtemp(prefix="telegram-parts-", dir=parent))
    else:
        with tempfile.TemporaryDirectory(prefix="telegram-parts-", dir=parent) as directory:
            yield Path(directory)


@asynccontextmanager
async def telegram_videos(media, *, max_bytes: int = MAX_VIDEO_SIZE, premium: bool = False,
                          direction: str = "tail", retain: bool = False):
    """上传期间持有临时分段；退出上下文前，调用方必须结束所有文件读取。"""
    video = media.video
    if direction not in ("head", "tail"):
        raise JobError("电报分段方向必须是 head 或 tail")
    original = await asyncio.to_thread(video.stat)
    if max_bytes <= 0 or not original.st_size:
        raise JobError("电报视频或分段上限无效")
    if premium:
        if original.st_size > PREMIUM_MAX_VIDEO_SIZE:
            raise JobError("视频超过 Telegram 会员单文件兼容上限（约 4GB）；会员模式不切割，夸克上传仍独立进行")
        yield [(video, media.video_meta)]
        return
    if original.st_size <= max_bytes:
        yield [(video, media.video_meta)]
        return

    # 主任务持有时不在这里清理；两端成功或用户选择删除后由主任务统一清理。
    async with _split_directory(video.parent, retain) as temporary:
        directory = Path(temporary).resolve()
        if directory.parent != video.parent.resolve():
            raise JobError("电报分段目录不在本次视频目录内")
        # 实际 MP4 开销可能超过估计，最多缩小预算重做两次，不重新编码或上传超限文件。
        for attempt, ratio in enumerate((0.98, 0.90, 0.80), 1):
            budget = max(1, int(max_bytes * ratio) - min(1024 * 1024, max_bytes // 20))
            points = await split_points(video, budget, direction=direction)
            output = directory / f"attempt-{attempt}"
            output.mkdir()
            pattern = output / "part-%06d.mp4"
            arguments = ["-hide_banner", "-copyts", "-start_at_zero", "-i", video,
                         "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-map_chapters", "-1"]
            if points:
                await ffmpeg(
                    *arguments, "-f", "segment", "-segment_times", ",".join(f"{point:.9f}" for point in points),
                    "-segment_time_delta", "0.000001", "-reference_stream", "v:0", "-reset_timestamps", "1",
                    "-segment_start_number", "1", "-segment_format", "mp4",
                    "-segment_format_options", "movflags=+faststart", pattern,
                )
            else:
                # 原文件可能有大量容器填充；重新封装后一个文件就可能低于上限。
                await ffmpeg(*arguments, "-movflags", "+faststart", output / "part-000001.mp4")
            paths = sorted(output.glob("part-*.mp4"))
            if len(paths) != len(points) + 1 or any(not path.stat().st_size for path in paths):
                raise JobError("电报视频分段不完整，未开始发布")
            if any(path.stat().st_size > max_bytes for path in paths):
                if not retain:
                    for path in paths:
                        path.unlink()
                    output.rmdir()
                continue
            parts = []
            for index, path in enumerate(paths, 1):
                target = directory / f"{video.stem}({index}).mp4"
                path.rename(target)
                metadata = await asyncio.to_thread(media_metadata, target)
                parts.append((target, metadata))
            current = await asyncio.to_thread(video.stat)
            if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                raise JobError("电报分段期间原视频发生变化，未开始发布")
            emit("telegram_split", parts=len(parts), bytes=original.st_size, size_limit=max_bytes,
                 direction=direction)
            yield parts
            return
        raise JobError("视频关键帧间隔或容器开销过大，无法生成不超限的电报分段；夸克上传仍独立进行")
