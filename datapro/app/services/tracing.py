"""Langfuse 可观测（可选，F48）：未配置 Key 时全部为 no-op。

上报内容：一次提问一条 trace（问题/会话/用户/SQL/结果摘要）、每次模型调用一个
generation（模型/用量/耗时/版本）、反馈一个 score、节点事件若干。
任何上报异常只记日志，绝不影响取数主链路；只在应用关闭时 flush，不阻塞请求。
"""
import logging
import sys
from contextlib import contextmanager

from app.config import settings

logger = logging.getLogger("datapro.tracing")

_client = None
_initialized = False


def _build():
    if not (settings.langfuse_enabled and settings.langfuse_public_key
            and settings.langfuse_secret_key):
        return None
    try:
        from langfuse import Langfuse
        client = Langfuse(public_key=settings.langfuse_public_key,
                          secret_key=settings.langfuse_secret_key,
                          host=settings.langfuse_host,
                          environment=settings.app_env)
        logger.info("Langfuse 可观测已启用：%s", settings.langfuse_host)
        return client
    except Exception as exc:  # noqa: BLE001 —— 观测不能影响启动
        logger.warning("Langfuse 初始化失败，观测关闭：%s", exc)
        return None


def client():
    global _client, _initialized
    if not _initialized:
        _client = _build()
        _initialized = True
    return _client


def enabled() -> bool:
    return client() is not None


def reset() -> None:
    """测试用：清掉缓存的客户端（下次调用重新按配置构建）。"""
    global _client, _initialized
    try:
        if _client is not None:
            _client.flush()
    except Exception:  # noqa: BLE001
        pass
    _client = None
    _initialized = False


class NullTrace:
    """未启用时的空句柄。"""
    trace_id = None

    def event(self, *args, **kwargs):
        return None

    def finish(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Trace:
    def __init__(self, lf, question: str, session_id: str, user_id: str, tags: list[str]):
        self._lf = lf
        self._question = question
        self._session_id = session_id
        self._user_id = user_id
        self._tags = tags
        self._attr_cm = None
        self._obs_cm = None
        self._obs = None
        self.trace_id: str | None = None

    def __enter__(self):
        try:
            from langfuse import propagate_attributes
            self._attr_cm = propagate_attributes(session_id=self._session_id or None,
                                                 user_id=self._user_id or None,
                                                 tags=self._tags or None)
            self._attr_cm.__enter__()
            self._obs_cm = self._lf.start_as_current_observation(
                name="chat", as_type="chain", input={"question": self._question})
            self._obs = self._obs_cm.__enter__()
            self.trace_id = self._lf.get_current_trace_id()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Langfuse trace 开启失败：%s", exc)
            self._obs = None
        return self

    def event(self, name: str, **data) -> None:
        if self._obs is None:
            return
        try:
            self._lf.create_event(name=name, **data)
        except Exception:  # noqa: BLE001
            pass

    def finish(self, output=None, metadata=None, level: str = "DEFAULT") -> None:
        if self._obs is None:
            return
        try:
            self._lf.update_current_span(output=output, metadata=metadata, level=level)
        except Exception:  # noqa: BLE001
            pass

    def __exit__(self, *exc):
        if self._obs_cm is not None:
            try:
                self._obs_cm.__exit__(*exc)
            except Exception:  # noqa: BLE001
                pass
        if self._attr_cm is not None:
            try:
                self._attr_cm.__exit__(*exc)
            except Exception:  # noqa: BLE001
                pass
        return False


def trace(question: str, session_id: str = "", user_id: str = "",
          tags: list[str] | None = None):
    lf = client()
    if lf is None:
        return NullTrace()
    return Trace(lf, question, session_id, user_id, tags or [])


class NullGeneration:
    def record(self, *args, **kwargs):
        return None


class Generation:
    def __init__(self, lf, handle):
        self._lf = lf
        self._handle = handle

    def record(self, output=None, usage: dict | None = None, metadata=None,
               level: str = "DEFAULT") -> None:
        try:
            self._lf.update_current_generation(output=output, usage_details=usage,
                                               metadata=metadata, level=level)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def generation(name: str, model: str, input_data=None):
    """包住一次模型调用；未启用时退化为空操作。"""
    lf = client()
    if lf is None:
        yield NullGeneration()
        return
    obj = None
    try:
        obj = lf.start_as_current_observation(name=name, as_type="generation",
                                              model=model, input=input_data)
        obj.__enter__()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse generation 开启失败：%s", exc)
        yield NullGeneration()
        return
    try:
        yield Generation(lf, obj)
    except Exception:
        try:
            obj.__exit__(*sys.exc_info())
        except Exception:  # noqa: BLE001
            pass
        raise
    else:
        try:
            obj.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def score(trace_id: str | None, name: str, value: float, comment: str | None = None) -> None:
    lf = client()
    if lf is None or not trace_id:
        return
    try:
        lf.create_score(name=name, value=value, trace_id=trace_id,
                        comment=comment, data_type="NUMERIC")
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    lf = client()
    if lf is None:
        return
    try:
        lf.flush()
    except Exception:  # noqa: BLE001
        pass
