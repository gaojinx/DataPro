"""会话文件存储（B18）：上传文件与生成文件统一登记。

- 落盘 data/uploads/{session_id}/{file_id}_{安全文件名}
- 索引 data/uploads/index.json（跨重启保留）
- 安全：session_id 白名单字符、文件名清洗、扩展名黑名单、单文件 ≤ 20MB
"""
from __future__ import annotations  # 类方法 list 会遮蔽内建 list，注解需延迟求值

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("datapro.files")

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
    ".gif": "image/gif", ".webp": "image/webp",
    ".zip": "application/zip",
}


class FileStore:
    def __init__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        from app.services.shared_json import SharedJson
        store = SharedJson(INDEX, default={})
        with store.edit() as index:
            # 兼容历史索引：补齐版本链字段（root_id 指向首版，version 从 1 递增）
            for fid, entry in index.items():
                entry.setdefault("root_id", fid)
                entry.setdefault("version", 1)

    @staticmethod
    def _store():
        from app.services.shared_json import SharedJson
        return SharedJson(INDEX, default={})

    def _persist(self) -> None:
        """保留内部调用兼容；实际写入由 SharedJson.edit 在锁内完成。"""
        pass

    @staticmethod
    def _session(session_id: str) -> str:
        return session_id if _SESSION_RE.match(session_id or "") else "default"

    @staticmethod
    def sanitize(filename: str) -> str:
        name = Path(filename or "file").name
        return (_NAME_RE.sub("_", name).strip("._") or "file")[:120]

    def add(self, *, session_id: str, filename: str, content: bytes,
            origin: str = "upload", query_id: str | None = None,
            owner: str | None = None, root_id: str | None = None) -> dict:
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
        stored.write_bytes(content)  # 先落盘，索引写入失败时最多留孤儿文件，不影响一致性

        # 内容密钥扫描（告警不拦截）：疑似凭据打标 + 告警日志，处置权交给用户/管理员
        secret_hits: list[dict] = []
        try:
            from app.services.secret_scan import describe, scan

            secret_hits = scan(content)
            if secret_hits:
                logger.warning("上传内容疑似包含密钥 file=%s session=%s hits=[%s]",
                               safe_name, sid, describe(secret_hits))
                from app.services.metrics import metrics
                metrics.inc("datapro_upload_secret_flag_total")
        except Exception as exc:  # noqa: BLE001 —— 扫描是旁路告警，失败不阻断上传
            logger.warning("密钥扫描失败（按无命中处理）：%s", exc)

        with self._store().edit() as index:
            # 版本链：编辑保存/回滚都归到同一 root_id，version 递增
            chain_root = file_id
            if root_id and root_id in index and index[root_id].get("session_id") == sid:
                chain_root = index[root_id].get("root_id") or root_id
            version = 1 + max(
                (e.get("version", 1) for e in index.values()
                 if e.get("root_id") == chain_root), default=0)
            seq = 1 + max((e.get("seq", 0) for e in index.values()), default=0)
            entry = {
                "file_id": file_id,
                "session_id": sid,
                "name": safe_name,
                "size": len(content),
                "ext": ext,
                "mime": MIME_BY_EXT.get(ext, "application/octet-stream"),
                "origin": origin if origin in ("upload", "generated", "edited") else "upload",
                "query_id": query_id,
                "owner": owner,          # 上传者账号，用于文件访问鉴权（历史文件为空=公开）
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "stored_name": stored.name,
                "seq": seq,
                "root_id": chain_root,
                "version": version,
                "secret_hits": secret_hits,   # 疑似密钥命中（已脱敏），前端列表可见
            }
            index[file_id] = entry
        return self._public(entry)

    def list(self, session_id: str) -> list[dict]:
        sid = self._session(session_id)
        items = [e for e in self._store().snapshot().values() if e["session_id"] == sid]
        return [self._public(e) for e in
                sorted(items, key=lambda x: (x["created_at"], x.get("seq", 0)), reverse=True)]

    def get(self, file_id: str) -> dict | None:
        return self._store().snapshot().get(file_id)

    def versions(self, file_id: str) -> list[dict]:
        """同一版本链的全部文件（按 version 升序）；历史文件补齐 root_id 后也可查。"""
        entry = self.get(file_id)
        if not entry:
            return []
        root = entry.get("root_id") or file_id
        items = [e for e in self._store().snapshot().values() if e.get("root_id") == root]
        return [self._public(e) for e in
                sorted(items, key=lambda x: (x.get("version", 1), x.get("seq", 0)))]

    def content_of(self, entry: dict) -> bytes:
        """读文件内容。`entry` 必须是完整条目（含 stored_name）——不要传 `_public()` 的结果。"""
        return self.path_of(entry).read_bytes()

    def update(self, file_id: str, patch: dict) -> dict | None:
        with self._store().edit() as index:
            entry = index.get(file_id)
            if not entry:
                return None
            entry.update(patch)
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
        with self._store().edit() as index:
            entry = index.pop(file_id, None)
            if not entry:
                return False
        try:
            self.path_of(entry).unlink(missing_ok=True)
        except ValueError:
            pass
        return True

    @staticmethod
    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "stored_name"}


file_store = FileStore()
