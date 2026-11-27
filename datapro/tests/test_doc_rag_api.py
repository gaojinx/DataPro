"""文档知识库 API（0020）：上传 → 列表 → 检索预览 → 删除。

走真实 HTTP 与真实业务库，最后一定清理：知识库是长期数据，测试残留会污染
线上检索结果（一篇同名文档会长期以高分占位）。
"""
import pytest
from fastapi.testclient import TestClient

DOC = """# 投放预算管理办法

## 一、预算申请
投放预算需经市场部与财务部双签后生效，单笔超过 50 万需报总经理审批。

## 二、超投处理
实际花费超出预算 10% 时，需在下月预算中扣减并提交说明。
"""


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        assert r.status_code == 200, r.text
        c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
        yield c


def test_docs_require_auth():
    from app.main import app

    with TestClient(app) as anon:
        assert anon.get("/api/v1/knowledge/docs").status_code in (401, 403)


def test_upload_list_search_delete(client):
    created = []
    try:
        r = client.post("/api/v1/knowledge/docs", json={
            "title": "投放预算管理办法（API 自检）", "content": DOC,
            "source": "selftest.md", "doc_type": "md", "category": "测试"})
        assert r.status_code == 200, r.text
        body = r.json()
        created.append(body["id"])
        assert body["chunk_count"] >= 2
        assert body["replaced"] is False

        listed = client.get("/api/v1/knowledge/docs").json()
        assert any(d["id"] == body["id"] for d in listed)
        assert next(d for d in listed if d["id"] == body["id"])["chunk_count"] >= 2

        # 检索预览不做阈值过滤，用于调参；分数应能区分相关与无关
        hit = client.get("/api/v1/knowledge/docs/search",
                         params={"q": "超投怎么处理", "top_k": 3}).json()
        assert hit["hits"], "相关问题应命中"
        top = hit["hits"][0]
        # 标题在 section 里、正文在 text 里：命中「超投处理」这一段即可
        assert "超投" in top["section"] or "超出预算" in top["text"]
        miss = client.get("/api/v1/knowledge/docs/search",
                          params={"q": "今天天气如何", "top_k": 3}).json()
        assert not miss["hits"] or miss["hits"][0]["score"] < hit["hits"][0]["score"]

        # 同样的内容重复上传应替换而不是新增，避免检索里出现两份相同片段
        again = client.post("/api/v1/knowledge/docs", json={
            "title": "投放预算管理办法（API 自检）", "content": DOC,
            "source": "selftest.md", "doc_type": "md", "category": "测试"})
        assert again.status_code == 200 and again.json()["replaced"] is True
        assert again.json()["id"] == body["id"]
    finally:
        for doc_id in created:
            client.delete(f"/api/v1/knowledge/docs/{doc_id}")

    assert not [d for d in client.get("/api/v1/knowledge/docs").json()
                if d["id"] in created]


def test_upload_rejects_unsupported_extension(client):
    r = client.post("/api/v1/knowledge/docs/upload",
                    files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 400


def test_upload_accepts_markdown_file(client):
    created = []
    try:
        r = client.post("/api/v1/knowledge/docs/upload",
                        files={"file": ("预算.md", DOC.encode("utf-8"), "text/markdown")},
                        params={"title": "预算上传自检", "category": "测试"})
        assert r.status_code == 200, r.text
        created.append(r.json()["id"])
        assert r.json()["chunk_count"] >= 2
    finally:
        for doc_id in created:
            client.delete(f"/api/v1/knowledge/docs/{doc_id}")


def test_reindex_unknown_doc_returns_400(client):
    r = client.post("/api/v1/knowledge/docs/not-exists/reindex")
    assert r.status_code == 400


def test_stats_shape(client):
    r = client.get("/api/v1/knowledge/docs/stats")
    assert r.status_code == 200
    body = r.json()
    assert {"enabled", "chunks", "embedded", "dims", "embedding_available"} <= set(body)
