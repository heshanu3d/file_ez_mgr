"""Per-connection download destinations, stored independently of credentials."""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class DownloadHistory:
    LIMIT = 50

    def __init__(self, config_dir, profile):
        identity = [profile.id, profile.protocol, profile.host, profile.port, profile.username]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        self.path = Path(config_dir) / "download-history" / f"{key}.json"

    def load(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), list):
            raise ValueError("下载历史格式错误，原文件已保留")
        for item in data["items"]:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key]
                for key in ("name", "local_dir", "remote_dir", "time")
            ):
                raise ValueError("下载历史条目错误，原文件已保留")
        return data["items"][:self.LIMIT]

    def record(self, name, local_dir, remote_dir):
        items = self.load()
        item = dict(name=name, local_dir=str(local_dir), remote_dir=remote_dir,
                    time=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        items = [item] + [old for old in items if any(old[k] != item[k] for k in ("name", "local_dir", "remote_dir"))]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".history-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "items": items[:self.LIMIT]}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
