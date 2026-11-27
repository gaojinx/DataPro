"""管理后台对话记录接口（需求：管理台查看对话记录）单测：参数清洗与透传。"""
from app.api import routes_admin
from app.services import persist


async def test_records_route_normalizes_params(monkeypatch):
    captured = {}

    async def fake(username, days, rating, keyword, page, page_size, outcome, error_code):
        captured.update(username=username, days=days, rating=rating, keyword=keyword,
                        page=page, page_size=page_size,
                        outcome=outcome, error_code=error_code)
        return ([{"id": 7, "question": "查销售额"}], 1)

    monkeypatch.setattr(persist, "list_records", fake)
    data = await routes_admin.list_records(username=" admin ", days=7, rating="up",
                                          keyword=" 销售 ", page=0, page_size=999,
                                          outcome=" reject ", error_code=" E003 ", _=None)
    assert captured["username"] == "admin" and captured["keyword"] == "销售"
    assert captured["page"] == 1 and captured["page_size"] == 100
    assert captured["outcome"] == "reject" and captured["error_code"] == "E003"
    assert data["total"] == 1 and data["items"][0]["id"] == 7


def test_merge_records_orders_and_pages():
    # 取数记录与消息按时间倒序合并，分页切片正确
    queries = [{"source": "query", "created_at": "2026-11-24 10:02:00"},
               {"source": "query", "created_at": "2026-11-24 10:00:00"}]
    messages = [{"source": "message", "created_at": "2026-11-24 10:01:00"}]
    items, total = persist.merge_records(queries, messages, page=1, page_size=2)
    assert total == 3
    assert [i["created_at"] for i in items] == ["2026-11-24 10:02:00", "2026-11-24 10:01:00"]
    page2, _ = persist.merge_records(queries, messages, page=2, page_size=2)
    assert [i["source"] for i in page2] == ["query"]
