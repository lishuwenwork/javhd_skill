"""Cloudflare D1 REST API 上传记录；不保存本地业务记录，不输出凭据或 SQL 参数。"""

from __future__ import annotations

from pathlib import Path
import re
import threading

import httpx

from common import JobError

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "d1.sql"
METADATA_FIELDS = ("title_zh", "title_en", "detail_url", "cover_url", "preview_url")
EPOCH = 1577836800000  # 2020-01-01，给 Snowflake 的 41 位毫秒时间留足空间。


def validate_config(config, *, required=False):
    for name in ("ACCOUNT_ID", "DATABASE_ID", "API_TOKEN"):
        value = config.get(name, "")
        if not isinstance(value, str) or (value and (value != value.strip() or re.search(r"[\s\x00-\x1f\x7f]", value))):
            raise JobError(f"{name} 必须是无空白字符的字符串，请在本地 config.json 核对")
    if config.get("ACCOUNT_ID") and not re.fullmatch(r"[0-9a-fA-F]{32}", config["ACCOUNT_ID"]):
        raise JobError("ACCOUNT_ID 格式无效，请在本地核对 Cloudflare 账户 ID")
    if config.get("DATABASE_ID") and not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", config["DATABASE_ID"]):
        raise JobError("DATABASE_ID 格式无效，请在本地核对 D1 数据库 UUID")
    instance = config.get("SNOWFLAKE_INSTANCE_ID", 0)
    if type(instance) is not int or not 0 <= instance <= 1023:
        raise JobError("SNOWFLAKE_INSTANCE_ID 必须是 0 到 1023 的整数")
    ready = all(config.get(name) for name in ("ACCOUNT_ID", "DATABASE_ID", "API_TOKEN"))
    if required and not ready:
        raise JobError("请在本地 config.json 填写 ACCOUNT_ID、DATABASE_ID、API_TOKEN 后再执行上传或初始化 D1")
    return bool(ready)


def metadata_from_info(info):
    return {"title_zh": str(getattr(info, "title", "") or ""),
            "title_en": str(getattr(info, "title_en", "") or ""),
            "detail_url": str(getattr(info, "page_url", "") or ""),
            "cover_url": str(getattr(info, "cover_url", "") or ""),
            "preview_url": str(getattr(info, "preview_url", "") or "")}


