"""极简 Prometheus 指标（I03）：进程内计数器 + /metrics 文本输出。

只做计数，不引第三方依赖；多实例部署时各实例独立计数，由 Prometheus 汇总。
告警规则见 `ops/prometheus/alerts.yml`。
"""
import threading
from collections import Counter

HELP = {
    "datapro_chat_requests_total": "对话请求数（按模式）",
    "datapro_chat_outcomes_total": "对话结果分布（result/reject/escalate/clarify/error）",
    "datapro_errors_total": "错误码计数",
    "datapro_rate_limited_total": "触发限流次数",
    "datapro_cache_total": "限定范围缓存命中/未命中（scope=intent|meta）",
}


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Counter = Counter()

    def inc(self, name: str, **labels) -> None:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += 1

    def value(self, name: str, **labels) -> int:
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            return self._counters.get(key, 0)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            items = sorted(self._counters.items())
        seen_types: set[str] = set()
        for (name, labels), count in items:
            if name not in seen_types:
                seen_types.add(name)
                lines.append(f"# HELP {name} {HELP.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
            label_text = "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""
            lines.append(f"{name}{label_text} {count}")
        return "\n".join(lines) + ("\n" if lines else "")


metrics = Metrics()
