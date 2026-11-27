"""向量召回（A05）单测：排序、阈值、降级、缓存、Milvus 映射，使用假编码器/假客户端。

不连库：所有用例关掉 `VECTOR_CACHE_ENABLED`（否则会读写真实 `meta_embedding`，
测试数据污染线上召回）。
"""
import json
import math

import pytest

from app.config import settings
from app.services.vector_recall import HybridRanker, VectorRecallService


class FakeEncoder:
    """按关键词映射到三分类向量，模拟语义编码器。"""

    @property
    def model(self):
        return "fake:v1"

    async def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0]
            if "退款" in t:
                v[0] = 1
            if "订单" in t:
                v[1] = 1
            if "用户" in t:
                v[2] = 1
            norm = math.sqrt(sum(x * x for x in v)) or 1e-12
            out.append([x / norm for x in v])
        return out


TABLES = [
    {"table_name": "dwd_refund_di", "cn_name": "退款明细日报表", "description": "退款、退货"},
    {"table_name": "dwd_order_detail_di", "cn_name": "订单明细日报表", "description": "订单、支付"},
    {"table_name": "dwd_user_di", "cn_name": "用户行为日报表", "description": "注册、登录"},
]


@pytest.fixture(autouse=True)
def _no_vector_cache(monkeypatch):
    monkeypatch.setattr(settings, "vector_cache_enabled", False, raising=False)


async def test_ranker_orders_by_similarity_and_filters():
    ranker = HybridRanker(FakeEncoder())
    hits = await ranker.rank("退款率是多少", TABLES, lambda t: f"{t['cn_name']} {t['description']}",
                             top_k=2, min_score=0.5)
    assert hits and hits[0][0]["table_name"] == "dwd_refund_di"
    assert hits[0][1] == 1.0
    assert all(score >= 0.5 for _, score in hits)


async def test_ranker_respects_top_k():
    ranker = HybridRanker(FakeEncoder())
    hits = await ranker.rank("退款 订单 用户", TABLES, lambda t: f"{t['cn_name']} {t['description']}",
                             top_k=1, min_score=0.0)
    assert len(hits) == 1


async def test_ranker_keeps_separate_index_per_doc_type():
    """表/口径/字段共用一套索引槽时，三次检索会互相冲掉索引 → 每次全量重编码。"""
    ranker = HybridRanker(FakeEncoder())
    fn = lambda t: f"{t['cn_name']} {t['description']}"
    await ranker.rank("退款", TABLES, fn, top_k=1, min_score=0.0, doc_type="table")
    await ranker.rank("退款", TABLES, fn, top_k=1, min_score=0.0, doc_type="column")
    await ranker.rank("退款", TABLES, fn, top_k=1, min_score=0.0, doc_type="table")
    assert set(ranker._indexes) == {"table", "column"}


async def test_service_degrades_to_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "vector_recall_enabled", False)
    tables, calibers, columns = await VectorRecallService().search("退款率", TABLES, [])
    assert tables == [] and calibers == [] and columns == []


async def test_service_returns_scored_items(monkeypatch):
    monkeypatch.setattr(settings, "vector_recall_enabled", True)
    monkeypatch.setattr(settings, "vector_min_score", 0.5)
    svc = VectorRecallService()
    svc._ranker = HybridRanker(FakeEncoder())      # 跳过模型加载
    tables, _, columns = await svc.search("退款率是多少", TABLES, [])
    assert columns == []            # 没传字段列表时字段路为空，不影响原有两路
    assert tables and tables[0]["table_name"] == "dwd_refund_di"
    assert "vector_score" in tables[0]


async def test_prewarm_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "vector_recall_enabled", False)
    assert await VectorRecallService().prewarm() == {"skipped": "disabled"}


