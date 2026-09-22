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
        if not isinstance(data, dict) or data.get("version") not in (1, 2) or not isinstance(data.get("items"), list):
            raise ValueError("下载历史格式错误，原文件已保留")
        for item in data["items"]:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key]
                for key in ("local_dir", "remote_dir", "time")
            ):
                raise ValueError("下载历史条目错误，原文件已保留")
        # Old records lack a file/directory marker. Keep their directory pairs
        # intact rather than guessing a parent and changing saved destinations.
        items, seen = [], set()
        for item in data["items"]:
            key = (item["local_dir"], item["remote_dir"])
            if key not in seen:
                seen.add(key)
                items.append({key: item[key] for key in ("local_dir", "remote_dir", "time")})
        return items[:self.LIMIT]

    def record(self, local_dir, remote_dir):
        items = self.load()
        item = dict(local_dir=str(local_dir), remote_dir=remote_dir,
                    time=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        items = [item] + [old for old in items
                          if (old["local_dir"], old["remote_dir"]) != (item["local_dir"], item["remote_dir"])]
        self._save(items)

    def delete(self, pairs):
        # Reload at deletion time so downloads completed while the dialog was
        # open are preserved unless their directory pair was selected.
        pairs = set(pairs)
        self._save([item for item in self.load()
                    if (item["local_dir"], item["remote_dir"]) not in pairs])

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".history-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 2, "items": items[:self.LIMIT]}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
