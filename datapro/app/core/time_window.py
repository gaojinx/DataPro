"""时间语义解析（确定性，不调用 LLM）。

设计原则：
- 日期算术交给代码，模型只负责把「已确定的时间窗」翻译成 SQL 条件；
- 认得出 → 返回闭区间 [start, end]；认不出但"有时间的形状" → 交澄清（绝不猜）；
- 完全没提时间 → 默认最近 7 天，并在回答的假设里显式声明（不静默改口径）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

DEFAULT_DAYS = 7


@dataclass(frozen=True)
class TimeWindow:
    start: date
    end: date
    grain: str = "day"          # day | month | quarter | year
    label: str = ""
    explicit: bool = True       # False 表示系统默认值，必须在回答中声明

    def text(self) -> str:
        """注入 prompt 的时间窗描述。"""
        return f"{self.start.isoformat()} ~ {self.end.isoformat()}（{self.label}）"

    def days(self) -> int:
        return (self.end - self.start).days + 1


# ---- 日期工具 ----

def latest_partition(today: date | None = None) -> date:
    """日报表最新可用分区：T-1。"""
    return (today or date.today()) - timedelta(days=1)


def anchor_date(today: date | None = None) -> date:
    """相对时间窗的锚点：优先数仓真实最新分区，探测不到才回退日历 T-1。

    只影响「最近 N 天 / 本周 / 本月」这类相对表达；显式日期（昨天、11月20日）
    仍按日历解析——用户说出的日期不能被悄悄挪动。
    """
    from app.services.partition_clock import partition_clock

    return partition_clock.anchor(today)


def _month_first(d: date) -> date:
    return d.replace(day=1)


def _shift_month(d: date, n: int) -> date:
    """d 所在月的 1 号平移 n 个月。"""
    y, m = d.year, d.month - 1 + n
    y += m // 12
    return date(y, m % 12 + 1, 1)


def _prev_month_end(d: date) -> date:
    return _month_first(d) - timedelta(days=1)


def default_window(today: date | None = None) -> TimeWindow:
    """未提时间时的默认窗口：最近 7 天（显式标记 explicit=False）。"""
    end = anchor_date(today)
    return TimeWindow(end - timedelta(days=DEFAULT_DAYS - 1), end, "day",
                      f"最近 {DEFAULT_DAYS} 天（默认）", explicit=False)


# ---- 中文数字 ----

_CN_DIGIT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGIT.get(s)


# ---- 各类时间表达 ----

_RECENT = re.compile(
    r"(?:最近|近|过去)\s*([0-9]{1,3}|[一二两三四五六七八九十]{1,3})?\s*"
    r"(天|日|周|个星期|星期|个月|月|个季度|季度|年)")

_FULL_DATE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
_MONTH_DAY = re.compile(r"(?<![\d年])(\d{1,2})月(\d{1,2})[日号]")
_ONE_MONTH = re.compile(r"(?<![\d年])(\d{1,2})月(?![\d])")
_YEAR_MONTH = re.compile(r"(\d{4})[-/年](\d{1,2})月?(?!\d)")
_SLASH_RANGE = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:到|至|~|—|–|-)\s*(\d{1,2})/(\d{1,2})")

_REL_DAYS = (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1),
             ("今天", 0), ("今日", 0), ("当天", 0))


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _resolve_range(t: str, today: date) -> TimeWindow | None:
    """11/1~11/20、11月1日到11月20日、2026-11-01至2026-11-20。"""
    m = _SLASH_RANGE.search(t)
    if m:
        a = _safe_date(today.year, int(m.group(1)), int(m.group(2)))
        b = _safe_date(today.year, int(m.group(3)), int(m.group(4)))
        if a and b:
            if a > b:
                a, b = b, a
            return TimeWindow(a, b, "day", f"{a.isoformat()} ~ {b.isoformat()}")
    for sep in ("到", "至", "~"):
        if sep not in t:
            continue
        left, _, right = t.partition(sep)
        a, b = _parse_single_date(left, today), _parse_single_date(right, today)
        if a and b:
            if a > b:
                a, b = b, a
            return TimeWindow(a, b, "day", f"{a.isoformat()} ~ {b.isoformat()}")
    return None


def _parse_single_date(text: str, today: date) -> date | None:
    m = _FULL_DATE.search(text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MONTH_DAY.search(text)
    if m:
        return _safe_date(today.year, int(m.group(1)), int(m.group(2)))
    return None


def resolve(text: str, today: date | None = None) -> TimeWindow | None:
    """从问题文本中解析时间窗；解析不出返回 None（调用方决定默认或澄清）。"""
    t = (text or "").replace(" ", "").strip()
    if not t:
        return None
    today = today or date.today()
    latest = anchor_date(today)

    # 1) 明确区间
    w = _resolve_range(t, today)
    if w:
        return w

    # 2) 显式日期优先于相对词：避免"2026-11-20当天"被"当天"解析成今天
    m = _FULL_DATE.search(t)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return TimeWindow(d, d, "day", d.isoformat())
    m = _MONTH_DAY.search(t)
    if m:
        d = _safe_date(today.year, int(m.group(1)), int(m.group(2)))
        if d:
            return TimeWindow(d, d, "day", d.isoformat())

    # 3) 单日相对表达
    for kw, off in _REL_DAYS:
        if kw in t:
            d = today - timedelta(days=off)
            return TimeWindow(d, d, "day", kw)

    # 4) 周 / 月 / 季 / 年
    if any(k in t for k in ("本周", "这周", "本星期", "这个星期")):
        s = today - timedelta(days=today.weekday())
        return TimeWindow(s, max(s, latest), "day", "本周")
    if any(k in t for k in ("上周", "上个星期", "上星期")):
        s = today - timedelta(days=today.weekday() + 7)
        return TimeWindow(s, s + timedelta(days=6), "day", "上周")
    if any(k in t for k in ("本月", "这个月")):
        s = _month_first(today)
        return TimeWindow(s, max(s, latest), "month", "本月")
    if any(k in t for k in ("上月", "上个月")):
        e = _prev_month_end(today)
        return TimeWindow(_month_first(e), e, "month", "上月")
    if any(k in t for k in ("本季度", "这个季度")):
        s = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
        return TimeWindow(s, max(s, latest), "quarter", "本季度")
    if any(k in t for k in ("上季度", "上个季度")):
        s = _shift_month(date(today.year, (today.month - 1) // 3 * 3 + 1, 1), -3)
        return TimeWindow(s, _shift_month(s, 3) - timedelta(days=1), "quarter", "上季度")
    if any(k in t for k in ("今年", "本年")):
        s = date(today.year, 1, 1)
        return TimeWindow(s, max(s, latest), "year", "今年")
    if "去年" in t:
        return TimeWindow(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "year", "去年")

    # 4) 最近 N 天/周/月/季/年
    m = _RECENT.search(t)
    if m:
        n = _cn_to_int(m.group(1)) or 1
        unit = m.group(2)
        if unit in ("天", "日"):
            return TimeWindow(latest - timedelta(days=n - 1), latest, "day", f"最近 {n} 天")
        if unit in ("周", "星期", "个星期"):
            return TimeWindow(latest - timedelta(days=7 * n - 1), latest, "day", f"最近 {n} 周")
        if unit in ("个月", "月"):
            s = _shift_month(_month_first(latest), -(n - 1))
            return TimeWindow(s, latest, "month", f"最近 {n} 个月")
        if unit in ("个季度", "季度"):
            s = _shift_month(_month_first(latest), -3 * (n - 1))
            return TimeWindow(s, latest, "quarter", f"最近 {n} 个季度")
        s = date(latest.year - (n - 1), 1, 1)
        return TimeWindow(s, latest, "year", f"最近 {n} 年")

    # 6) 具体月份
    m = _YEAR_MONTH.search(t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            s = date(y, mo, 1)
            e = _shift_month(s, 1) - timedelta(days=1)
            return TimeWindow(s, e, "month", f"{y} 年 {mo} 月")
    m = _ONE_MONTH.search(t)
    if m:
        mo = int(m.group(1))
        if 1 <= mo <= 12:
            y = today.year if mo <= today.month else today.year - 1
            s = date(y, mo, 1)
            e = _shift_month(s, 1) - timedelta(days=1)
            return TimeWindow(s, e, "month", f"{y} 年 {mo} 月")
    return None


# ---- 歧义识别：有时间的形状但无法安全落成一个窗口 ----

_AMBIGUOUS_WORDS = ("同比", "环比", "对比", "相比", "前阵子", "前段时间", "近期",
                    "这几天", "早些时候", "月初", "月底", "上旬", "中旬", "下旬")
_TWO_MONTHS = re.compile(r"\d{1,2}月")


def is_ambiguous_time(text: str, today: date | None = None) -> bool:
    """需要向用户确认时间口径的情形：模糊表达、两期对比、同期对比。

    明确区间（11月16日到11月22日）先解析成功，不算歧义——否则"11月"出现两次会被误判。
    """
    t = (text or "").replace(" ", "")
    if not t:
        return False
    if any(k in t for k in _AMBIGUOUS_WORDS):
        return True
    if _resolve_range(t, today or date.today()):
        return False
    return len(set(_TWO_MONTHS.findall(t))) >= 2


def resolve_question(text: str, today: date | None = None) -> tuple[TimeWindow | None, bool]:
    """返回 (时间窗, 是否需要对时间澄清)。

    - (window, False)：可直接使用（显式或默认）
    - (None, True)  ：时间表达有歧义，必须澄清
    - (None, False) ：未提时间，由调用方沿用上一轮或取默认值
    """
    if is_ambiguous_time(text, today):
        return None, True
    return resolve(text, today), False
