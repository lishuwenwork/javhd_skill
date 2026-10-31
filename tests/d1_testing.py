"""用真实 SQLite 执行 D1 SQL，以 MockTransport 替代 Cloudflare 网络。"""

from copy import deepcopy
import json
import sqlite3
import threading
from unittest.mock import patch

import httpx

from records import UploadRecords

CONFIG = {"ACCOUNT_ID": "a" * 32, "DATABASE_ID": "11111111-2222-3333-4444-555555555555",
          "API_TOKEN": "offline-test-token", "SNOWFLAKE_INSTANCE_ID": 1}


class Capture:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.events = []
        self.requests = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.records = UploadRecords(CONFIG, client=self.client)
        self.records.initialize()

    def respond(self, request):
        assert request.url.host == "api.cloudflare.com"
        assert request.headers["Authorization"] == "Bearer " + CONFIG["API_TOKEN"]
        body = json.loads(request.content)
        with self.lock:
            self.requests.append(body)
            cursor = self.connection.execute(body["sql"], body["params"])
            rows = [dict(row) for row in cursor.fetchall()]
        return httpx.Response(200, json={"success": True, "errors": [],
                                       "result": [{"success": True, "results": rows}]})

    def emit(self, event, **data):
        self.events.append({"event": event, **deepcopy(data)})

    def result(self, video_id=None, event="result"):
        values = [value for value in self.events if value["event"] == event
                  and (video_id is None or value.get("id") == video_id)]
        if not values:
            raise AssertionError("缺少本次进程结果事件")
        return values[-1]

    def rows(self):
        with self.lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM video_uploads ORDER BY id")]

    def row(self, video_id):
        with self.lock:
            value = self.connection.execute("SELECT * FROM video_uploads WHERE video_id=?", (video_id,)).fetchone()
        return dict(value) if value else None

    def close(self):
        self.client.close()
        self.connection.close()


def install_capture(test):
    capture = Capture()
    test.addCleanup(capture.close)
    for target, kwargs in (("main.UploadRecords", {"return_value": capture.records}),
                           ("main.emit", {"side_effect": capture.emit})):
        patcher = patch(target, **kwargs)
        patcher.start()
        test.addCleanup(patcher.stop)
    return capture
