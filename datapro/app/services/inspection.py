"""数据巡检（A08）：规则配置化 + 执行 + 结果持久化 + webhook 告警。

规则存 `data/inspection_rules.json`，结果存 `data/inspection_results.json`（保留最近 200 条）。
支持检查：rowcount_drop（分区行数骤降）、null_ratio（关键字段空值率）、no_data（断流/分区过期）。
执行只读，异常只记日志/标 error，不影响其他规则。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa

from app.config import settings
from app.services.shared_json import SharedJson, file_lock

logger = logging.getLogger("datapro.inspection")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RULES_PATH = _DATA_DIR / "inspection_rules.json"
RESULTS_PATH = _DATA_DIR / "inspection_results.json"
MAX_RESULTS = 200

DEFAULT_RULES = [
    {"id": "R001", "table": "dwd_order_detail_di", "check": "rowcount_drop",
     "threshold": 0.5, "enabled": True, "desc": "订单表分区行数较前一日下降超过 50%"},
    {"id": "R002", "table": "dwd_order_detail_di", "check": "null_ratio", "column": "amount",
     "threshold": 0.05, "enabled": True, "desc": "订单金额空值率超过 5%"},
    {"id": "R003", "table": "dwd_user_di", "check": "rowcount_drop",
     "threshold": 0.5, "enabled": True, "desc": "用户表分区行数较前一日下降超过 50%"},
    {"id": "R004", "table": "dwd_order_detail_di", "check": "no_data",
     "threshold": 1, "enabled": True, "desc": "订单表最新分区落后今天超过 1 天（断流）"},
]


class InspectionService:
    def __init__(self):
        # 多实例：按调用时的模块级路径取存储，保持测试 monkeypatch 有效
        pass

    @staticmethod
    def _rules_path() -> Path:
        return RULES_PATH

    @staticmethod
    def _results_path() -> Path:
        return RESULTS_PATH

    # ---------- 规则 ----------
    def rules(self) -> list[dict]:
        store = SharedJson(self._rules_path(), default=[])
        rows = store.snapshot()
        if not isinstance(rows, list) or not rows:
            with store.edit() as data:
                if not data:
                    data.extend(DEFAULT_RULES)
            return [dict(r) for r in DEFAULT_RULES]
        return rows

    def save_rules(self, rules: list[dict]) -> list[dict]:
        """保存规则：校验必填、去重 id，阈值转 float。"""
        cleaned, seen = [], set()
        for raw in rules or []:
            rule = dict(raw)
            rule_id = str(rule.get("id") or "").strip()
            table = str(rule.get("table") or "").strip()
            check = str(rule.get("check") or "").strip()
            if not rule_id or not table or check not in ("rowcount_drop", "null_ratio", "no_data"):
                raise ValueError("规则需要 id / table / check（rowcount_drop|null_ratio|no_data）")
            if rule_id in seen:
                raise ValueError(f"规则 id 重复：{rule_id}")
            seen.add(rule_id)
            try:
                rule["threshold"] = float(rule.get("threshold") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"规则 {rule_id} 阈值必须是数字") from exc
            rule["enabled"] = bool(rule.get("enabled", True))
            cleaned.append(rule)
        with SharedJson(self._rules_path(), default=[]).edit() as data:
            data[:] = cleaned
        return cleaned

    # ---------- 结果 ----------
    def results(self) -> dict:
        payload = SharedJson(self._results_path(),
                             default={"updated_at": None, "items": []}).snapshot()
        if not isinstance(payload, dict):
            payload = {"updated_at": None, "items": []}
        return payload

    def summary(self) -> dict:
        payload = self.results()
        items = payload.get("items") or []
        latest_ts = items[0].get("ts") if items else None
        latest = items[0].get("items", []) if items else []
        return {
            "updated_at": payload.get("updated_at"),
            "latest_ts": latest_ts,
            "latest_total": len(latest),
            "latest_failed": sum(1 for x in latest if x.get("status") == "fail"),
            "latest_error": sum(1 for x in latest if x.get("status") == "error"),
            "rules": len(self.rules()),
        }

    # ---------- 执行 ----------
    def run_all(self, triggered_by: str = "manual") -> dict:
        if not settings.feature_inspection_enabled:
            return {"ts": None, "total": 0, "failed": 0, "items": [], "skipped": True}
        # 跨进程互斥：worker 定时与 admin 手动触发不能并发跑两轮巡检
        _run_lock = self._results_path().with_name("inspection_run.lock")
        with file_lock(_run_lock):
            return self._run_all_locked(triggered_by)

    def _run_all_locked(self, triggered_by: str) -> dict:
        started = time.time()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        engine = sa.create_engine(settings.data_source_url, pool_pre_ping=True)
        items: list[dict] = []
        try:
            with engine.connect() as conn:
                for rule in self.rules():
                    if not rule.get("enabled", True):
                        items.append({**self._base(rule), "status": "skip", "message": "规则已停用"})
                        continue
                    try:
                        items.append(self._run_rule(conn, rule))
                    except Exception as exc:  # noqa: BLE001 —— 单条规则失败不阻断其他规则
                        logger.warning("巡检规则 %s 执行失败：%s", rule.get("id"), exc)
                        items.append({**self._base(rule), "status": "error",
                                      "message": f"执行失败：{str(exc).splitlines()[0]}"})
        finally:
            engine.dispose()

        failed = [x for x in items if x["status"] == "fail"]
        payload = {
            "ts": ts,
            "triggered_by": triggered_by,
            "duration_ms": int((time.time() - started) * 1000),
            "total": len(items),
            "failed": len(failed),
            "items": items,
        }
        with SharedJson(self._results_path(),
                        default={"updated_at": None, "items": []}).edit() as data:
            rows = data.get("items") or []
            rows.insert(0, payload)
            data["updated_at"] = ts
            data["items"] = rows[:MAX_RESULTS]
        if failed:
            self._notify_failures(ts, failed)
        # 指标（I03/I04）：巡检轮次与失败规则计数，供告警规则使用
        from app.services.metrics import metrics
        metrics.inc("datapro_inspection_runs_total", result="fail" if failed else "pass")
        for item in failed:
            metrics.inc("datapro_inspection_failure_total", check=item.get("check") or "unknown")
        logger.info("巡检完成：%s 条规则，%s 条失败，耗时 %sms", len(items), len(failed),
                    payload["duration_ms"])
        return payload

    # ---------- 内部 ----------
    @staticmethod
    def _base(rule: dict) -> dict:
        return {"rule_id": rule.get("id"), "table": rule.get("table"),
                "check": rule.get("check"), "column": rule.get("column"),
                "desc": rule.get("desc", "")}

    def _run_rule(self, conn, rule: dict) -> dict:
        table = str(rule["table"])
        full = f"dwd_shop.{table}" if "." not in table else table
        check = rule["check"]
        base = self._base(rule)

        latest, prev = self._partitions(conn, full)
        if latest is None:
            return {**base, "status": "fail", "actual": None,
                    "message": "表内没有任何 dt 分区数据"}

        if check == "rowcount_drop":
            if prev is None:
                return {**base, "status": "skip", "message": "只有一个分区，无法比较前一日"}
            cur_cnt = self._count(conn, full, latest)
            prev_cnt = self._count(conn, full, prev)
            if prev_cnt == 0:
                return {**base, "status": "skip", "message": f"前一日 {prev} 无数据，无法比较"}
            drop = (prev_cnt - cur_cnt) / prev_cnt
            threshold = float(rule.get("threshold") or 0.5)
            status = "fail" if drop > threshold else "pass"
            return {**base, "status": status, "actual": round(drop, 4),
                    "threshold": threshold, "latest": str(latest), "previous": str(prev),
                    "rows": cur_cnt,
                    "message": (f"{latest} 行数 {cur_cnt}，较 {prev} 的 {prev_cnt} "
                                f"下降 {drop:.1%}（阈值 {threshold:.0%}）")}

        if check == "null_ratio":
            column = str(rule.get("column") or "")
            if not column:
                return {**base, "status": "error", "message": "null_ratio 规则缺少 column"}
            nulls, total = self._null_ratio(conn, full, column, latest)
            threshold = float(rule.get("threshold") or 0.05)
            ratio = (nulls / total) if total else 0.0
            status = "fail" if ratio > threshold else "pass"
            return {**base, "status": status, "actual": round(ratio, 4),
                    "threshold": threshold, "latest": str(latest),
                    "message": (f"{latest} 分区 {column} 空值 {nulls}/{total}，"
                                f"空值率 {ratio:.2%}（阈值 {threshold:.0%}）")}

        # no_data：最新分区落后今天超过 threshold 天
        lag_days = (date.today() - latest).days
        threshold = int(float(rule.get("threshold") or 1))
        status = "fail" if lag_days > threshold else "pass"
        return {**base, "status": status, "actual": lag_days, "threshold": threshold,
                "latest": str(latest),
                "message": f"最新分区 {latest}，落后今天 {lag_days} 天（阈值 {threshold} 天）"}

    @staticmethod
    def _partitions(conn, table: str):
        latest = conn.execute(sa.text(f"SELECT MAX(dt) FROM {table}")).scalar()
        if latest is None:
            return None, None
        prev = conn.execute(sa.text(f"SELECT MAX(dt) FROM {table} WHERE dt < :d"),
                            {"d": latest}).scalar()
        return latest, prev

    @staticmethod
    def _count(conn, table: str, dt) -> int:
        return int(conn.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE dt = :d"),
                                {"d": dt}).scalar() or 0)

    @staticmethod
    def _null_ratio(conn, table: str, column: str, dt) -> tuple[int, int]:
        row = conn.execute(
            sa.text(f"SELECT SUM(`{column}` IS NULL) AS n, COUNT(*) AS c "
                    f"FROM {table} WHERE dt = :d"), {"d": dt}).mappings().first()
        return int(row["n"] or 0), int(row["c"] or 0)

    @staticmethod
    def _notify_failures(ts: str, failures: list[dict]) -> None:
        """IM webhook 告警（可选）：只发失败规则，超时 5 秒，失败仅记日志。"""
        url = settings.inspection_webhook_url.strip()
        if not url:
            return
        text = "\n".join(f"- [{x['rule_id']}] {x['table']} {x['message']}" for x in failures)
        message = f"DataPro 巡检告警 {ts}\n失败 {len(failures)} 条：\n{text}"
        from app.services.webhook import send_text
        send_text(url, message, kind=settings.inspection_webhook_type,
                  secret=settings.inspection_webhook_secret)


inspection_service = InspectionService()
