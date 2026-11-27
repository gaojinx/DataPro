"""文档知识库 RAG（0020）：Markdown / 网页 / 纯文本 → 切块 → 向量化 → 混合检索。

定位：与 `recall.py` 的**元数据召回并行**，不替代它。元数据侧靠的是精确标识符
（字段名、枚举值），向量化只会把 `amount` 和 `total_amount` 混为一谈；文档侧靠的是
语义相近，正是向量检索擅长的。「先确定性问题用规则、语义问题用向量」是这里的核心分工。

三段流水：
1. `parse`：html 转标题化文本（h1→`# `），markdown/纯文本原样保留
2. `split_chunks`：按标题路径分段，段内按句末切窗（size/overlap），并记录 section
3. `search`：向量余弦 + 字符 2-gram 覆盖率融合，低于阈值整条丢弃

**阈值是这套系统的安全阀**：最高分不到 `DOC_RAG_MIN_SCORE` 就返回空，调用方据此回答
「未收录」。宁可说不知道，也不拿 0.2 分的片段拼一个看起来有依据的答案。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from array import array
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from app.config import settings
from app.services.embed_client import (EmbeddingError, EmbeddingUnavailable,
                                       embed_client)

logger = logging.getLogger("datapro.doc_rag")

SUPPORTED_TYPES = ("md", "text", "html")

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_SENT_END = "。！？；!?;\n"
_WS = re.compile(r"[ \t\u3000]+")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+")


# ---------------- 文本解析 ----------------

class _HtmlToText(HTMLParser):
    """极简 HTML→标题化文本：块级标签换行，h1~h6 转 `#`，script/style 整段丢弃。

    不引入 bs4/lxml：上传入口只有管理员使用，正文抽取够用即可；解析失败时上层
    回退为「去标签」的兜底路径，不会因此丢掉整篇文档。
    """

    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "pre", "blockquote",
             "table", "ul", "ol", "hr", "header", "footer"}
    # 只列**成对**标签：meta/link 这类自闭合标签若进 SKIP，depth 只增不减会把整篇文档吞掉
    SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK:
            self._parts.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n" + "#" * int(tag[1]) + " ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/>、<meta/>、<img/> 等自闭合标签不改变跳过深度
        if not self._skip_depth and tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK or tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def parse(content: str, doc_type: str = "md") -> str:
    """把原始内容转成「带 markdown 标题的纯文本」，供切分器识别章节。"""
    doc_type = (doc_type or "md").lower()
    if doc_type in ("html", "htm"):
        parser = _HtmlToText()
        try:
            parser.feed(content or "")
            parser.close()
            text = parser.text()
        except Exception as exc:  # noqa: BLE001 —— 解析失败退化为去标签
            logger.warning("HTML 解析失败，回退去标签：%s", exc)
            text = re.sub(r"<[^>]+>", " ", content or "")
        # 不再 unescape：HTMLParser(convert_charrefs=True) 已解码过实体，重复解码会把
        # 「&amp;amp;」错解成「&」，正文语义被改坏
        text = text
    else:
        text = content or ""
    return _normalize(text)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank > 1:      # 连续空行压成一行，避免切分时产生大量空块
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def detect_type(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".md", ".markdown"):
        return "md"
    if suffix in (".html", ".htm"):
        return "html"
    return "text"


# ---------------- 切分 ----------------

def _window(text: str, size: int, overlap: int) -> list[str]:
    """按 size 切窗、句末优先断开、相邻窗口 overlap 字符重叠。

    句末优先是为了不让一个口径定义被切成两半；找不到句末才硬切。
    overlap 必须小于 size，否则窗口不前进会死循环——这里显式夹紧。
    """
    text = (text or "").strip()
    if not text:
        return []
    size = max(80, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    if len(text) <= size:
        return [text]
    out: list[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            floor = start + int(size * 0.6)
            cut = -1
            for i in range(end, floor - 1, -1):
                if text[i - 1] in _SENT_END:
                    cut = i
                    break
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece and piece != (out[-1] if out else None):
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


def split_chunks(text: str, size: int | None = None,
                 overlap: int | None = None) -> list[dict]:
    """按标题路径分段后切块，返回 [{seq, section, text}]。

    section 会随块一起检索与展示：用户看到「退款口径 > 3.2 跨月退款」比看到
    「片段 7」更能判断该不该信这条引用。
    """
    size = int(size or settings.doc_chunk_size)
    overlap = int(overlap if overlap is not None else settings.doc_chunk_overlap)
    blocks: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current_section = ""
    current_lines: list[str] = []
    for line in (text or "").split("\n"):
        m = _HEADING.match(line)
        if m:
            if current_lines:
                blocks.append((current_section, current_lines))
                current_lines = []
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_section = " > ".join(t for _, t in stack)
            continue
        current_lines.append(line)
    if current_lines:
        blocks.append((current_section, current_lines))

    chunks: list[dict] = []
    seq = 0
    for section, lines in blocks:
        body = "\n".join(lines).strip()
        if len(body) < 12:      # 纯标题/单句过渡，不单独成块，避免检索到只有标题的噪声
            continue
        for piece in _window(body, size, overlap):
            chunks.append({"seq": seq, "section": section[:255], "text": piece})
            seq += 1
    return chunks


# ---------------- 向量编解码 ----------------

def vec_to_bytes(vec: list[float]) -> bytes:
    """float32 小端字节。显式指定字节序：跨架构（x86 部署 / ARM 开发机）读回来仍是同一个数。"""
    a = array("f", vec)
    import sys

    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def vec_from_bytes(raw: bytes | None) -> array | None:
    if not raw:
        return None
    a = array("f")
    try:
        a.frombytes(raw)
    except ValueError:
        return None
    import sys

    if sys.byteorder != "little":
        a.byteswap()
    return a


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


# ---------------- 词法打分 ----------------

def _grams(text: str) -> set[str]:
    t = _NON_WORD.sub("", (text or "").lower())
    if not t:
        return set()
    if len(t) == 1:
        return {t}
    return {t[i:i + 2] for i in range(len(t) - 1)}


def lexical_score(question: str, text: str, section: str = "", title: str = "") -> float:
    """查询 2-gram 覆盖率，标题/章节命中再加成。

    只做「查询覆盖了多少」而不是对称的 Jaccard：长 chunk 天然包含更多 gram，
    用 Jaccard 会把短而准的片段压下去。
    """
    q = _grams(question)
    if not q:
        return 0.0
    body = _grams(text)
    cov = len(q & body) / len(q)
    head = _grams(section + title)
    if q & head:
        cov = min(1.0, cov + 0.15)
    return round(cov, 4)


# ---------------- 服务 ----------------

def section_label(title: str, section: str) -> str:
    """展示用章节名：去掉与文档标题重复的前缀。

    文档正文的 H1 常常就是标题本身，切块后 section 变成「退款规范 > 跨月对账」，
    直接拼在《退款规范》后面会出现「退款规范 § 退款规范 > 跨月对账」的重复。
    """
    s = (section or "").strip()
    t = (title or "").strip()
    if s and t and s.startswith(t):
        s = s[len(t):].lstrip(" >").strip()
    return s


class DocRagService:
    def __init__(self) -> None:
        self._cache: list[dict] = []
        self._loaded_at = 0.0
        self._cache_key = ""

    # ---- 索引读取 ----
    def invalidate(self) -> None:
        self._loaded_at = 0.0
        self._cache_key = ""

    def _load_rows_sync(self) -> tuple[str, list[dict]]:
        """从库读出全部 chunk + 文档标题，返回（指纹, 行）。

        指纹 = 行数 + 最大 chunk id，写操作必然改变其中之一，因此本实例能立即感知；
        多实例下最多延迟 `DOC_RAG_CACHE_TTL` 秒，对知识库这种低频写入可接受。
        """
        from app.db.models import KbChunk, KbDoc
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            docs = {d.id: d for d in db.query(KbDoc).filter(KbDoc.status == "ready").all()}
            if not docs:
                return "empty", []
            rows = db.query(KbChunk).filter(KbChunk.doc_id.in_(list(docs))).all()
            items = []
            for r in rows:
                doc = docs.get(r.doc_id)
                if doc is None:
                    continue
                items.append({
                    "id": r.id, "doc_id": r.doc_id, "seq": r.seq,
                    "section": r.section or "", "text": r.text,
                    "title": doc.title, "category": doc.category,
                    "source": doc.source or "",
                    "vec": vec_from_bytes(r.vector), "dim": r.dim or 0,
                })
            key = f"{len(items)}:{max((i['id'] for i in items), default=0)}"
            return key, items

    def _index(self) -> list[dict]:
        """带 TTL 与失败回退的索引缓存：库抖动时继续用上一份，不把问答打成 0 命中。"""
        now = time.monotonic()
        if self._loaded_at and now - self._loaded_at < settings.doc_rag_cache_ttl:
            return self._cache
        try:
            key, items = self._load_rows_sync()
        except Exception as exc:  # noqa: BLE001 —— 库不可用不影响主链路
            logger.warning("读取文档索引失败，沿用上一份缓存（%d 块）：%s", len(self._cache), exc)
            self._loaded_at = now
            return self._cache
        self._cache, self._cache_key, self._loaded_at = items, key, now
        return self._cache

    # ---- 检索 ----
    async def search(self, question: str, top_k: int | None = None,
                     min_score: float | None = None) -> list[dict]:
        if not settings.doc_rag_enabled or not (question or "").strip():
            return []
        chunks = await asyncio.to_thread(self._index)
        if not chunks:
            return []
        top_k = int(top_k or settings.doc_rag_top_k)
        threshold = float(settings.doc_rag_min_score if min_score is None else min_score)

        q_vec: list[float] | None = None
        if embed_client.available():
            try:
                q_vec = (await embed_client.encode([question.strip()]))[0]
            except (EmbeddingUnavailable, EmbeddingError) as exc:
                # 向量路失败不阻断：词法分单独也能作答，只是召回面变窄
                logger.warning("问题向量化失败，本次仅用词法检索：%s", exc)
                q_vec = None

        w = max(0.0, min(1.0, float(settings.doc_rag_lexical_weight)))
        if q_vec is not None and chunks:
            # 所有块都没有可用向量（首次入库时向量化失败、或换了模型没 reindex）时，
            # 不能保留 75% 的向量权重去吃零分——那会让系统「静默什么都查不到」。
            # 此时按无向量处理，权重全给词法，至少还能按字面命中。
            usable = sum(1 for c in chunks if c["vec"] is not None
                         and (not c["dim"] or c["dim"] == len(q_vec)))
            if usable == 0:
                logger.warning("全部文档块无可用向量（未 reindex？），本次仅用词法检索")
                q_vec = None
        if q_vec is None:
            w = 1.0     # 没有向量就把权重全给词法，否则 0.75 的向量分会把上限压到 0.25
        scored: list[tuple[float, float, float, dict]] = []
        skipped_dim = 0
        for c in chunks:
            cos = 0.0
            if q_vec is not None and c["vec"] is not None:
                if c["dim"] and c["dim"] != len(q_vec):
                    skipped_dim += 1      # 换了 embedding 模型但没 reindex，维度对不上就跳过
                else:
                    cos = max(0.0, _dot(c["vec"], q_vec))
            lex = lexical_score(question, c["text"], c["section"], c["title"])
            final = (1 - w) * cos + w * lex
            if final >= threshold:
                scored.append((final, cos, lex, c))
        if skipped_dim:
            logger.warning("有 %d 个文档块维度与当前模型不一致，已跳过；请重新建立索引",
                           skipped_dim)

        scored.sort(key=lambda x: -x[0])
        hits = []
        for final, cos, lex, c in scored[:top_k]:
            hits.append({
                "doc_id": c["doc_id"], "title": c["title"], "section": c["section"],
                "text": c["text"], "score": round(final, 4),
                "vector_score": round(cos, 4), "lexical_score": lex,
                "seq": c["seq"], "source": c["source"], "category": c["category"],
            })
        if not hits:
            # 没命中就是没命中：调用方会据此回答「未收录」，这里留痕便于调阈值
            logger.info("文档检索无命中（阈值 %.2f，候选 %d 块）", threshold, len(chunks))
        return hits

    @staticmethod
    def render_context(hits: list[dict], max_chars: int | None = None) -> str:
        """拼【参考资料】块，带 [1][2] 编号供模型引用。

        超长时按分数从高到低填充并在块尾标注截断——不截断会把元数据槽位挤掉，
        模型反而更容易忽略高分的这一段。
        """
        limit = int(max_chars or settings.doc_rag_max_context_chars)
        parts: list[str] = []
        used = 0
        for i, h in enumerate(hits, 1):
            head = f"[{i}]《{h['title']}》"
            label = section_label(h.get("title", ""), h.get("section", ""))
            if label:
                head += f" § {label}"
            body = (h.get("text") or "").strip()
            piece = f"{head}\n{body}"
            if used + len(piece) > limit and parts:
                parts.append(f"（参考资料超过 {limit} 字，其余 {len(hits) - i + 1} 段已省略）")
                break
            parts.append(piece)
            used += len(piece)
        return "\n\n".join(parts)

    # ---- 写入 ----
    async def _embed_chunks(self, chunks: list[dict]) -> tuple[list[list[float] | None], str]:
        """尽力向量化：失败时返回全 None，让文档先可被词法检索，事后 reindex 补向量。"""
        if not embed_client.available():
            return [None] * len(chunks), ""
        try:
            vecs = await embed_client.encode([c["text"] for c in chunks])
            return vecs, (settings.embed_model or "")
        except (EmbeddingUnavailable, EmbeddingError) as exc:
            logger.warning("文档切块向量化失败，先按词法入库，可稍后 reindex：%s", exc)
            return [None] * len(chunks), ""

    async def ingest(self, title: str, content: str, source: str = "",
                     doc_type: str = "md", category: str = "通用",
                     tags: list[str] | None = None, created_by: str = "",
                     doc_id: str | None = None) -> dict:
        """新增或覆盖一篇文档。

        覆盖规则有两条：显式 `doc_id`（reindex 用）优先；否则按内容指纹判重——
        同一篇文档重复上传只保留一份，不会在检索时出现两份一模一样的高分片段。
        """
        doc_type = (doc_type or "md").lower()
        if doc_type not in SUPPORTED_TYPES:
            raise ValueError(f"不支持的文档类型：{doc_type}")
        if not (title or "").strip():
            raise ValueError("文档标题不能为空")
        text = parse(content, doc_type)
        if not text.strip():
            raise ValueError("文档内容为空或解析后无有效文本")
        chunks = split_chunks(text)
        if not chunks:
            raise ValueError("文档切分后无有效片段（内容过短或只有标题）")

        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        vecs, used_model = await self._embed_chunks(chunks)
        saved_id, replaced = await asyncio.to_thread(
            self._write_doc_sync, doc_id, title, source, doc_type, category, tags or [],
            sha, text, chunks, vecs, used_model, created_by)
        self.invalidate()
        return {"id": saved_id, "title": title, "chunk_count": len(chunks),
                "char_count": len(text), "embed_model": used_model,
                "embedded": sum(1 for v in vecs if v), "replaced": replaced}

    @staticmethod
    def _write_doc_sync(forced_id: str | None, title: str, source: str, doc_type: str,
                        category: str, tags: list[str], sha: str, text: str,
                        chunks: list[dict], vecs: list[list[float] | None],
                        used_model: str, created_by: str) -> tuple[str, bool]:
        from app.db.models import KbChunk, KbDoc
        from app.db.session import SessionLocal

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with SessionLocal() as db:
            old = None
            if forced_id:
                old = db.query(KbDoc).filter(KbDoc.id == forced_id).first()
            if old is None:
                old = db.query(KbDoc).filter(KbDoc.content_sha == sha).first()
            doc_id = old.id if old else (forced_id or uuid.uuid4().hex[:16])
            replaced = old is not None
            if old:
                db.query(KbChunk).filter(KbChunk.doc_id == doc_id).delete()
                old.title, old.source, old.doc_type = title, source, doc_type
                old.category, old.tags = category, ",".join(tags)
                old.chunk_count, old.char_count = len(chunks), len(text)
                old.content, old.content_sha = text, sha
                old.embed_model, old.status, old.error = used_model, "ready", None
                old.updated_at = now
            else:
                db.add(KbDoc(id=doc_id, title=title, source=source, doc_type=doc_type,
                             category=category, tags=",".join(tags), chunk_count=len(chunks),
                             char_count=len(text), content=text, content_sha=sha,
                             embed_model=used_model, status="ready", created_by=created_by))
            for c, v in zip(chunks, vecs):
                db.add(KbChunk(doc_id=doc_id, seq=c["seq"], section=c["section"],
                               text=c["text"], char_count=len(c["text"]),
                               vector=vec_to_bytes(v) if v else None,
                               dim=len(v) if v else 0))
            db.commit()
        return doc_id, replaced

    async def reindex(self, doc_id: str) -> dict:
        """重建某篇文档的切块与向量（换 embedding 模型、或首次入库时向量化失败后使用）。"""
        meta = await asyncio.to_thread(self._read_doc_meta_sync, doc_id)
        if meta is None:
            raise ValueError("文档不存在")
        title, content, source, category, doc_type = meta
        if not (content or "").strip():
            raise ValueError("该文档没有保存原文，无法重建索引，请重新上传")
        return await self.ingest(title, content, source, doc_type, category, [], "", doc_id)

    @staticmethod
    def _read_doc_meta_sync(doc_id: str) -> tuple[str, str, str, str, str] | None:
        from app.db.models import KbDoc
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            doc = db.query(KbDoc).filter(KbDoc.id == doc_id).first()
            if doc is None:
                return None
            return (doc.title, doc.content or "", doc.source or "",
                    doc.category or "通用", doc.doc_type or "md")

    # ---- 管理 ----
    async def list_docs(self) -> list[dict]:
        return await asyncio.to_thread(self._list_docs_sync)

    @staticmethod
    def _list_docs_sync() -> list[dict]:
        from app.db.models import KbDoc
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            rows = db.query(KbDoc).order_by(KbDoc.updated_at.desc()).all()
            return [{
                "id": d.id, "title": d.title, "source": d.source, "doc_type": d.doc_type,
                "category": d.category, "tags": [t for t in (d.tags or "").split(",") if t],
                "chunk_count": d.chunk_count, "char_count": d.char_count,
                "embed_model": d.embed_model, "status": d.status, "error": d.error,
                "created_by": d.created_by,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            } for d in rows]

    async def delete_doc(self, doc_id: str) -> bool:
        ok = await asyncio.to_thread(self._delete_doc_sync, doc_id)
        self.invalidate()
        return ok

    @staticmethod
    def _delete_doc_sync(doc_id: str) -> bool:
        from app.db.models import KbChunk, KbDoc
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            doc = db.query(KbDoc).filter(KbDoc.id == doc_id).first()
            if doc is None:
                return False
            db.query(KbChunk).filter(KbChunk.doc_id == doc_id).delete()
            db.delete(doc)
            db.commit()
            return True

    def stats(self) -> dict:
        """运维自检用：索引规模与维度分布，便于发现「换模型没 reindex」。"""
        chunks = self._index()
        dims = Counter(c["dim"] for c in chunks)
        return {"enabled": settings.doc_rag_enabled, "chunks": len(chunks),
                "embedded": sum(1 for c in chunks if c["vec"]),
                "dims": {str(k): v for k, v in dims.items()},
                "embedding_available": embed_client.available(),
                "embed_model": settings.embed_model or None,
                "min_score": settings.doc_rag_min_score}


doc_rag = DocRagService()
