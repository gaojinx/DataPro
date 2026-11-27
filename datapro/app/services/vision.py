"""图片理解：把上传的图片交给视觉模型，转成可注入 Prompt 的文字描述。

为什么必须单独一层：主模型（V4-Flash / V4-Pro）不接受图片，接口会直接 400；
图片只有交给支持视觉的模型才能读。而视觉模型目前是实验性的，所以这一层必须满足
三个条件——**可关闭、失败可降级、绝不假装看过**。

设计要点：
- 图片按文件**真实内容**判断格式（用户常把 jpg 改名成 png，声明 MIME 不可信）。
- 描述文本会被下游当成事实依据，因此提示词明确要求「看不清就写看不清，不得推测数字」。
- 未配置视觉模型时不报错、不调模型，直接返回 None，由调用方给出诚实说明。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import time

from app.config import settings

logger = logging.getLogger("datapro.vision")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

SYSTEM = (
    "你是图片解析助手。你的输出会被另一个系统当作事实依据使用，因此只描述你真正看到的内容。\n"
    "规则：\n"
    "1. 第一句概括图片是什么：截图 / 表格 / 图表 / 文档 / 照片。\n"
    "2. 若含表格或数据，用 Markdown 表格原样转录可见的表头与数字；看不清的单元格写「看不清」。\n"
    "3. 若含报错、日志或文字段落，逐字转录关键部分，不要改写。\n"
    "4. 禁止出现图片中不存在的数字、结论或建议；不要推测被截断的内容。\n"
    "5. 不超过 800 字，不要复述这些规则。"
)


class VisionUnsupported(Exception):
    """当前模型不支持图片输入（配置问题，重试无用）。"""


class VisionError(Exception):
    """调用失败（网络/限流/超时），已按策略重试过。"""


def sniff_image_mime(data: bytes) -> str | None:
    """按魔数判断图片类型；不是图片返回 None。

    不看扩展名：`截图.png` 实际是 JPEG、或者把 .exe 改名成 .png 的情况都很常见，
    交给上游只会得到 400，不如本地先判掉。
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_image_entry(entry: dict) -> bool:
    ext = (entry.get("ext") or "").lower()
    mime = (entry.get("mime") or "").lower()
    return ext in IMAGE_EXT or mime.startswith("image/")


