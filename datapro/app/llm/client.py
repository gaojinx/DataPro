"""LLM 客户端：openai 兼容协议直连，不套框架（选型原则 §2）。

B09 真实 LLM 接入 / B17 注册表联动：
- 每次调用实时解析配置：注册表默认启用项（有 Key）优先，其次 .env
- 管理员在设置页切换默认 API 或修改 Key，下一次请求即生效，无需重启
- 结构化 JSON 输出，解析失败返回 None（状态机重试/升级）
- 超时抛 LLMTimeoutError（上层转 E005）；主模型失败时按 llm_fallback_model 同源重试一次（B15）
"""
import json
import logging
import re
import time

from app.config import settings
from app.services import tracing

logger = logging.getLogger("datapro.llm")

_FENCE = re.compile(r"```(?:json)?|```")
_SDK_WARNED = False      # SDK 缺失只告警一次，避免每个请求刷日志


class LLMTimeoutError(Exception):
    """模型服务超时（映射 E005）。"""


class LLMNotConfiguredError(Exception):
    """无可用 API 配置且未开启 mock 降级。"""


def _is_timeout(exc: Exception) -> bool:
    return "Timeout" in type(exc).__name__ or isinstance(exc, TimeoutError)


def _parse_json(raw: str) -> dict | None:
    """解析模型输出：剥离 markdown 围栏，失败时兜底截取首尾大括号。"""
    text = _FENCE.sub("", raw or "").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                pass
    logger.warning("LLM 输出非合法 JSON，前 200 字: %s", text[:200])
    return None


class LLMClient:
    def __init__(self):
        # 按 (base_url, api_key) 缓存客户端；配置变更自动换实例
        self._clients: dict[tuple[str, str], object] = {}

    def resolve(self) -> dict | None:
        """当前生效配置：注册表默认项优先，回退 .env。"""
        from app.services.llm_registry import llm_registry

        api = llm_registry.default_active()
        if api and api.get("api_key"):
            return {"base_url": api["base_url"], "api_key": api["api_key"],
                    "model": api["model"], "source": f"registry:{api['name']}"}
        if settings.llm_api_key:
            return {"base_url": settings.llm_base_url, "api_key": settings.llm_api_key,
                    "model": settings.llm_model, "source": "env"}
        return None

    def available(self) -> bool:
        """可用 = 配了 Key 且 SDK 能导入。

        只看配置不看依赖是错的：缺 openai 包时每次调用都抛 ImportError，
        结果整条链路退化成「模型没给 SQL → 转人工」，用户看到的是系统不可用，
        而不是清晰的「未配置 LLM」提示。这里把依赖也纳入判定，让降级路径生效。
        """
        if self.resolve() is None:
            return False
        global _SDK_WARNED
        try:
            import openai  # noqa: F401
            return True
        except Exception as exc:  # noqa: BLE001 —— 依赖缺失按不可用处理
            if not _SDK_WARNED:
                logger.error("LLM SDK 不可用（openai 未安装？），已降级为口径模板：%s", exc)
                _SDK_WARNED = True
            return False

    def _get_client(self, cfg: dict):
        key = (cfg["base_url"], cfg["api_key"])
        if key not in self._clients:
            from openai import AsyncOpenAI

            self._clients[key] = AsyncOpenAI(
                base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=0,
            )
        return self._clients[key]

    async def structured_call(self, prompt: str, system: str = "",
                              tool_version: str = "v0", temperature: float = 0.0) -> dict | None:
        cfg = self.resolve()
        if cfg is None:
            if settings.llm_mock:
                logger.info("LLM 无可用配置，降级 mock")
                return self._mock_response(prompt)
            raise LLMNotConfiguredError("未配置可用的 LLM API，请在设置中添加")

        models = [cfg["model"]]
        fallback = settings.llm_fallback_model
        if fallback and fallback != cfg["model"]:
            models.append(fallback)

        last_err: Exception | None = None
        for idx, model in enumerate(models):
            try:
                return await self._call_once(cfg, model, prompt, system, tool_version, temperature)
            except LLMTimeoutError:
                raise
            except Exception as e:  # 限流/鉴权/网络等统一按模型失败处理
                last_err = e
                logger.warning("LLM 调用失败 model=%s (%d/%d): %s", model, idx + 1, len(models), e)
        logger.error("LLM 全部模型失败: %s", last_err)
        return None

    async def _call_once(self, cfg: dict, model: str, prompt: str, system: str,
                         tool_version: str, temperature: float = 0.0) -> dict | None:
        client = self._get_client(cfg)
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        with tracing.generation(name=f"llm:{tool_version}", model=model,
                                input_data={"system": system[:2000], "prompt": prompt}) as gen:
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    timeout=settings.timeout_generate,
                    temperature=temperature,
                )
            except Exception as e:
                if _is_timeout(e):
                    raise LLMTimeoutError(str(e)) from e
                raise
            data = _parse_json(resp.choices[0].message.content or "")
            usage = getattr(resp, "usage", None)
            gen.record(output=data,
                       usage={"input": getattr(usage, "prompt_tokens", 0) or 0,
                              "output": getattr(usage, "completion_tokens", 0) or 0,
                              "total": getattr(usage, "total_tokens", 0) or 0},
                       level="DEFAULT" if data is not None else "WARNING")
        logger.info("LLM ok source=%s model=%s tokens=%s prompt_v=%s latency_ms=%d",
                    cfg["source"], model, getattr(resp.usage, "total_tokens", "?"),
                    tool_version, round((time.monotonic() - started) * 1000))
        return data

    async def chat(self, prompt: str, system: str = "") -> str | None:
        """纯文本对话（非 JSON）：用于寒暄/能力说明类回答。"""
        cfg = self.resolve()
        if cfg is None:
            raise LLMNotConfiguredError("未配置可用的 LLM API，请在设置中添加")

        models = [cfg["model"]]
        fallback = settings.llm_fallback_model
        if fallback and fallback != cfg["model"]:
            models.append(fallback)

        last_err: Exception | None = None
        for idx, model in enumerate(models):
            try:
                return await self._chat_once(cfg, model, prompt, system)
            except LLMTimeoutError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM chat 失败 model=%s (%d/%d): %s", model, idx + 1, len(models), e)
        logger.error("LLM chat 全部模型失败: %s", last_err)
        return None

    async def _chat_once(self, cfg: dict, model: str, prompt: str, system: str) -> str | None:
        client = self._get_client(cfg)
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        started = time.monotonic()
        with tracing.generation(name="llm:chat", model=model,
                                input_data={"system": system[:2000], "prompt": prompt}) as gen:
            try:
                resp = await client.chat.completions.create(
                    model=model, messages=messages,
                    timeout=settings.timeout_generate, temperature=0.3,
                )
            except Exception as e:
                if _is_timeout(e):
                    raise LLMTimeoutError(str(e)) from e
                raise
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            gen.record(output=text,
                       usage={"input": getattr(usage, "prompt_tokens", 0) or 0,
                              "output": getattr(usage, "completion_tokens", 0) or 0,
                              "total": getattr(usage, "total_tokens", 0) or 0})
        logger.info("LLM chat ok source=%s model=%s tokens=%s latency_ms=%d",
                    cfg["source"], model, getattr(resp.usage, "total_tokens", "?"),
                    round((time.monotonic() - started) * 1000))
        return text or None

    def _mock_response(self, prompt: str) -> dict | None:
        return {"sql": None, "explanation": "", "assumptions": [], "confidence": "low"} \
            if "生成SQL" in prompt else {"answer": "mock", "confidence": "high"}


llm_client = LLMClient()
