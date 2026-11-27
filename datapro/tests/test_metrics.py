"""监控指标（I03）单测：计数与 Prometheus 文本格式。"""
from app.services.metrics import Metrics


def test_counter_and_render():
    m = Metrics()
    m.inc("datapro_chat_requests_total", mode="query")
    m.inc("datapro_chat_requests_total", mode="query")
    m.inc("datapro_chat_requests_total", mode="general")
    assert m.value("datapro_chat_requests_total", mode="query") == 2
    text = m.render()
    assert "# TYPE datapro_chat_requests_total counter" in text
    assert 'datapro_chat_requests_total{mode="query"} 2' in text
    assert 'datapro_chat_requests_total{mode="general"} 1' in text


def test_empty_render():
    assert Metrics().render() == ""