async def test_search_encodes_query_only_once(monkeypatch):
    """三路检索共用同一个问题向量：否则一次问答要对同一句话编码 3 次。"""
    monkeypatch.setattr(settings, "vector_recall_enabled", True)
    calibers = [{"metric_id": "M012", "metric_name": "退货率", "aliases": ["退款率"],
                 "definition": "退款订单占支付订单"}]

    class CountingEncoder(FakeEncoder):
        def __init__(self):
            self.calls = 0

        async def encode(self, texts):
            self.calls += len(texts)
            return await super().encode(texts)

    enc = CountingEncoder()
    ranker = HybridRanker(enc)
    # warm 必须用与检索完全相同的文本构造函数，否则索引键对不上、等于没预热
    from app.services.vector_recall import caliber_doc_text, caliber_uid, table_doc_text, table_uid

    await ranker.warm("table", TABLES, table_doc_text, table_uid)
    await ranker.warm("caliber", calibers, caliber_doc_text, caliber_uid)

    before = enc.calls
    svc = VectorRecallService()
    svc._ranker = ranker
    await svc.search("退款率是多少", TABLES, calibers, [])
    assert enc.calls - before == 1, "除问题向量外不应再有编码"


async def test_vector_cache_reuses_unchanged_text(monkeypatch):
    """文本没变就不要再编码：否则每个进程启动都要重付上百条 embedding 的钱和时间。"""
    monkeypatch.setattr(settings, "vector_cache_enabled", True)
    store: dict[str, dict] = {}

    from app.services import embedding_store as es

    monkeypatch.setattr(es.embedding_store, "load",
                        lambda doc_type: {k: dict(v) for k, v in store.items()})

    def save(items):
        for it in items:
            store[it["uid"]] = {"text_sha": it["text_sha"], "model": it["model"],
                                "dim": it["dim"], "vec": it["vec"]}
        return len(items)

    monkeypatch.setattr(es.embedding_store, "save", save)

    class CountingEncoder(FakeEncoder):
        def __init__(self):
            self.calls = 0

        async def encode(self, texts):
            self.calls += len(texts)
            return await super().encode(texts)

    enc = CountingEncoder()
    ranker = HybridRanker(enc)
    fn = lambda t: f"{t['cn_name']} {t['description']}"
    uid = lambda t: f"table:{t['table_name']}"

    await ranker.rank("退款", TABLES, fn, 1, 0.0, uid_fn=uid, doc_type="table")
    first = enc.calls
    assert first == len(TABLES) + 1        # 文档全量 + 一次 query

    ranker.invalidate()                     # 模拟进程重启：内存索引没了，但库缓存还在
    await ranker.rank("退款", TABLES, fn, 1, 0.0, uid_fn=uid, doc_type="table")
    assert enc.calls - first == 1           # 只编码了 query


async def test_vector_cache_invalidated_when_text_changes(monkeypatch):
    """表描述改了必须重编码该条，不能继续用旧向量。"""
    import hashlib

    monkeypatch.setattr(settings, "vector_cache_enabled", True)
    store: dict[str, dict] = {}

    from app.services import embedding_store as es

    def save(items):
        for it in items:
            store[it["uid"]] = {"text_sha": it["text_sha"], "model": it["model"],
                                "dim": it["dim"], "vec": it["vec"]}
        return len(items)

    monkeypatch.setattr(es.embedding_store, "load", lambda doc_type: dict(store))
    monkeypatch.setattr(es.embedding_store, "save", save)

    ranker = HybridRanker(FakeEncoder())
    uid = lambda t: f"table:{t['table_name']}"
    await ranker.rank("退款", TABLES, lambda t: t["description"], 1, 0.0,
                      uid_fn=uid, doc_type="table")

    changed = [dict(TABLES[0], description="退款、退货、售后新描述"), *TABLES[1:]]
    ranker.invalidate()
    await ranker.rank("退款", changed, lambda t: t["description"], 1, 0.0,
                      uid_fn=uid, doc_type="table")
    assert store["table:dwd_refund_di"]["text_sha"] == hashlib.sha256(
        "退款、退货、售后新描述".encode()).hexdigest()


