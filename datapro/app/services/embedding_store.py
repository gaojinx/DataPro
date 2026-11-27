"""元数据向量缓存（0021）：`meta_embedding` 的读写。

只做存取，不做编码决策——「哪些文档需要重算」由 `vector_recall` 按 text_sha 判定。
独立出来的原因：库访问要能单独注入假实现做单测，也要能在库不可用时静默降级
（返回空表 → 上层按未命中处理，重新编码，结果仍正确，只是慢一次）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("datapro.embedding_store")


class EmbeddingStore:
    def load(self, doc_type: str | None = None) -> dict[str, dict]:
        """读取缓存：uid → {text_sha, model, dim, vec}。

        库不可用时返回空字典而不是抛错——上层会退化为「全部未命中 → 重新编码」，
        召回结果依然正确，只是这一轮多花几秒。
        """
        try:
            return self._load_sync(doc_type)
        except Exception as exc:  # noqa: BLE001 —— 缓存是优化项，坏了不能影响召回
            logger.warning("读取元数据向量缓存失败，本次按未命中处理：%s", exc)
            return {}

    @staticmethod
    def _load_sync(doc_type: str | None) -> dict[str, dict]:
        from app.db.models import MetaEmbedding
        from app.db.session import SessionLocal
        from app.services.doc_rag import vec_from_bytes

        with SessionLocal() as db:
            q = db.query(MetaEmbedding)
            if doc_type:
                q = q.filter(MetaEmbedding.doc_type == doc_type)
            out: dict[str, dict] = {}
            for row in q.all():
                vec = vec_from_bytes(row.vector)
                if vec is None:
                    continue
                out[row.uid] = {"text_sha": row.text_sha, "model": row.model,
                                "dim": row.dim, "vec": vec}
            return out

    def save(self, items: list[dict]) -> int:
        """写入/更新；失败只告警。items: {uid, doc_type, text_sha, model, dim, vec}"""
        if not items:
            return 0
        try:
            return self._save_sync(items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入元数据向量缓存失败（不影响本轮召回）：%s", exc)
            return 0

    @staticmethod
    def _save_sync(items: list[dict]) -> int:
        from app.db.models import MetaEmbedding
        from app.db.session import SessionLocal
        from app.services.doc_rag import vec_to_bytes

        with SessionLocal() as db:
            for it in items:
                uid = it["uid"]
                row = db.query(MetaEmbedding).filter(MetaEmbedding.uid == uid).first()
                vec_bytes = vec_to_bytes([float(x) for x in it["vec"]])
                if row is None:
                    db.add(MetaEmbedding(uid=uid, doc_type=it.get("doc_type", ""),
                                         text_sha=it["text_sha"], model=it.get("model", ""),
                                         dim=it.get("dim") or len(it["vec"]),
                                         vector=vec_bytes))
                else:
                    row.doc_type = it.get("doc_type", row.doc_type)
                    row.text_sha = it["text_sha"]
                    row.model = it.get("model", "")
                    row.dim = it.get("dim") or len(it["vec"])
                    row.vector = vec_bytes
            db.commit()
        return len(items)

    def prune(self, keep_uids: set[str], doc_type: str | None = None) -> int:
        """删除已不存在的文档（表被下线、口径被删）。失败忽略。"""
        try:
            return self._prune_sync(keep_uids, doc_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理元数据向量缓存失败：%s", exc)
            return 0

    @staticmethod
    def _prune_sync(keep_uids: set[str], doc_type: str | None) -> int:
        from app.db.models import MetaEmbedding
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            q = db.query(MetaEmbedding)
            if doc_type:
                q = q.filter(MetaEmbedding.doc_type == doc_type)
            removed = 0
            for row in q.all():
                if row.uid not in keep_uids:
                    db.delete(row)
                    removed += 1
            if removed:
                db.commit()
            return removed


embedding_store = EmbeddingStore()
