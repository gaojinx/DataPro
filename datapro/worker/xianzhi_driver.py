"""采集驱动：CollectorConfig / CollectorDriver 接口 / PlaywrightDriver / XhrDriver(P1) / LocalXlsxDriver。

安全约束：
- 配置只接受 secret_ref 的 file:/env:/kms:// 引用，明文形态在 validate() 被拒；
- 站点选择器集中管理；未抓包的 XHR 接口不臆造；
- 任何异常只对外暴露类型名 + 原因码，不回显页面 HTML、Cookie 或表单值。
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.services import secret_ref
from worker.xianzhi_fields import HEADER_MAP, RawRow

logger = logging.getLogger("datapro.ingest.driver")

REPORT_PATH = "/fe/infoAnalysis"
REPORT_HOST_HINT = "websiteManagement"   # 登录成功后的落地页，实测一手结论
WORKER_DIR = Path(__file__).resolve().parent.parent
DATAPRO_DIR = WORKER_DIR

# DOM 选择器集中管理：站点是 SPA，改版时只改这里。
# 未做 DOM 快照，因此采用「按可见文本/占位符」的稳妥定位并多重兜底，不臆造 CSS id。
SELECTORS: dict[str, tuple[str, ...]] = {
    "username": ("input[type=text]", "input[name=username]", "input[placeholder*='账号']",
                 "input[placeholder*='用户名']"),
    "password": ("input[type=password]", "input[name=password]"),
    "login_button": ("button:has-text('登录')", "text=登录", "input[type=submit]"),
    "date_start": ("input[placeholder*='开始']", "input[placeholder*='起始']",
                   "input[name=startDate]", ".ant-picker-input input"),
    "date_end": ("input[placeholder*='结束']", "input[placeholder*='截止']",
                 "input[name=endDate]"),
    "export_button": ("button:has-text('导出数据')", "text=导出数据", "a:has-text('导出数据')"),
}

_RETRYABLE_CODES = frozenset({"NETWORK", "TIMEOUT", "HTTP_5XX", "BROWSER_CRASH"})


class CollectorError(RuntimeError):
    """采集失败： reason_code 用于告警与批次 error，message 保证无明文/HTML。"""

    def __init__(self, reason_code: str, detail: str = "", retryable: bool = False) -> None:
        self.reason_code = reason_code
        self.retryable = retryable or reason_code in _RETRYABLE_CODES
        self.detail = detail
        super().__init__(f"[{reason_code}] {detail or reason_code}")


@dataclass
class CollectorConfig:
    """一次采集的运行参数；凭据字段只能是引用，不能是明文。"""

    site_url: str = "https://xianzhi.umeng100.com/fe/infoAnalysis"
    username_ref: str = ""
    password_ref: str = ""
    date_mode: str = "lookback"          # lookback | range
    lookback_days: int = 3
    date_start: str = ""                 # range 模式用
    date_end: str = ""
    timeout_sec: int = 45
    max_retry: int = 3
    source_code: str = "xianzhi"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CollectorConfig":
        known = {k: v for k, v in payload.items() if k in cls.__dataclass_fields__ and k != "extra"}
        return cls(extra={k: v for k, v in payload.items() if k not in known}, **known)

    @classmethod
    def from_file(cls, path: str | Path) -> "CollectorConfig":
        import json

        target = Path(path)
        if not target.is_absolute():
            target = DATAPRO_DIR / target
        return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))

    def validate(self, require_secrets: bool = True) -> None:
        """fail-closed：非法/明文引用、非法日期策略一律在启动前拒绝。

        require_secrets=False 仅供本地 xlsx 回填（不登录站点，不需要凭据）。
        """
        if not (self.site_url or "").startswith("https://"):
            raise CollectorError("BAD_CONFIG", "site_url 必须为 https 地址")
        if self.date_mode not in ("lookback", "range"):
            raise CollectorError("BAD_CONFIG", "date_mode 只能为 lookback 或 range")
        if self.date_mode == "lookback" and not (1 <= int(self.lookback_days) <= 30):
            raise CollectorError("BAD_CONFIG", "lookback_days 必须在 1~30 之间")
        if self.date_mode == "range":
            if not self.date_start or not self.date_end:
                raise CollectorError("BAD_CONFIG", "range 模式必须提供 date_start/date_end")
            if _parse_iso_date(self.date_end) < _parse_iso_date(self.date_start):
                raise CollectorError("BAD_CONFIG", "date_end 不能早于 date_start")
        if not require_secrets:
            return
        for label, ref in (("XIANZHI_USERNAME", self.username_ref),
                           ("XIANZHI_PASSWORD", self.password_ref)):
            try:
                normalized = secret_ref.validate_ref(ref)
            except secret_ref.SecretRefError as exc:
                raise CollectorError("BAD_REF", f"{label} 引用非法（{type(exc).__name__}）") from exc
            if not normalized:
                raise CollectorError("BAD_REF", f"{label} 引用为空")
            if secret_ref.looks_like_plaintext(normalized):
                raise CollectorError("BAD_REF", f"{label} 拒绝明文值")

    def build_window(self, today: date | None = None) -> tuple[date, date]:
        """lookback → [T-N, T-1]；range → 显式区间。『昨天』按日历，不随锚点漂移。"""
        anchor = today or datetime.now().strftime("%Y-%m-%d")
        if isinstance(anchor, date):
            today_date = anchor
        else:
            today_date = _parse_iso_date(str(anchor))
        if self.date_mode == "range":
            return _parse_iso_date(self.date_start), _parse_iso_date(self.date_end)
        end = today_date - timedelta(days=1)
        return end - timedelta(days=int(self.lookback_days) - 1), end


def _parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise CollectorError("BAD_DATE", "日期格式应为 YYYY-MM-DD") from exc


def _classify_exception(exc: BaseException) -> CollectorError:
    """把底层异常收敛成原因码；绝不把 exc 原文（可能含 HTML/表单值）透传出去。"""
    if isinstance(exc, CollectorError):
        return exc
    name = type(exc).__name__
    if "Timeout" in name:
        return CollectorError("TIMEOUT", name, retryable=True)
    if "Network" in name or "Connection" in name or "Transport" in name:
        return CollectorError("NETWORK", name, retryable=True)
    return CollectorError("DRIVER_ERROR", name)


class CollectorDriver(ABC):
    """抓数接口：落表/元数据层只依赖本接口，A/B 路线可替换。"""

    @abstractmethod
    def fetch(self, config: CollectorConfig, start: date, end: date,
              secrets: dict[str, str] | None = None) -> list[RawRow]:
        """按 [start, end] 抓取报表原始行。secrets 只在内存中传递。"""

    @abstractmethod
    def close(self) -> None:
        """释放浏览器/连接等资源；可重复调用。"""


class PlaywrightDriver(CollectorDriver):
    """路线 A（MVP）：登录 → 跳 websiteManagement → 回 infoAnalysis → 选日期 → 导出 xlsx。

    站点登录流程为主理人一手实测；DOM 选择器未做快照，需首次带真实凭据实跑时校准 SELECTORS。
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.login_url = "https://xianzhi.umeng100.com/"
        self.sha256 = ""                 # 导出文件校验和，写 ingest_batch 血缘
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    def _ensure_browser(self, timeout_sec: int) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CollectorError("DEPENDENCY_MISSING", "playwright 未安装") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            context = self._browser.new_context(accept_downloads=True)
            self._page = context.new_page()
            self._page.set_default_timeout(timeout_sec * 1000)
        except CollectorError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 只透出类型名
            raise _classify_exception(exc) from exc
        return self._page

    @staticmethod
    def _locate(page: Any, names: tuple[str, ...], label: str) -> Any:
        """按 SELECTORS 候选顺序找第一个可见元素；找不到抛原因码，不回显 DOM。"""
        for selector in names:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:  # noqa: BLE001 —— 选择器非法时尝试下一个
                continue
        raise CollectorError("DOM_NOT_FOUND", f"{label} 控件未找到，疑似站点改版")

    def login(self, secrets: dict[str, str], timeout_sec: int = 45) -> None:
        """提交登录表单；失败（凭据错）不重试，由上层 fail-closed。"""
        page = self._ensure_browser(timeout_sec)
        try:
            page.goto(self.login_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except Exception as exc:  # noqa: BLE001
            raise _classify_exception(exc) from exc
        try:
            self._locate(page, SELECTORS["username"], "账号输入框").fill(secrets.get("username", ""))
            self._locate(page, SELECTORS["password"], "密码输入框").fill(secrets.get("password", ""))
            self._locate(page, SELECTORS["login_button"], "登录按钮").click()
            page.wait_for_load_state("domcontentloaded", timeout=timeout_sec * 1000)
        except CollectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 登录拒绝可能是凭据错：统一按不可重试处理，且不回显页面内容
            raise CollectorError("LOGIN_FAILED", type(exc).__name__) from exc

    def goto_report(self, site_url: str, timeout_sec: int = 45) -> None:
        """登录后显式回到报表页（实测会先落到 websiteManagement）。"""
        page = self._ensure_browser(timeout_sec)
        target = urljoin(site_url, REPORT_PATH)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
            if REPORT_HOST_HINT in page.url:
                # 部分账号首次访问仍被弹回管理页，再导航一次
                page.goto(target, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except Exception as exc:  # noqa: BLE001
            raise _classify_exception(exc) from exc

    def pick_dates(self, start: date, end: date, timeout_sec: int = 45) -> None:
        """必须显式设置站点日期控件，不依赖站点默认区间（默认窗错配是历史故障根因）。"""
        page = self._ensure_browser(timeout_sec)
        start_input = self._locate(page, SELECTORS["date_start"], "开始日期")
        end_input = self._locate(page, SELECTORS["date_end"], "结束日期")
        try:
            for locator, value in ((start_input, start.isoformat()), (end_input, end.isoformat())):
                locator.click()
                locator.fill(value)
                locator.press("Tab")
            page.wait_for_timeout(300)
        except Exception as exc:  # noqa: BLE001
            raise CollectorError("DATE_PICK_FAILED", type(exc).__name__) from exc

    def export_xlsx(self, timeout_sec: int = 45) -> bytes:
        """点「导出数据」并读取下载文件；只返回字节，不把临时路径写进日志。"""
        page = self._ensure_browser(timeout_sec)
        button = self._locate(page, SELECTORS["export_button"], "导出数据按钮")
        try:
            with page.expect_download(timeout=timeout_sec * 1000) as info:
                button.click()
            download = info.value
            payload = download.path().read_bytes()
            size = len(payload)
        except Exception as exc:  # noqa: BLE001
            raise _classify_exception(exc) from exc
        if size < 100:
            raise CollectorError("EXPORT_EMPTY", f"导出文件过小({size}B)")
        return payload

    @staticmethod
    def parse_xlsx(payload: bytes) -> list[RawRow]:
        """表头必须完整匹配 15 列，否则判站点改版，不静默入库。"""
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise CollectorError("DEPENDENCY_MISSING", "openpyxl 未安装") from exc
        try:
            wb = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
            ws = wb.worksheets[0]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            wb.close()
        except Exception as exc:  # noqa: BLE001
            raise CollectorError("XLSX_READ_FAILED", type(exc).__name__) from exc
        if not rows:
            raise CollectorError("HEADER_MISMATCH", "导出文件为空")
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        missing = [cn for cn in HEADER_MAP if cn not in header]
        if missing or len(header) != len(HEADER_MAP):
            raise CollectorError("HEADER_MISMATCH", f"缺失 {len(missing)} 个表头列")
        result: list[RawRow] = []
        for idx, values in enumerate(rows[1:], start=2):
            if all(v is None for v in values):
                continue
            cells = {header[i]: values[i] for i in range(min(len(header), len(values)))}
            result.append(RawRow(cells=cells, row_no=idx))
        return result

    def fetch(self, config: CollectorConfig, start: date, end: date,
              secrets: dict[str, str] | None = None) -> list[RawRow]:
        secrets = secrets or {}
        last_error: CollectorError | None = None
        for attempt in range(1, int(config.max_retry) + 1):
            try:
                self.close()                      # 每次重试重建上下文，避免脏会话
                parts = urlsplit(config.site_url)
                self.login_url = f"{parts.scheme}://{parts.netloc}/"
                self.login(secrets, config.timeout_sec)
                self.goto_report(config.site_url, config.timeout_sec)
                self.pick_dates(start, end, config.timeout_sec)
                payload = self.export_xlsx(config.timeout_sec)
                self.sha256 = hashlib.sha256(payload).hexdigest()
                rows = self.parse_xlsx(payload)
                if not rows:
                    raise CollectorError("EXPORT_EMPTY", "导出行数为 0")
                return rows
            except CollectorError as exc:
                last_error = exc
                if not exc.retryable or attempt >= int(config.max_retry):
                    break
                time.sleep(min(2 ** attempt, 8))
            except Exception as exc:  # noqa: BLE001
                last_error = _classify_exception(exc)
                if not last_error.retryable or attempt >= int(config.max_retry):
                    break
                time.sleep(min(2 ** attempt, 8))
        raise last_error or CollectorError("DRIVER_ERROR", "未知抓取失败")

    def close(self) -> None:
        for attr in ("_page", "_browser", "_playwright"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    if attr == "_page":
                        obj.context.close()
                    else:
                        obj.close()
                except Exception:  # noqa: BLE001 —— 清理失败不影响主链路
                    pass
                setattr(self, attr, None)


class LocalXlsxDriver(CollectorDriver):
    """本地 xlsx 驱动：归档导出回填 / dry-run / 测试夹具，不登录、不需要凭据。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.sha256 = ""

    def fetch(self, config: CollectorConfig, start: date, end: date,
              secrets: dict[str, str] | None = None) -> list[RawRow]:
        if not self.path.is_absolute():
            self.path = DATAPRO_DIR / self.path
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise CollectorError("SOURCE_FILE_MISSING", type(exc).__name__) from exc
        self.sha256 = hashlib.sha256(payload).hexdigest()
        return PlaywrightDriver.parse_xlsx(payload)

    def close(self) -> None:
        return None


class XhrDriver(CollectorDriver):
    """路线 B（P1 占位）：接口路径/鉴权/日期参数尚未抓包，主动失败而不是臆造契约。"""

    def fetch(self, config: CollectorConfig, start: date, end: date,
              secrets: dict[str, str] | None = None) -> list[RawRow]:
        raise CollectorError("XHR_NOT_IMPLEMENTED", "XHR 接口待抓包实测，禁止臆造")

    def close(self) -> None:
        return None


def resolve_secrets(config: CollectorConfig) -> dict[str, str]:
    """fail-closed 解析两个 file: 引用；失败只抛引用名与状态，绝无明文。"""
    config.validate()
    out: dict[str, str] = {}
    for key, ref, label in (
        ("username", config.username_ref, "XIANZHI_USERNAME"),
        ("password", config.password_ref, "XIANZHI_PASSWORD"),
    ):
        state = secret_ref.health(ref)["state"]
        if state != "ok":
            raise CollectorError("SECRET_UNRESOLVED", f"{label} state={state}")
        try:
            out[key] = secret_ref.resolve(ref)
        except secret_ref.SecretRefError as exc:
            raise CollectorError("SECRET_UNRESOLVED",
                                 f"{label} state=unresolved ({type(exc).__name__})") from exc
    return out


def build_driver(config: CollectorConfig) -> CollectorDriver:
    """按配置选择驱动；默认路线 A，(file path) 回填走 LocalXlsxDriver。"""
    kind = str(config.extra.get("driver") or "playwright")
    if kind == "local_xlsx":
        path = config.extra.get("source_file") or ""
        if not path:
            raise CollectorError("BAD_CONFIG", "local_xlsx 驱动需要 source_file")
        return LocalXlsxDriver(path)
    if kind == "xhr":
        return XhrDriver()
    return PlaywrightDriver()


def report_source_url(config: CollectorConfig) -> str:
    """写入 ingest_batch/ODS 的来源页；去掉 query 防 token 泄漏。"""
    parts = urlsplit(config.site_url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def env_override(name: str, default: str = "") -> str:
    """仅用于非凭据配置（如 ODS_WRITER_URL 账号来自 env，不落源码）。"""
    return os.environ.get(name, default)
