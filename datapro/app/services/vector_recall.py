"""向量召回 + Rerank（A05）。

编码器二选一，接口统一为 `async encode(texts)`：
- **api**（推荐）：复用 `embed_client`（OpenAI 兼容 /embeddings），无本地依赖；
- **local**：OpenVINO IR + transformers，模型在本地、数据不出域，但镜像大 ~1GB。

`VECTOR_ENCODER=auto` 时优先本地，本地不可用再退 API——两个都不可用则整体关闭，
召回退回纯关键词路（不是报错，`recall.py` 照常工作）。

融合策略：关键词命中优先，向量命中按相似度补位；两路都保留原始分数，便于排查。

索引缓存按 **doc_type 分槽**：表/口径/字段是三类独立文档集。共用一个槽时，三次检索
会互相把对方的索引冲掉，等于每次都全量重编码——走 API embedding 时会把一次问答
变成十几个请求。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from app.config import settings

logger = logging.getLogger("datapro.vector_recall")


def pool_embeddings(hidden, attention_mask=None, mode: str = "cls"):
    """把编码器输出压成句向量。

    三种形态都要认，否则换模型/换导出方式就会静默算错：
    - `[batch, seq, hidden]`：按池化方式取（bge 系列约定取 CLS，不是 mean）；
    - `[batch, hidden]`：导出时已池化过，直接用；
    - 其他维度：直接报错。**不能静默取 [:, 0]**——二维数组的 [:, 0] 是一列标量，
      算出来的相似度毫无意义，而且不会报错。
    """
    import numpy as np

    arr = np.asarray(hidden, dtype=np.float32)
    if arr.ndim == 2:
        return arr
    if arr.ndim != 3:
        raise ValueError(f"编码器输出维度异常 {arr.shape}，期望 2 维或 3 维")
    if (mode or "cls").strip().lower() == "mean":
        if attention_mask is None:
            return arr.mean(axis=1)
        mask = np.asarray(attention_mask, dtype=np.float32)
        if mask.ndim != 2 or mask.shape[1] != arr.shape[1]:
            return arr.mean(axis=1)     # 掩码与序列长度对不上时退回等权平均
        m = mask[..., None]
        return (arr * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-6)
    return arr[:, 0]                    # CLS：bge / 多数中文句向量模型的约定


class Encoder(Protocol):
    async def encode(self, texts: list[str]) -> list[list[float]]:  # 已 L2 归一化
        ...

    @property
    def model(self) -> str:  # 缓存命中判定用：换模型必须让旧向量失效
        ...


def load_local_encoder() -> Encoder | None:
    """按配置加载本地 OpenVINO 编码器；依赖或模型缺失时返回 None。"""
    ir_path = settings.vector_ir_path
    if not ir_path:
        return None
    if not Path(ir_path).exists():
        logger.warning("向量召回已开启但 IR 不存在：%s", ir_path)
        return None
    try:
        import openvino as ov
        from transformers import AutoTokenizer
    except ImportError:
        logger.info("未安装 openvino/transformers，本地编码器不可用")
        return None

    # 加载期失败（IR 损坏、版本不兼容、tokenizer 目录不存在）一律返回 None：
    # 让 _load_failed 生效，不要每轮问答都重试一次并刷一屏堆栈。
    try:
        compiled = ov.Core().compile_model(ir_path, "CPU")
        out_port = compiled.output(0)
        input_names = [i.get_any_name() for i in compiled.inputs]
        tokenizer = AutoTokenizer.from_pretrained(settings.vector_tokenizer)
    except Exception as exc:  # noqa: BLE001
        logger.error("本地编码器加载失败，向量召回关闭（检查 VECTOR_IR_PATH / "
                     "VECTOR_TOKENIZER）：%s", exc)
        return None
    max_length = settings.vector_max_length

    class _OvEncoder:
        @property
        def model(self) -> str:
            return f"local:{Path(ir_path).name}"

        def _encode_sync(self, texts: list[str]) -> list[list[float]]:
            import numpy as np

            enc = tokenizer(texts, padding=True, truncation=True, max_length=max_length,
                            return_tensors="np")
            feed = {n: np.asarray(enc[n], dtype=np.int64) for n in input_names if n in enc}
            raw = compiled(feed)[out_port]
            vecs = pool_embeddings(raw, enc.get("attention_mask"), settings.vector_pooling)
            vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
            return vecs.tolist()

        async def encode(self, texts: list[str]) -> list[list[float]]:
            # 编码是 CPU 密集的：丢线程池，别卡住事件循环（SSE 还在给前端推步骤）
            return await asyncio.to_thread(self._encode_sync, texts)

    return _OvEncoder()


class ApiEncoder:
    """把 `embed_client` 适配成召回编码器；两者共用同一份 embedding 配置。"""

    def __init__(self, model: str):
        self._model = model or "api"

    @property
    def model(self) -> str:
        return f"api:{self._model}"

    async def encode(self, texts: list[str]) -> list[list[float]]:
        from app.services.embed_client import embed_client

        return await embed_client.encode(texts)


def load_encoder() -> Encoder | None:
    """按 `VECTOR_ENCODER` 选择编码器；不可用返回 None（召回关闭，不影响主链路）。"""
    mode = (settings.vector_encoder or "auto").strip().lower()
    if mode in ("auto", "local"):
        enc = load_local_encoder()
        if enc is not None:
            return enc
        if mode == "local":
            logger.warning("VECTOR_ENCODER=local 但本地编码器不可用，向量召回关闭")
            return None
    try:
        from app.services.embed_client import embed_client
    except Exception as exc:  # noqa: BLE001 —— 依赖缺失按不可用处理
        logger.warning("加载 API 编码器失败：%s", exc)
        return None
    if embed_client.available():
        return ApiEncoder(settings.embed_model)
    logger.info("未配置 embedding（EMBED_BASE_URL/EMBED_MODEL/EMBED_API_KEY_REF），向量召回关闭")
    return None


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HybridRanker:
    """向量索引 + 打分排序。

    索引按 (doc_type, 文本列表) 缓存；向量本身优先从 `meta_embedding` 复用，
    只有文本指纹变了的文档才重新编码。
    """

    def __init__(self, encoder: Encoder | None):
        self._encoder = encoder
        self._indexes: dict[str, tuple[list[str], list[Sequence[float]]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self._encoder is not None

    @property
    def model(self) -> str:
        return getattr(self._encoder, "model", "") if self._encoder else ""

    def invalidate(self) -> None:
        self._indexes.clear()

    async def _vectors_for(self, doc_type: str, uids: list[str],
                           texts: list[str]) -> list[list[float]]:
        """优先读库缓存，只对未命中的文本调编码器，再把新向量写回缓存。"""
        use_cache = bool(settings.vector_cache_enabled) and bool(uids) and all(uids)
        cached: dict[str, dict] = {}
        if use_cache:
            from app.services.embedding_store import embedding_store

            cached = await asyncio.to_thread(embedding_store.load, doc_type)

        vecs: list[list[float] | None] = [None] * len(texts)
        todo: list[int] = []
        for i, text in enumerate(texts):
            hit = cached.get(uids[i]) if use_cache else None
            if hit and hit["text_sha"] == _sha(text) and hit["model"] == self.model:
                vecs[i] = list(hit["vec"])
            else:
                todo.append(i)

        if todo:
            fresh = await self._encoder.encode([texts[i] for i in todo])
            rows = []
            for i, vec in zip(todo, fresh):
                vecs[i] = vec
                if use_cache:
                    rows.append({"uid": uids[i], "doc_type": doc_type,
                                 "text_sha": _sha(texts[i]), "model": self.model,
                                 "dim": len(vec), "vec": vec})
            if rows:
                from app.services.embedding_store import embedding_store

                await asyncio.to_thread(embedding_store.save, rows)
            logger.info("向量编码 %d/%d 条（缓存命中 %d）doc_type=%s",
                        len(todo), len(texts), len(texts) - len(todo), doc_type)
        return [v if v is not None else [] for v in vecs]

    async def warm(self, doc_type: str, items: list[dict],
                   text_fn: Callable[[dict], str],
                   uid_fn: Callable[[dict], str]) -> int:
        """预热某类文档的索引（编码 + 落缓存），返回实际有向量的条数。"""
        if not self.available or not items:
            return 0
        texts = [text_fn(it) for it in items]
        uids = [uid_fn(it) for it in items]
        vecs = await self._vectors_for(doc_type, uids, texts)
        self._indexes[doc_type] = (texts, vecs)
        return sum(1 for v in vecs if v)

    async def _index_for(self, doc_type: str, uids: list[str],
                         texts: list[str]) -> list[Sequence[float]]:
        cached = self._indexes.get(doc_type)
        if cached and cached[0] == texts:
            return cached[1]
        # 同一 doc_type 并发检索时别重复编码：只让第一个进，其余等结果
        lock = self._locks.setdefault(doc_type, asyncio.Lock())
        async with lock:
            cached = self._indexes.get(doc_type)
            if cached and cached[0] == texts:
                return cached[1]
            vecs = await self._vectors_for(doc_type, uids, texts)
            self._indexes[doc_type] = (list(texts), vecs)
            return vecs

    async def embed_query(self, question: str) -> list[float]:
        """问题向量只算一次：表/口径/字段三路共用。

        不共用的话，同一次问答要对同一句话编码 3 次——走 API embedding 就是 3 个请求、
        几百毫秒纯浪费，而且三次结果理论上可能不同（上游负载均衡到不同副本）。
        """
        return (await self._encoder.encode([question]))[0]

    async def rank(self, question: str, items: list[dict], text_fn: Callable[[dict], str],
                   top_k: int, min_score: float,
                   uid_fn: Callable[[dict], str] | None = None,
                   doc_type: str = "default",
                   query_vec: Sequence[float] | None = None) -> list[tuple[dict, float]]:
        """返回（条目, 相似度），按分数降序，只保留 ≥ min_score 的前 top_k 条。"""
        if not self.available or not items:
            return []
        texts = [text_fn(it) for it in items]
        uids = [uid_fn(it) for it in items] if uid_fn else [""] * len(items)
        vectors = await self._index_for(doc_type, uids, texts)
        q = list(query_vec) if query_vec is not None else await self.embed_query(question)
        scored = [(_dot(vec, q), i) for i, vec in enumerate(vectors) if vec]
        scored.sort(reverse=True)
        return [(items[i], round(float(s), 6)) for s, i in scored[:top_k]
                if float(s) >= min_score]


def table_doc_text(t: dict) -> str:
    """一份「表文档」的全文。

    **进程内索引与 Milvus 灌库共用这一个函数**：两处各写一套的话，同一个问题在
    `VECTOR_BACKEND=memory` 与 `milvus` 两个部署形态下会得到不同命中结果，
    而差异很难在线上被发现（都不会报错，只是召回变差）。
    """
    pieces = [t.get("cn_name") or t.get("table_name", ""), t.get("description", "")]
    if t.get("keywords"):
        pieces.append(" ".join(t["keywords"]))
    if t.get("columns"):
        # 只放前 30 列，且排在最后：编码窗口默认 128 token（实测表文档 176~261 字），
        # 超宽表会被截尾，必须让「表名→描述→关键词」这些更有区分度的内容留在前面
        pieces.append("字段：" + "、".join(t["columns"][:30]))
    return " ".join(p for p in pieces if p).strip()


def caliber_doc_text(c: dict) -> str:
    """一份「口径文档」的全文：别名必须进文本，用户很少用规范名提问。"""
    return " ".join(p for p in [
        c.get("metric_name", ""),
        " ".join(c.get("aliases") or []),
        c.get("definition", ""),
    ] if p).strip()


def knowledge_doc_text(k: dict) -> str:
    """一份「知识条目」的全文：带上计算口径，用户常直接问「怎么算」。"""
    return " ".join(p for p in [
        k.get("term", ""),
        " ".join(k.get("aliases") or []),
        k.get("definition", ""),
        k.get("formula", ""),
    ] if p).strip()


def column_doc_text(cm: dict) -> str:
    """一条「字段文档」（二级文档）的全文：所属表 + 字段中英文名 + 类型 + 业务提示。

    为什么不写样例值与 PII 标记：样例值多是真实业务数据，编码后会被持久化进 Milvus
    或向量缓存，等于把数据带出边界；字段级检索只需要「这个字段是什么」，不需要
    「里面长什么样」。
    `cm` 需带表上下文（`table_name`/`table_cn`），由 `flatten_columns` 统一补齐。
    """
    return " ".join(p for p in [
        cm.get("table_name", ""),
        cm.get("table_cn", ""),
        cm.get("name", ""),
        cm.get("cn_name", ""),
        cm.get("type", ""),
        cm.get("pitfall", ""),
    ] if p).strip()


def table_uid(t: dict) -> str:
    return f"table:{t.get('table_name', '')}"


def caliber_uid(c: dict) -> str:
    return f"caliber:{c.get('metric_id', '')}"


def column_uid(cm: dict) -> str:
    return f"column:{cm.get('table_name', '')}.{cm.get('name', '')}"


def flatten_columns(tables: list[dict]) -> list[dict]:
    """把「表 → column_meta」摊平成字段列表，并补上所属表上下文。

    检索、灌库、构造文本三处都用这一份：字段与表的对应关系一旦在两处各写一套，
    就会出现「向量库认为是 A 表字段、进程内索引认为是 B 表字段」这种查不出来的错位。
    """
    out: list[dict] = []
    for t in tables:
        table_name = t.get("table_name", "")
        table_cn = t.get("cn_name") or table_name
        for cm in t.get("column_meta") or []:
            if not cm.get("name"):
                continue
            out.append({**cm, "table_name": table_name, "table_cn": table_cn})
    return out


class MilvusIndex:
    """Milvus 后端（A05）：用 `pymilvus` 的 MilvusClient 检索已灌入的元数据文档。

    集合由 `python -m importer.ingest_vectors` 创建（字段 uid/type/text/payload/vector）。
    客户端可注入，便于单测；真实服务不可用时由 VectorRecallService 回退进程内索引。
    """

    def __init__(self, encoder: Encoder, client=None, collection: str | None = None):
        self._encoder = encoder
        self._collection = collection or settings.milvus_collection
        if client is not None:
            self._client = client
        else:
            from pymilvus import MilvusClient  # 延迟导入：未部署 Milvus 时不构成依赖

            self._client = MilvusClient(uri=settings.milvus_uri)

    async def embed_query(self, question: str) -> list[float]:
        return (await self._encoder.encode([question]))[0]

    async def search(self, question: str, doc_type: str, top_k: int,
                     min_score: float, query_vec: Sequence[float] | None = None) -> list[dict]:
        vec = list(query_vec) if query_vec is not None else await self.embed_query(question)
        rows = self._client.search(
            collection_name=self._collection, data=[list(vec)], limit=top_k,
            filter=f'type == "{doc_type}"',
            output_fields=["uid", "type", "text", "payload"])
        hits = []
        for hit in (rows[0] if rows else []):
            score = float(hit.get("distance", 0.0))
            if score < min_score:
                continue
            entity = hit.get("entity") or {}
            payload = entity.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            item = dict(payload or {})
            item["uid"] = entity.get("uid", "")
            hits.append({**item, "vector_score": round(score, 3)})
        return hits


class VectorRecallService:
    def __init__(self):
        self._ranker: HybridRanker | None = None
        self._milvus: MilvusIndex | None = None
        self._load_failed = False

    def _get_ranker(self) -> HybridRanker | MilvusIndex | None:
        if self._ranker is None and self._milvus is None and not self._load_failed:
            encoder = load_encoder()
            if encoder is None:
                self._load_failed = True
                return None
            if settings.vector_backend == "milvus":
                try:
                    self._milvus = MilvusIndex(encoder)
                except Exception as exc:  # noqa: BLE001 —— 连不上就退回进程内索引
                    logger.warning("Milvus 不可用，回退进程内索引：%s", exc)
                    self._ranker = HybridRanker(encoder)
            else:
                self._ranker = HybridRanker(encoder)
        return self._milvus or self._ranker

    def reset(self) -> None:
        """配置变更（换模型/开关）后重建编码器；由预热或运维接口调用。"""
        self._ranker = self._milvus = None
        self._load_failed = False

    async def prewarm(self) -> dict:
        """把表/口径/字段三类文档的向量算好并落缓存。

        启动期调用（后台），避免第一次真实提问时同步编码上百条文档把响应拖到数秒。
        Milvus 后端的向量由 CLI 灌库，不在这里重复编码。
        """
        if not settings.vector_recall_enabled:
            return {"skipped": "disabled"}
        backend = self._get_ranker()
        if backend is None:
            return {"skipped": "no_encoder"}
        if isinstance(backend, MilvusIndex):
            return {"skipped": "milvus"}

        from app.services.metadata_store import metadata_store

        tables = metadata_store.tables()
        calibers = metadata_store.calibers()
        columns = flatten_columns(tables)
        stats = {
            "table": await backend.warm("table", tables, table_doc_text, table_uid),
            "caliber": await backend.warm("caliber", calibers, caliber_doc_text, caliber_uid),
            "column": await backend.warm("column", columns, column_doc_text, column_uid),
        }
        logger.info("向量召回预热完成：%s（encoder=%s）", stats, backend.model)
        return stats

    async def search(self, question: str, tables: list[dict],
                     calibers: list[dict], columns: list[dict] | None = None
                     ) -> tuple[list[dict], list[dict], list[dict]]:
        """向量路召回：返回（补位表, 补位口径, 补位字段）。

        字段路是「二级检索」：字段文档数量是表文档的 6~8 倍，用独立的 top_k 与阈值
        （`VECTOR_COLUMN_TOP_K`/`VECTOR_COLUMN_MIN_SCORE`），不与表共用一套。
        """
        empty: tuple[list[dict], list[dict], list[dict]] = ([], [], [])
        if not settings.vector_recall_enabled:
            return empty
        backend = self._get_ranker()
        if backend is None:
            return empty
        cols = columns or []
        # 问题只编码一次，三路共用（见 HybridRanker.embed_query 的说明）
        q_vec = await backend.embed_query(question)
        if isinstance(backend, MilvusIndex):
            return (await backend.search(question, "table", settings.vector_top_k,
                                         settings.vector_min_score, query_vec=q_vec),
                    await backend.search(question, "caliber", settings.vector_top_k,
                                         settings.vector_min_score, query_vec=q_vec),
                    await backend.search(question, "column", settings.vector_column_top_k,
                                         settings.vector_column_min_score, query_vec=q_vec))
        hit_tables = await backend.rank(
            question, tables, table_doc_text, settings.vector_top_k,
            settings.vector_min_score, uid_fn=table_uid, doc_type="table", query_vec=q_vec)
        hit_calibers = await backend.rank(
            question, calibers, caliber_doc_text, settings.vector_top_k,
            settings.vector_min_score, uid_fn=caliber_uid, doc_type="caliber",
            query_vec=q_vec)
        hit_columns = await backend.rank(
            question, cols, column_doc_text, settings.vector_column_top_k,
            settings.vector_column_min_score, uid_fn=column_uid, doc_type="column",
            query_vec=q_vec)
        return ([{**t, "vector_score": round(s, 3)} for t, s in hit_tables],
                [{**c, "vector_score": round(s, 3)} for c, s in hit_calibers],
                [{**c, "vector_score": round(s, 3)} for c, s in hit_columns])


vector_recall = VectorRecallService()
