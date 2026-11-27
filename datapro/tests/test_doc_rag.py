"""文档知识库 RAG（0020）：解析、切分、词法打分、阈值拒答、embedding 降级。

只测纯函数与可注入的服务边界，不连库：`_load_rows_sync` 用假索引替换，
`embed_client.available()` 用 monkeypatch 关掉——这两处正是生产里最容易出问题
（库抖动、embedding 不可用）的降级路径。
"""
import asyncio

import pytest

from app.services import doc_rag as mod
from app.services.doc_rag import (DocRagService, lexical_score, parse, split_chunks,
                                  vec_from_bytes, vec_to_bytes)

HTML = """
<html><head><meta charset="utf-8"><style>.a{color:red}</style></head>
<body>
  <script>var x=1;</script>
  <h1>退款管理规范</h1>
  <p>消费者申请退款后，商家需在 48 小时内响应。</p>
  <h2>跨月退款对账</h2>
  <p>跨月退款需在次月 5 日前完成对账，逾期由财务手工调账。</p>
</body></html>
"""


def test_html_parse_keeps_headings_drops_script():
    text = parse(HTML, "html")
    assert "# 退款管理规范" in text
    assert "## 跨月退款对账" in text
    assert "var x" not in text and "color:red" not in text
    assert "48 小时内响应" in text


def test_html_self_closing_meta_does_not_swallow_body():
    """<meta>/<link> 是自闭合标签：早期实现把它们算进跳过深度，会把整篇正文吞掉。"""
    text = parse('<head><meta charset="utf-8"><link rel="x"></head><p>正文在这里</p>', "html")
    assert "正文在这里" in text


def test_split_chunks_keeps_section_path():
    chunks = split_chunks(parse(HTML, "html"), size=200, overlap=0)
    assert chunks, "至少切出一块"
    assert chunks[-1]["section"] == "退款管理规范 > 跨月退款对账"
    assert any("5 日前完成对账" in c["text"] for c in chunks)
    assert [c["seq"] for c in chunks] == list(range(len(chunks)))


def test_split_chunks_window_size_and_overlap():
    body = "第一句。" * 60          # 240 字，size=100 应切成多块
    chunks = split_chunks(body, size=100, overlap=20)
    assert len(chunks) >= 2
    assert all(len(c["text"]) <= 100 for c in chunks)
    # 相邻块必须有重叠，否则跨块的句子会被割断
    assert chunks[0]["text"][-10:] in chunks[1]["text"]


def test_split_chunks_does_not_deadloop_on_tiny_overlap():
    """overlap >= size 时必须被夹紧，否则窗口不前进会死循环。"""
    chunks = split_chunks("测试句子。" * 30, size=80, overlap=999)
    assert len(chunks) > 1


def test_lexical_score_separates_relevant_from_irrelevant():
    text = "跨月退款需在次月 5 日前完成对账，逾期由财务手工调账。"
    assert lexical_score("跨月退款什么时候对账", text, "退款管理规范") > 0.5
    assert lexical_score("广告投放预算怎么申请", text, "退款管理规范") < 0.2


def test_vector_roundtrip_is_stable():
    vec = [0.5, -0.25, 1.0, 0.0]
    back = vec_from_bytes(vec_to_bytes(vec))
    assert back is not None and len(back) == 4
    assert all(abs(a - b) < 1e-6 for a, b in zip(vec, back))