async def test_prewarm_reports_counts(monkeypatch):
    monkeypatch.setattr(settings, "vector_recall_enabled", True)
    svc = VectorRecallService()
    svc._ranker = HybridRanker(FakeEncoder())

    from app.services.metadata_store import metadata_store

    monkeypatch.setattr(metadata_store, "tables", lambda: TABLES)
    monkeypatch.setattr(metadata_store, "calibers", lambda: [])
    stats = await svc.prewarm()
    assert stats["table"] == 3 and stats["caliber"] == 0 and stats["column"] == 0


def test_pool_embeddings_cls_and_mean():
    """bge 系列用 CLS；部分 sentence-transformers 模型用 mean。选错不报错、只让分布偏移。"""
    np = pytest.importorskip("numpy")
    from app.services.vector_recall import pool_embeddings

    hidden = np.array([[[1.0, 0.0], [3.0, 0.0], [5.0, 0.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0]], dtype=np.int64)

    cls = pool_embeddings(hidden, mask, "cls")
    assert cls.shape == (1, 2) and cls[0][0] == pytest.approx(1.0)

    mean = pool_embeddings(hidden, mask, "mean")
    assert mean[0][0] == pytest.approx(2.0)      # 只对未 padding 的两个 token 求平均


def test_pool_embeddings_accepts_prepooled_output():
    np = pytest.importorskip("numpy")
    from app.services.vector_recall import pool_embeddings

    arr = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)
    out = pool_embeddings(arr, None, "cls")
    assert out.shape == (1, 3)


def test_pool_embeddings_rejects_bad_shape():
    """1 维输出直接报错：静默取 [:, 0] 会算出毫无意义的相似度。"""
    np = pytest.importorskip("numpy")
    from app.services.vector_recall import pool_embeddings

    with pytest.raises(ValueError):
        pool_embeddings(np.zeros((1,), dtype=np.float32), None, "cls")


def test_local_encoder_none_without_ir(monkeypatch):
    from app.services.vector_recall import load_encoder, load_local_encoder

    monkeypatch.setattr(settings, "vector_ir_path", "", raising=False)
    assert load_local_encoder() is None

    monkeypatch.setattr(settings, "vector_encoder", "local", raising=False)
    assert load_encoder() is None, "local 模式缺 IR 时必须整体关闭，而不是报错"


def test_local_encoder_none_when_ir_missing(monkeypatch, tmp_path):
    from app.services.vector_recall import load_local_encoder

    monkeypatch.setattr(settings, "vector_ir_path",
                        str(tmp_path / "nope.xml"), raising=False)
    assert load_local_encoder() is None


async def test_force_keyword_switch_skips_vector_path(monkeypatch):
    """RECALL_DEGRADED_MODE=force_keyword 必须是真开关：效果不及预期时能立刻退回。"""
    from app.services import recall as recall_mod

    monkeypatch.setattr(settings, "vector_recall_enabled", True)
    monkeypatch.setattr(settings, "recall_degraded_mode", "force_keyword")
    called = {"n": 0}

    async def spy(*args, **kwargs):
        called["n"] += 1
        return [], [], []

    monkeypatch.setattr(recall_mod.vector_recall, "search", spy)
    await recall_mod.recall_service.recall("查昨天的支付订单数")
    assert called["n"] == 0


class FakeMilvusClient:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return [[
            {"distance": 0.91, "entity": {"uid": "table:dwd_refund_di", "type": "table",
                                          "text": "退款",
                                          "payload": json.dumps({"table_name": "dwd_refund_di"})}},
            {"distance": 0.10, "entity": {"uid": "table:other", "type": "table",
                                          "text": "低分", "payload": "{}"}},
        ]]


async def test_milvus_index_filters_and_parses_payload():
    from app.services.vector_recall import MilvusIndex

    client = FakeMilvusClient()
    index = MilvusIndex(FakeEncoder(), client=client, collection="meta")
    hits = await index.search("退款率", "table", top_k=3, min_score=0.35)
    assert len(hits) == 1 and hits[0]["table_name"] == "dwd_refund_di"
    assert hits[0]["vector_score"] == 0.91
    assert client.calls[0]["filter"] == 'type == "table"'
    assert client.calls[0]["collection_name"] == "meta"
