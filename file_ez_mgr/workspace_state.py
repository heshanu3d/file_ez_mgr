"""Persist open tabs and their paths, without credentials or queued operations."""
import json
import os
import tempfile
from pathlib import Path


class WorkspaceStateStore:
    def __init__(self, directory):
        self.path = Path(directory) / "workspace.json"

    def load(self):
        if not self.path.exists():
            return None
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("窗口记录格式或版本不受支持")
        geometry = state.get("geometry")
        if geometry is not None and (not isinstance(geometry, dict)
                or type(geometry.get("width")) is not int or type(geometry.get("height")) is not int
                or not 980 <= geometry["width"] <= 16384
                or not 650 <= geometry["height"] <= 16384
                or type(geometry.get("maximized")) is not bool):
            raise ValueError("窗口尺寸记录格式错误")
        tabs = state.get("tabs")
        if not isinstance(tabs, list) or not isinstance(state.get("active_index"), int):
            raise ValueError("窗口标签记录格式错误")
        if state.get("selected_profile") is not None and not isinstance(state["selected_profile"], str):
            raise ValueError("窗口连接记录格式错误")
        for tab in tabs:
            if not isinstance(tab, dict):
                raise ValueError("窗口标签记录格式错误")
            if tab.get("profile_id") is not None and not isinstance(tab["profile_id"], str):
                raise ValueError("窗口连接记录格式错误")
            if any(not isinstance(tab.get(key), str) for key in ("local_dir", "remote_dir")):
                raise ValueError("窗口目录记录格式错误")
            if tab.get("conflict", "overwrite") not in ("skip", "overwrite"):
                raise ValueError("窗口传输设置格式错误")
        return state

    def save(self, tabs, active_index, selected_profile, geometry=None):
        state = dict(version=1, tabs=tabs, active_index=active_index, selected_profile=selected_profile)
        if geometry is not None:
            state["geometry"] = geometry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