@pytest.fixture
def service(monkeypatch):
    """假索引：两条强相关块 + 一条无关块，全部没有向量（模拟 embedding 未配置）。"""
    svc = DocRagService()
    rows = [
        {"id": 1, "doc_id": "d1", "seq": 0, "section": "退款管理规范 > 跨月退款对账",
         "text": "跨月退款需在次月 5 日前完成对账，逾期由财务手工调账。",
         "title": "退款管理规范", "category": "财务", "source": "a.md",
         "vec": None, "dim": 0},
        {"id": 2, "doc_id": "d1", "seq": 1, "section": "退款管理规范 > 退款时限",
         "text": "消费者申请退款后，商家需在 48 小时内响应。",
         "title": "退款管理规范", "category": "财务", "source": "a.md",
         "vec": None, "dim": 0},
        {"id": 3, "doc_id": "d2", "seq": 0, "section": "投放制度",
         "text": "广告投放预算需经市场部与财务部双签。",
         "title": "投放制度", "category": "市场", "source": "b.md",
         "vec": None, "dim": 0},
    ]
    monkeypatch.setattr(svc, "_load_rows_sync", lambda: ("3:3", rows), raising=False)

    from app.services import embed_client as ec

    monkeypatch.setattr(ec.embed_client, "available", lambda: False, raising=False)
    svc.invalidate()
    return svc


def test_search_hits_relevant_section(service):
    hits = asyncio.run(service.search("跨月退款什么时候对账"))
    assert hits, "相关问题必须命中"
    assert hits[0]["section"].endswith("跨月退款对账")
    assert hits[0]["title"] == "退款管理规范"
    assert hits[0]["score"] >= 0.35


def test_search_returns_empty_below_threshold(service):
    """阈值是安全阀：无关问题宁可返回空（上层说「未收录」），也不返回低分片段。"""
    hits = asyncio.run(service.search("今天天气怎么样"))
    assert hits == []


def test_search_respects_top_k(service):
    hits = asyncio.run(service.search("退款", top_k=1, min_score=0.0))
    assert len(hits) == 1


def test_search_degrades_to_lexical_when_embedding_unavailable(service, monkeypatch):
    """embedding 抛错不能把检索打成 0 命中：词法分应独立给出结果。"""
    from app.services import embed_client as ec

    async def boom(_texts):
        raise ec.EmbeddingError("upstream 500")

    monkeypatch.setattr(ec.embed_client, "available", lambda: True, raising=False)
    monkeypatch.setattr(ec.embed_client, "encode", boom, raising=False)
    hits = asyncio.run(service.search("跨月退款什么时候对账"))
    assert hits and hits[0]["vector_score"] == 0.0


def test_dim_mismatch_blocks_are_skipped(service, monkeypatch):
    """换了 embedding 模型但没 reindex：维度不一致的块必须跳过，不能算错相似度。"""
    from app.services import embed_client as ec

    async def fake_encode(_texts):
        return [[1.0, 0.0]]

    monkeypatch.setattr(ec.embed_client, "available", lambda: True, raising=False)
    monkeypatch.setattr(ec.embed_client, "encode", fake_encode, raising=False)
    for row in service._load_rows_sync()[1]:
        row["vec"], row["dim"] = [1.0, 0.0, 0.0, 0.0], 4
    service.invalidate()
    hits = asyncio.run(service.search("跨月退款什么时候对账"))
    # 向量被跳过，只剩词法分，仍应命中相关块且向量分为 0
    assert hits and all(h["vector_score"] == 0.0 for h in hits)


def test_render_context_numbers_and_truncates():
    hits = [
        {"title": "A", "section": "s1", "text": "内容一" * 20, "score": 0.9},
        {"title": "B", "section": "s2", "text": "内容二" * 20, "score": 0.8},
    ]
    full = mod.DocRagService.render_context(hits, max_chars=5000)
    assert "[1]《A》" in full and "[2]《B》" in full
    short = mod.DocRagService.render_context(hits, max_chars=120)
    assert "[1]《A》" in short and "已省略" in short


def test_embed_client_unavailable_raises(monkeypatch):
    """未配置时不返回零向量：零向量会让检索静默全部不命中，比报错更难查。"""
    from app.config import settings
    from app.services.embed_client import EmbedClient, EmbeddingUnavailable

    monkeypatch.setattr(settings, "embed_base_url", "", raising=False)
    monkeypatch.setattr(settings, "embed_model", "", raising=False)
    client = EmbedClient()
    assert client.available() is False
    with pytest.raises(EmbeddingUnavailable):
        asyncio.run(client.encode(["测试"]))


