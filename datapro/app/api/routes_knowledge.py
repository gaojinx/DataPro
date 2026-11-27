"""知识库：结构化条目（术语/口径）+ 文档知识库（RAG）。

- 条目：`knowledge.json`，短、精确、按别名匹配
- 文档：`kb_doc`/`kb_chunk`，长文本切块向量化，按语义 + 词法混合检索
两者都只对管理员开放写入；对话内召回走服务层，普通用户不受影响。
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import settings
from app.services.auth import AuthUser, require_permission
from app.services.doc_rag import SUPPORTED_TYPES, detect_type, doc_rag
from app.services.knowledge_base import knowledge_base

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

MAX_DOC_BYTES = 2 * 1024 * 1024      # 单篇 2MB：再大就该走文件解析而不是知识库问答


class KnowledgeIn(BaseModel):
    term: str
    aliases: list[str] = []
    category: str = "通用"
    definition: str = ""
    formula: str = ""


class DocIn(BaseModel):
    title: str
    content: str
    source: str = ""
    doc_type: str = "md"
    category: str = "通用"
    tags: list[str] = []


@router.get("")
async def list_knowledge(q: str = "", _: AuthUser = Depends(require_permission("knowledge.manage"))):
    return knowledge_base.list(q)


@router.post("")
async def add_knowledge(body: KnowledgeIn,
                        _: AuthUser = Depends(require_permission("knowledge.manage"))):
    return knowledge_base.add(body.model_dump())


@router.put("/{item_id}")
async def update_knowledge(item_id: str, body: dict,
                           _: AuthUser = Depends(require_permission("knowledge.manage"))):
    result = knowledge_base.update(item_id, body)
    if result is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return result


@router.delete("/{item_id}")
async def delete_knowledge(item_id: str,
                           _: AuthUser = Depends(require_permission("knowledge.manage"))):
    if not knowledge_base.delete(item_id):
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return {"deleted": item_id}


# ---------------- 文档知识库（0020） ----------------

@router.get("/docs")
async def list_docs(_: AuthUser = Depends(require_permission("knowledge.manage"))):
    return await doc_rag.list_docs()


@router.get("/docs/stats")
async def doc_stats(_: AuthUser = Depends(require_permission("knowledge.manage"))):
    """索引自检：块数、已向量化块数、维度分布、embedding 是否可用。

    换 embedding 模型后如果只看到 `dims` 里有两个不同维度，说明旧文档没 reindex。
    """
    return doc_rag.stats()


@router.get("/docs/search")
async def preview_search(q: str, top_k: int = 5,
                         _: AuthUser = Depends(require_permission("knowledge.manage"))):
    """检索预览：管理员调整阈值/切分参数时，先用真实问题看看命中与分数。"""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")
    hits = await doc_rag.search(q, top_k=max(1, min(top_k, 20)), min_score=0.0)
    return {"question": q, "hits": hits, "threshold": settings.doc_rag_min_score}


@router.post("/docs")
async def create_doc(body: DocIn, user: AuthUser = Depends(require_permission("knowledge.manage"))):
    try:
        return await doc_rag.ingest(body.title, body.content, body.source,
                                    body.doc_type, body.category, body.tags, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/docs/upload")
async def upload_doc(file: UploadFile = File(...),
                     title: str = "", category: str = "通用",
                     user: AuthUser = Depends(require_permission("knowledge.manage"))):
    raw = await file.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise HTTPException(status_code=413, detail=f"文档超过 {MAX_DOC_BYTES // 1024} KB 上限")
    doc_type = detect_type(file.filename or "")
    if doc_type not in SUPPORTED_TYPES:
        raise HTTPException(status_code=400, detail="仅支持 .md / .txt / .html")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # GBK 编码的旧文档很常见：解不出来就按 GB18030 再试一次，仍失败则明确报错
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="文件编码无法识别，请转存为 UTF-8") from exc
    try:
        return await doc_rag.ingest(title or (file.filename or "未命名文档"),
                                    text, file.filename or "", doc_type,
                                    category, [], user.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/docs/{doc_id}/reindex")
async def reindex_doc(doc_id: str,
                      _: AuthUser = Depends(require_permission("knowledge.manage"))):
    try:
        return await doc_rag.reindex(doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/docs/{doc_id}")
async def delete_doc(doc_id: str,
                     _: AuthUser = Depends(require_permission("knowledge.manage"))):
    if not await doc_rag.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"deleted": doc_id}
