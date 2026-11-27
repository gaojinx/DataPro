"""Embedding 客户端：OpenAI 兼容 /embeddings 接口，供文档 RAG 使用。

设计取舍：
- **配置不齐或调用失败时抛 `EmbeddingUnavailable`，绝不返回零向量**。零向量与任何
  文本的余弦都是 0，会让检索「无报错地全部不命中」，比直接降级更难排查。
- 密钥只走 `embed_api_key_ref`（env:/file:/kms://），与 `llm_api.api_key_ref` 同一套
  解析；不接受明文配置项，避免在 .env 里多一处泄漏面。
- 批量 + 有限重试：embedding 是幂等的（同文本同向量），重试安全；重试次数与退避
  都有上限，避免上游 5xx 时把一次问答拖成几十秒。
- 结果 L2 归一化后返回：检索只用点积，省掉每次算模长。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import time

from app.config import settings
from app.services.metrics import metrics

logger = logging.getLogger("datapro.embed")

_CACHE_TTL = 300.0
_CACHE_MAX = 512


class EmbeddingUnavailable(Exception):
    """未配置或依赖缺失：调用方应降级为词法检索，而不是报错给用户。"""


class EmbeddingError(Exception):
    """已配置但调用失败（网络/鉴权/限流）：调用方同样降级，但需记日志与指标。"""


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 1e-12:
        raise EmbeddingError("embedding 返回零向量")
    return [v / norm for v in vec]


class EmbedClient:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], object] = {}
        self._cache: dict[str, tuple[float, list[float]]] = {}

    def resolve(self) -> dict | None:
        """生效配置：base_url + model + 可解析的 Key 三者齐备才返回。"""
        base_url = (settings.embed_base_url or "").strip()
        model = (settings.embed_model or "").strip()
        if not base_url or not model:
            return None
        key = ""
        ref = (settings.embed_api_key_ref or "").strip()
        if ref:
            from app.services.secret_ref import SecretRefError, resolve

            try:
                key = resolve(ref)
            except SecretRefError as exc:
                logger.warning("embedding 密钥引用解析失败，文档 RAG 降级为词法检索：%s", exc)
                return None
        if not key:
            # 部分自建服务（如本地 vLLM/Ollama 的兼容层）不校验 Key，允许空串但需显式声明
            key = "EMPTY"
        return {"base_url": base_url, "model": model, "api_key": key}

    def available(self) -> bool:
        if self.resolve() is None:
            return False
        try:
            import openai  # noqa: F401
            return True
        except Exception as exc:  # noqa: BLE001 —— 依赖缺失按不可用降级
            logger.warning("openai SDK 不可用，文档 RAG 降级为词法检索：%s", exc)
            return False

    def _get_client(self, cfg: dict):
        key = (cfg["base_url"], cfg["api_key"])
        if key not in self._clients:
            from openai import AsyncOpenAI

            self._clients[key] = AsyncOpenAI(
                base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=0)
        return self._clients[key]

    def _cache_get(self, text: str) -> list[float] | None:
        item = self._cache.get(text)
        if not item:
            return None
        expire_at, vec = item
        if expire_at < time.monotonic():
            self._cache.pop(text, None)
            return None
        return vec

    def _cache_put(self, text: str, vec: list[float]) -> None:
        if len(self._cache) >= _CACHE_MAX:
            # 简单淘汰：清掉最早的一批，避免无界增长；embedding 缓存只为省重复调用
            for k in list(self._cache)[: _CACHE_MAX // 4]:
                self._cache.pop(k, None)
        self._cache[text] = (time.monotonic() + _CACHE_TTL, vec)

    async def _call_batch(self, cfg: dict, batch: list[str]) -> list[list[float]]:
        client = self._get_client(cfg)
        last: Exception | None = None
        attempts = max(1, int(settings.embed_max_retries))
        for attempt in range(attempts):
            try:
                resp = await client.embeddings.create(
                    model=cfg["model"], input=batch, timeout=settings.embed_timeout)
                data = sorted(resp.data, key=lambda d: d.index)
                if len(data) != len(batch):
                    raise EmbeddingError(f"返回条数不符：期望 {len(batch)} 实得 {len(data)}")
                return [_l2([float(x) for x in d.embedding]) for d in data]
            except Exception as exc:  # noqa: BLE001 —— 统一归类后决定是否重试
                last = exc
                if attempt == attempts - 1:
                    break
                # 指数退避 + 抖动：多个实例同时被上游限流时不至于同频重试
                backoff = min(8.0, 0.5 * (2 ** attempt)) * (0.5 + random.random())
                logger.info("embedding 第 %d/%d 次失败，%.2fs 后重试：%s",
                            attempt + 1, attempts, backoff, exc)
                await asyncio.sleep(backoff)
        raise EmbeddingError(f"embedding 调用失败：{last}")

    async def encode(self, texts: list[str]) -> list[list[float]]:
        """批量编码，返回与输入等长、已归一化的向量列表。

        空串不送上游（部分服务会报 400），直接给一个「不可能命中」的占位：
        这里不返回零向量，而是抛错——调用方要么给正文，要么不要把空串送进来。
        """
        if not texts:
            return []
        if any(not (t or "").strip() for t in texts):
            raise EmbeddingError("存在空文本，拒绝编码")
        cfg = self.resolve()
        if cfg is None:
            raise EmbeddingUnavailable("未配置 embedding 服务")
        if not self.available():
            raise EmbeddingUnavailable("openai SDK 不可用")

        out: list[list[float] | None] = [None] * len(texts)
        pending: list[int] = []
        for i, t in enumerate(texts):
            hit = self._cache_get(t)
            if hit is not None:
                out[i] = hit
            else:
                pending.append(i)

        started = time.monotonic()
        size = max(1, int(settings.embed_batch_size))
        try:
            for start in range(0, len(pending), size):
                idxs = pending[start:start + size]
                vecs = await self._call_batch(cfg, [texts[i] for i in idxs])
                model_dim = len(vecs[0]) if vecs else 0
                if model_dim and settings.embed_dim and model_dim != settings.embed_dim:
                    # 不阻断：以实际维度为准落库（dim 列会记录），只提示配置与模型不符
                    logger.warning("embedding 维度 %d 与配置 EMBED_DIM=%d 不一致，以实际为准",
                                   model_dim, settings.embed_dim)
                for i, vec in zip(idxs, vecs):
                    out[i] = vec
                    self._cache_put(texts[i], vec)
        except (EmbeddingUnavailable, EmbeddingError):
            metrics.inc("datapro_embed_total", result="error")
            raise
        metrics.inc("datapro_embed_total", result="ok")
        logger.info("embedding ok model=%s n=%d cached=%d latency_ms=%d",
                    cfg["model"], len(texts), len(texts) - len(pending),
                    round((time.monotonic() - started) * 1000))
        return [v if v is not None else [] for v in out]

    @staticmethod
    def fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


embed_client = EmbedClient()