def test_embed_client_rejects_empty_text(monkeypatch):
    from app.config import settings
    from app.services.embed_client import EmbedClient, EmbeddingError

    monkeypatch.setattr(settings, "embed_base_url", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(settings, "embed_model", "test-embed", raising=False)
    with pytest.raises(EmbeddingError):
        asyncio.run(EmbedClient().encode(["  "]))


def test_ingest_rejects_empty_content(service):
    with pytest.raises(ValueError):
        asyncio.run(service.ingest("空文档", "   ", doc_type="md"))


def test_ingest_rejects_unsupported_type(service):
    with pytest.raises(ValueError):
        asyncio.run(service.ingest("x", "正文内容足够长的一段文字。", doc_type="pdf"))


def test_meta_qa_answers_from_docs_only(monkeypatch):
    """只命中文档（没有表、没有口径）时不该 E002 拒答，这是引入文档 RAG 的核心动因。"""
    from types import SimpleNamespace

    from app.core.statemachine import Pipeline, PipelineContext

    class _RecallSvc:
        async def recall(self, question, session_id=None):
            return SimpleNamespace(tables=[], calibers=[], knowledge=[])

    captured = {}

    class _AnswerSvc:
        async def answer_meta(self, ctx, recall, docs=None):
            captured["docs"] = docs
            return "跨月退款需在次月 5 日前完成对账 [1]"

    svc = DocRagService()

    async def fake_search(question, top_k=None, min_score=None):
        return [{"doc_id": "d1", "title": "退款管理规范", "section": "跨月退款对账",
                 "text": "跨月退款需在次月 5 日前完成对账。", "score": 0.82,
                 "vector_score": 0.8, "lexical_score": 0.7, "seq": 0,
                 "source": "a.md", "category": "财务"}]

    monkeypatch.setattr(svc, "search", fake_search, raising=False)
    monkeypatch.setattr(mod, "doc_rag", svc, raising=False)

    pipeline = Pipeline(None, _RecallSvc(), None, None, None, _AnswerSvc())
    ctx = PipelineContext(question="跨月退款什么时候对账", user_id="admin", user_role="admin")

    async def collect():
        return [ev async for ev in pipeline._run_meta_qa(ctx)]

    events = asyncio.run(collect())
    result = [e for e in events if e.get("type") == "result"]
    assert result, "必须产出 result 事件而不是抛 E002"
    assert captured["docs"] and captured["docs"][0]["title"] == "退款管理规范"
    sources = result[0]["sources"]
    docs_src = [s for s in sources if s["type"] == "doc"]
    assert docs_src and docs_src[0]["name"] == "退款管理规范 § 跨月退款对账"


def test_doc_search_failure_does_not_break_meta_qa(monkeypatch):
    """文档库异常按「无文档」继续，不能让整轮口径问答失败。"""
    from types import SimpleNamespace

    from app.core.statemachine import Pipeline, PipelineContext

    class _RecallSvc:
        async def recall(self, question, session_id=None):
            return SimpleNamespace(tables=[], calibers=[{"metric_name": "退款率",
                                                         "definition": "x", "logic": "y"}],
                                   knowledge=[])

    class _AnswerSvc:
        async def answer_meta(self, ctx, recall, docs=None):
            assert docs == []
            return "退款率定义"

    async def boom(question, top_k=None, min_score=None):
        raise RuntimeError("db down")

    svc = DocRagService()
    monkeypatch.setattr(svc, "search", boom, raising=False)
    monkeypatch.setattr(mod, "doc_rag", svc, raising=False)

    pipeline = Pipeline(None, _RecallSvc(), None, None, None, _AnswerSvc())
    ctx = PipelineContext(question="退款率是什么", user_id="u", user_role="user")

    events = asyncio.run(_collect(pipeline._run_meta_qa(ctx)))
    assert [e for e in events if e.get("type") == "result"], "文档检索失败不应导致本轮失败"


async def _collect(gen):
    return [ev async for ev in gen]
