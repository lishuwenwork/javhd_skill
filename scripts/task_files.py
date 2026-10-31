"""保留上传任务及安全的重试/删除范围；清单不保存登录信息或签名 URL。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading

from common import JobError, detail_page_url, parse_tags, save_json

MANIFEST = "upload-task.json"
MEDIA_FIELDS = ("video", "cover", "thumb", "preview")
SERVICES = ("telegram", "quark")


def task_directory(root: Path, directory: Path) -> Path:
    """仅允许工作区 downloads 下一个真实的直接子目录，拒绝根目录和链接跳转。"""
    root = root.resolve()
    downloads = (root / "downloads").resolve()
    original = Path(directory).expanduser()
    if not original.is_absolute():
        original = root / original
    attributes = getattr(original.lstat(), "st_file_attributes", 0)
    if original.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) \
            or getattr(original, "is_junction", lambda: False)():
        raise JobError("任务目录不能是符号链接或目录联接")
    target = original.resolve()
    if downloads == root or not downloads.is_relative_to(root) or target.parent != downloads or not target.is_dir():
        raise JobError("只能处理当前 skill 的 downloads 下的单个保留任务目录")
    return target


@dataclass
class TaskFiles:
    directory: Path
    data: dict
    _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def create(cls, root: Path, video_id: str, tags: list[str], channel_id, *, page_url: str = ""):
        if not isinstance(video_id, str) or not re.fullmatch(r"[0-9]+", video_id) or int(video_id) == 0:
            raise JobError("任务视频 ID 无效")
        if page_url:
            detail_page_url(page_url)
        downloads = root.resolve() / "downloads"
        if not downloads.resolve().is_relative_to(root.resolve()):
            raise JobError("下载目录不在当前 skill 内")
        downloads.mkdir(exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=f"{video_id}-", dir=downloads)).resolve()
        return cls(directory, {"version": 1, "id": video_id, "tags": list(tags), "page_url": page_url,
                               "telegram_channel_id": channel_id, "state": "running", "media": None,
                               "statuses": {name: "failed" for name in SERVICES}, "remote_ids": {},
                               "quark_upload": {"version": 1, "attempted": False}})

    @classmethod
    def load(cls, root: Path, directory: Path, *, retry=False):
        directory = task_directory(root, directory)
        try:
            data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
            valid_id = isinstance(data.get("id"), str) and re.fullmatch(r"[0-9]+", data["id"]) and int(data["id"]) > 0
            statuses = data.get("statuses", {})
            if data.get("version") != 1 or not valid_id or not isinstance(statuses, dict) or set(statuses) != set(SERVICES) \
                    or any(status not in ("success", "failed") for status in statuses.values()):
                raise ValueError
            if not isinstance(data.get("tags"), list) or not all(isinstance(tag, str) for tag in data["tags"]):
                raise ValueError
            if parse_tags(" ".join(data["tags"])) != data["tags"]:
                raise ValueError
            if not isinstance(data.get("page_url", ""), str):
                raise ValueError
            if data.get("page_url"):
                detail_page_url(data["page_url"])
            if "d1_record_id" in data and (not isinstance(data["d1_record_id"], str)
                                           or not re.fullmatch(r"[0-9]+", data["d1_record_id"])):
                raise ValueError
            if "video_info" in data:
                from records import METADATA_FIELDS
                if not isinstance(data["video_info"], dict) or set(data["video_info"]) != set(METADATA_FIELDS) \
                        or not all(isinstance(value, str) for value in data["video_info"].values()):
                    raise ValueError
            if "telegram_video_message_ids" in data and (not isinstance(data["telegram_video_message_ids"], list)
                    or not all(type(value) is int and value > 0 for value in data["telegram_video_message_ids"])):
                raise ValueError
            remote_ids = data.get("remote_ids", {})
            if not isinstance(remote_ids, dict) or not set(remote_ids).issubset(SERVICES):
                raise ValueError
            for name in SERVICES:
                if statuses[name] == "success":
                    remote = remote_ids.get(name)
                    if name == "telegram":
                        if not isinstance(remote, list) or not remote or not all(type(value) is int and value > 0 for value in remote):
                            raise ValueError
                    elif not isinstance(remote, str) or not remote:
                        raise ValueError
            if data.get("state") not in ("running", "retained", "complete"):
                raise ValueError
            if data["state"] == "complete" and any(status != "success" for status in statuses.values()):
                raise ValueError
            if "quark_upload" in data:
                from quark import validate_checkpoint
                validate_checkpoint(data["quark_upload"])
        except (OSError, ValueError, TypeError, AttributeError, JobError):
            raise JobError("保留任务清单缺失或格式异常，未执行重试或删除") from None
        if retry and data["state"] not in ("retained", "complete"):
            raise JobError("上次任务未保存完整结果；请先核查两端上传结果，不能直接重试")
        return cls(directory, data)

    def capture_media(self, media):
        if hasattr(media, "info"):
            self.capture_info(media.info)
        if not all(hasattr(media, name) for name in (*MEDIA_FIELDS, "info", "video_meta", "preview_meta")):
            self.data["media"] = None
            return
        files = {}
        for name in MEDIA_FIELDS:
            path = Path(getattr(media, name)).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file():
                raise JobError("媒体文件不在本次任务目录内")
            stat = path.stat()
            files[name] = {"path": path.relative_to(self.directory).as_posix(),
                           "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        self.data["media"] = {"title": media.info.title, "files": files,
                              "video_meta": media.video_meta, "preview_meta": media.preview_meta}

    def capture_info(self, info):
        from records import metadata_from_info
        with self._lock:
            self.data["video_info"] = metadata_from_info(info)

    def restore_media(self):
        saved = self.data.get("media")
        if saved is None:
            return None
        from source import MediaFiles, VideoInfo

        try:
            files = {}
            for name in MEDIA_FIELDS:
                item = saved["files"][name]
                relative = Path(item["path"])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise ValueError
                path = (self.directory / relative).resolve()
                if not path.is_relative_to(self.directory) or not path.is_file():
                    raise ValueError
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                    raise ValueError
                files[name] = path
            if not isinstance(saved["title"], str) or not saved["title"]:
                raise ValueError
            for key in ("video_meta", "preview_meta"):
                if not isinstance(saved[key], dict) or not all(
                    type(saved[key].get(field)) in (int, float) and saved[key][field] > 0
                    for field in ("duration", "width", "height")
                ):
                    raise ValueError
            metadata = self.data.get("video_info", {})
            info = VideoInfo(self.data["id"], saved["title"], metadata.get("detail_url", ""),
                             metadata.get("cover_url", ""), metadata.get("preview_url", ""), "",
                             title_en=metadata.get("title_en", ""))
            return MediaFiles(info=info, **files, video_meta=saved["video_meta"], preview_meta=saved["preview_meta"])
        except (OSError, ValueError, TypeError, KeyError):
            raise JobError("保留的媒体文件缺失、发生变化或路径无效；未重新上传，请先核对任务目录") from None

    def save(self, statuses: dict, result: dict, *, state: str):
        with self._lock:
            self.data["state"] = state
            self.data["statuses"] = dict(statuses)
            self.data["remote_ids"] = {name: result[name + "_remote_ids"] for name in SERVICES
                                       if name + "_remote_ids" in result}
            if "telegram_video_message_ids" in result:
                self.data["telegram_video_message_ids"] = list(result["telegram_video_message_ids"])
            save_json(self.directory / MANIFEST, self.data)

    def save_quark_checkpoint(self, checkpoint: dict):
        # CLI stdout 回调在工作线程，电报结果在事件循环；序列化整个清单更新，避免覆盖彼此。
        from quark import validate_checkpoint
        validate_checkpoint(checkpoint)
        with self._lock:
            self.data["quark_upload"] = deepcopy(checkpoint)
            save_json(self.directory / MANIFEST, self.data)

    def delete(self, root: Path):
        # Windows 递归删除前重新解析并验证目标始终位于明确的任务范围内。
        target = task_directory(root, self.directory)
        try:
            shutil.rmtree(target)
        except Exception:
            if target.is_dir():
                try:
                    save_json(target / MANIFEST, self.data)
                except OSError:
                    pass
            raise