class VisionService:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], object] = {}

    def resolve(self) -> dict | None:
        """视觉模型配置：优先 VISION_*，缺省复用 LLM 注册表的 base_url 与 Key。

        复用是刻意的：管理员换 Key 或换供应商时不用改两处；模型名单独配，
        因为视觉模型和文本模型通常不是同一个 model id。
        """
        if not settings.vision_enabled:
            return None
        model = (settings.vision_model or "").strip()
        if not model:
            return None
        base_url = (settings.vision_base_url or "").strip()
        key = ""
        ref = (settings.vision_api_key_ref or "").strip()
        if ref:
            from app.services.secret_ref import SecretRefError, resolve

            try:
                key = resolve(ref)
            except SecretRefError as exc:
                logger.warning("视觉模型密钥引用解析失败：%s", exc)
                return None
        if not base_url or not key:
            from app.services.llm_registry import llm_registry

            api = llm_registry.default_active()
            if api:
                base_url = base_url or (api.get("base_url") or "")
                key = key or (api.get("api_key") or "")
        if not base_url or not key:
            return None
        return {"base_url": base_url, "api_key": key, "model": model}

    def available(self) -> bool:
        if self.resolve() is None:
            return False
        try:
            import openai  # noqa: F401
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("openai SDK 不可用，图片理解关闭：%s", exc)
            return False

    def _get_client(self, cfg: dict):
        key = (cfg["base_url"], cfg["api_key"])
        if key not in self._clients:
            from openai import AsyncOpenAI

            self._clients[key] = AsyncOpenAI(
                base_url=cfg["base_url"], api_key=cfg["api_key"], max_retries=0)
        return self._clients[key]

    async def _call(self, cfg: dict, blocks: list[dict], prompt: str) -> str:
        client = self._get_client(cfg)
        last: Exception | None = None
        attempts = max(1, int(settings.vision_max_retries))
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                resp = await client.chat.completions.create(
                    model=cfg["model"],
                    messages=[
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [{"type": "text", "text": prompt}, *blocks]},
                    ],
                    timeout=settings.vision_timeout,
                )
                text = (resp.choices[0].message.content or "").strip()
                usage = getattr(resp, "usage", None)
                logger.info("视觉解析 ok model=%s images=%d tokens=%s latency_ms=%d",
                            cfg["model"], len(blocks), getattr(usage, "total_tokens", "?"),
                            round((time.monotonic() - started) * 1000))
                if not text:
                    raise VisionError("视觉模型返回空内容")
                return text
            except Exception as exc:  # noqa: BLE001 —— 先分类，再决定是否重试
                last = exc
                msg = str(exc)
                if "does not support image" in msg or "image" in msg.lower() and "400" in msg:
                    # 模型不支持图片是配置错误，重试只会重复烧钱
                    raise VisionUnsupported(msg) from exc
                if attempt == attempts - 1:
                    break
                backoff = min(6.0, 0.8 * (2 ** attempt)) * (0.5 + random.random())
                logger.info("视觉解析第 %d/%d 次失败，%.2fs 后重试：%s",
                            attempt + 1, attempts, backoff, exc)
                await asyncio.sleep(backoff)
        raise VisionError(f"视觉模型调用失败：{last}")

    async def describe(self, entries: list[dict], question: str = "") -> dict:
        """把会话内图片转成一段文字描述。

        返回 {"text": 描述, "used": [文件名], "skipped": [{"name","reason"}]}；
        `text` 为空串表示没有任何图片被成功解析——调用方据此给出诚实说明，
        **不要**把它当成「图片里没有内容」。
        """
        result: dict = {"text": "", "used": [], "skipped": [], "model": ""}
        images = [e for e in (entries or []) if is_image_entry(e)]
        if not images:
            return result

        cfg = self.resolve()
        if cfg is None or not self.available():
            result["skipped"] = [{"name": e.get("name", ""), "reason": "视觉模型未配置"} for e in images]
            return result

        from app.services.file_store import file_store

        blocks: list[dict] = []
        for entry in images[: max(1, int(settings.vision_max_images))]:
            name = entry.get("name", "")
            # 必须按 file_id 重新取完整条目：调用方传来的通常是 `_public` 视图，
            # 里面没有 stored_name（有意不暴露给前端），直接 content_of 会读不到文件。
            full = file_store.get(str(entry.get("file_id") or ""))
            if not full:
                result["skipped"].append({"name": name, "reason": "文件已不存在"})
                continue
            try:
                data = file_store.content_of(full)
            except Exception as exc:  # noqa: BLE001 —— 文件被删/路径异常，跳过该张
                result["skipped"].append({"name": name, "reason": f"读取失败：{exc}"})
                continue
            mime = sniff_image_mime(data)
            if mime is None:
                # 扩展名说它是图片，内容却不是：多半是改名文件或损坏文件
                result["skipped"].append({"name": name, "reason": "文件内容不是有效图片"})
                continue
            if len(data) > settings.vision_max_bytes:
                result["skipped"].append({"name": name, "reason": "图片超过大小上限"})
                continue
            b64 = base64.b64encode(data).decode("ascii")
            blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            result["used"].append(name)

        extra = len(images) - min(len(images), int(settings.vision_max_images))
        if extra > 0:
            result["skipped"].append({"name": f"另有 {extra} 张", "reason": "超过单次解析张数上限"})

        if not blocks:
            return result

        prompt = ("请按规则解析这些图片。" if len(blocks) == 1
                  else f"请按规则逐张解析这 {len(blocks)} 张图片，并用「图1/图2」标出每段属于哪张。")
        if (question or "").strip():
            prompt += f"\n用户原始问题：{question.strip()[:500]}"

        try:
            result["text"] = await self._call(cfg, blocks, prompt)
            result["model"] = cfg["model"]
        except VisionUnsupported as exc:
            logger.error("当前视觉模型不支持图片输入（检查 VISION_MODEL）：%s", exc)
            result["skipped"].append({"name": "全部", "reason": "当前模型不支持图片输入"})
        except VisionError as exc:
            logger.warning("图片解析失败，按未识别处理：%s", exc)
            result["skipped"].append({"name": "全部", "reason": "视觉模型调用失败"})
        return result


vision_service = VisionService()
