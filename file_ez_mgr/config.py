from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Profile:
    name: str = "新连接"
    protocol: str = "sftp"
    host: str = ""
    port: int = 22
    username: str = ""
    local_dir: str = ""
    remote_dir: str = "."
    key_file: str = ""
    passive: bool = True
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.local_dir:
            self.local_dir = str(Path.home())


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "connections.json"

    def load(self) -> list[Profile]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("配置格式或版本不受支持")
        records = data.get("connections")
        if not isinstance(records, list):
            raise ValueError("连接配置必须是列表")
        keys = {field.name for field in fields(Profile)}
        profiles = []
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("连接配置项格式错误")
            profile = Profile(**{key: value for key, value in item.items() if key in keys})
            if profile.protocol not in {"sftp", "ftp", "ftps", "tftp"}:
                raise ValueError("配置包含不支持的协议")
            if not isinstance(profile.port, int) or not 1 <= profile.port <= 65535:
                raise ValueError("配置包含无效端口")
            for key in ("id", "name", "host", "username", "local_dir", "remote_dir", "key_file"):
                if not isinstance(getattr(profile, key), str):
                    raise ValueError(f"配置字段 {key} 必须是字符串")
            profiles.append(profile)
        if len({p.id for p in profiles}) != len(profiles):
            raise ValueError("配置包含重复的连接 ID")
        return profiles

    def save(self, profiles: list[Profile]):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".connections-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "connections": [asdict(p) for p in profiles]},
                          stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