class UploadRecords:
    def __init__(self, config, *, client=None, generator=None):
        validate_config(config, required=True)
        if generator is None:
            try:
                from snowflake import SnowflakeGenerator
                generator = SnowflakeGenerator(config.get("SNOWFLAKE_INSTANCE_ID", 0), epoch=EPOCH)
            except ImportError:
                raise JobError("缺少 snowflake-id，请更新安装 requirements.txt") from None
        self._generator = generator
        self._id_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._initialized = False
        self._url = ("https://api.cloudflare.com/client/v4/accounts/" + config["ACCOUNT_ID"]
                     + "/d1/database/" + config["DATABASE_ID"] + "/query")
        self._client = client or httpx.Client(timeout=httpx.Timeout(30, connect=15), follow_redirects=False)
        self._own_client = client is None
        self._headers = {"Authorization": "Bearer " + config["API_TOKEN"], "Content-Type": "application/json"}

    def query(self, sql, params=()):
        try:
            response = self._client.post(self._url, headers=self._headers,
                                         json={"sql": sql, "params": [str(value) for value in params]})
        except httpx.TimeoutException:
            raise JobError("D1 API 请求超时，数据库写入结果未确认；不会重新上传媒体") from None
        except httpx.HTTPError:
            raise JobError("D1 API 连接失败，请检查运行环境网络；不会改用本地记录文件") from None
        try:
            payload = response.json()
        except ValueError:
            raise JobError(f"D1 API 响应不可解析（HTTP {response.status_code}），写入未确认") from None
        valid = isinstance(payload, dict)
        items = payload.get("result") if valid else None
        if (response.status_code != 200 or not valid or payload.get("success") is not True
                or payload.get("errors") or not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict) or items[0].get("success") is not True):
            codes = [str(item["code"]) for item in payload.get("errors", [])
                     if isinstance(item, dict) and type(item.get("code")) is int] if valid and isinstance(payload.get("errors"), list) else []
            detail = "，错误码 " + ",".join(codes[:3]) if codes else ""
            raise JobError(f"D1 API 操作失败（HTTP {response.status_code}{detail}），请核对数据库及 D1 写入权限")
        rows = items[0].get("results", [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise JobError("D1 API 返回结果格式异常，写入未确认")
        return rows

    def initialize(self):
        with self._schema_lock:
            if not self._initialized:
                self.query(SCHEMA.read_text(encoding="utf-8"))
                self.query("SELECT id, video_id, " + ", ".join(METADATA_FIELDS)
                           + ", telegram_status, quark_status, telegram_message_ids, quark_file_id FROM video_uploads LIMIT 0")
                self._initialized = True

    def _id(self):
        with self._id_lock:
            try:
                value = next(self._generator)
            except (StopIteration, RuntimeError, ValueError):
                value = None
        if type(value) is not int or not 0 < value < 2 ** 63:
            raise JobError("snowflake-id 生成失败，请核对系统时间和实例编号")
        return str(value)

    def begin(self, video_id, detail_url="", *, record_id="", retry=False):
        if not isinstance(video_id, str) or not re.fullmatch(r"[0-9]+", video_id) or int(video_id) <= 0:
            raise JobError("D1 视频 ID 无效")
        self.initialize()
        if record_id:
            rows = self.query("SELECT id FROM video_uploads WHERE id = ? AND video_id = ?", (record_id, video_id))
            return self._confirmed(rows, record_id)
        if retry:
            rows = self.query("SELECT id FROM video_uploads WHERE video_id = ?", (video_id,))
            if rows:
                return self._confirmed(rows)
        rows = self.query("""INSERT INTO video_uploads (id, video_id, detail_url) VALUES (?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                detail_url = COALESCE(NULLIF(excluded.detail_url, ''), video_uploads.detail_url),
                telegram_status = 0, quark_status = 0, telegram_message_ids = '', quark_file_id = ''
            RETURNING id""", (self._id(), video_id, detail_url))
        return self._confirmed(rows)

    @staticmethod
    def _confirmed(rows, record_id=""):
        if len(rows) != 1 or not isinstance(rows[0].get("id"), str) \
                or not re.fullmatch(r"[0-9]+", rows[0]["id"]) or (record_id and rows[0]["id"] != record_id):
            raise JobError("D1 未返回匹配的记录 ID，数据库操作未确认")
        return rows[0]["id"]

    def metadata(self, record_id, metadata):
        values = [str(metadata.get(name) or "") for name in METADATA_FIELDS]
        assignments = ", ".join(name + " = COALESCE(NULLIF(?, ''), " + name + ")" for name in METADATA_FIELDS)
        rows = self.query("UPDATE video_uploads SET " + assignments + " WHERE id = ? RETURNING id", (*values, record_id))
        self._confirmed(rows, record_id)

    def platform(self, record_id, service, success, remote_ids):
        if service == "telegram":
            if not isinstance(remote_ids, list) or not all(type(value) is int and value > 0 for value in remote_ids):
                raise JobError("电报正片消息 ID 无效，未写入 D1")
            if success and not remote_ids:
                raise JobError("电报没有正片消息 ID，不能标记 D1 上传成功")
            field, value = "telegram_message_ids", ",".join(map(str, remote_ids))
        elif service == "quark":
            if not isinstance(remote_ids, str) or (success and not remote_ids):
                raise JobError("夸克文件 ID 无效，不能标记 D1 上传成功")
            field, value = "quark_file_id", remote_ids
        else:
            raise JobError("D1 上传平台无效")
        rows = self.query(f"UPDATE video_uploads SET {service}_status = ?, {field} = ? WHERE id = ? RETURNING id",
                          (int(bool(success)), value, record_id))
        self._confirmed(rows, record_id)

    def close(self):
        if self._own_client:
            self._client.close()
