"""会话文件存储（B18）：上传文件与生成文件统一登记。

- 落盘 data/uploads/{session_id}/{file_id}_{安全文件名}
- 索引 data/uploads/index.json（跨重启保留）
- 安全：session_id 白名单字符、文件名清洗、扩展名黑名单、单文件 ≤ 20MB
"""
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "data" / "uploads"
INDEX = ROOT / "index.json"

MAX_SIZE = 20 * 1024 * 1024
BLOCKED_EXT = {".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".msi", ".jar", ".vbs"}
_SESSION_RE = re.compile(r"^[\w-]{1,64}$")
_NAME_RE = re.compile(r"[^\w.\-\u4e00-\u9fa5]")

MIME_BY_EXT = {
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".txt": "text/plain",
    ".md": "text/markdown", ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}


class FileStore:
    def __init__(self):
        self._lock = threading.Lock()
        ROOT.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict] = {}
        if INDEX.exists():
            try:
                self._index = json.loads(INDEX.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._index = {}
        # 同秒内创建的文件按递增序号稳定排序
        self._seq = max((e.get("seq", 0) for e in self._index.values()), default=0)

    def _persist(self) -> None:
        INDEX.write_text(json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _session(session_id: str) -> str:
        return session_id if _SESSION_RE.match(session_id or "") else "default"

    @staticmethod
    def sanitize(filename: str) -> str:
        name = Path(filename or "file").name
        return (_NAME_RE.sub("_", name).strip("._") or "file")[:120]

    def add(self, *, session_id: str, filename: str, content: bytes,
            origin: str = "upload", query_id: str | None = None) -> dict:
        safe_name = self.sanitize(filename)
        ext = Path(safe_name).suffix.lower()
        if ext in BLOCKED_EXT:
            raise ValueError(f"不支持的文件类型 {ext}")
        if not content:
            raise ValueError("文件内容为空")
        if len(content) > MAX_SIZE:
            raise ValueError("文件超过 20MB 限制")

        sid = self._session(session_id)
        file_id = uuid.uuid4().hex[:12]
        folder = ROOT / sid
        folder.mkdir(parents=True, exist_ok=True)
        stored = folder / f"{file_id}_{safe_name}"
        stored.write_bytes(content)

        entry = {
            "file_id": file_id,
            "session_id": sid,
            "name": safe_name,
            "size": len(content),
            "ext": ext,
            "mime": MIME_BY_EXT.get(ext, "application/octet-stream"),
            "origin": origin if origin in ("upload", "generated", "edited") else "upload",
            "query_id": query_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stored_name": stored.name,
            "seq": self._seq + 1,
        }
        with self._lock:
            self._seq += 1
            self._index[file_id] = entry
            self._persist()
        return self._public(entry)

    def list(self, session_id: str) -> list[dict]:
        sid = self._session(session_id)
        with self._lock:
            items = [e for e in self._index.values() if e["session_id"] == sid]
        return [self._public(e) for e in
                sorted(items, key=lambda x: (x["created_at"], x.get("seq", 0)), reverse=True)]

    def get(self, file_id: str) -> dict | None:
        with self._lock:
            return self._index.get(file_id)

    def update(self, file_id: str, patch: dict) -> dict | None:
        with self._lock:
            entry = self._index.get(file_id)
            if not entry:
                return None
            entry.update(patch)
            self._persist()
            return self._public(entry)

    def parsed_files(self, session_id: str) -> list[dict]:
        """当前会话已解析为临时表的文件（供召回使用）。"""
        return [f for f in self.list(session_id) if f.get("parsed")]

    def path_of(self, entry: dict) -> Path:
        p = (ROOT / entry["session_id"] / entry["stored_name"]).resolve()
        if ROOT.resolve() not in p.parents:  # 防目录穿越
            raise ValueError("非法路径")
        return p

    def delete(self, file_id: str) -> bool:
        with self._lock:
            entry = self._index.pop(file_id, None)
            if not entry:
                return False
            self._persist()
        try:
            self.path_of(entry).unlink(missing_ok=True)
        except ValueError:
            pass
        return True

    @staticmethod
    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "stored_name"}


file_store = FileStore()
