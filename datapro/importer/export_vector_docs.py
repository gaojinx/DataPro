"""把元数据/口径/知识库导出为向量库文档（D05 → 向量库 / A05 索引输入）。

产物 JSONL 每行：{uid, type, text, payload}
- uid：`table:xxx` / `caliber:M003` / `knowledge:xxx`，向量库主键
- text：送 embedding 的文本（**复用 `app/services/vector_recall` 的同一套构造函数**，
  保证「进程内索引」与「Milvus 文档」是同一份文本，否则两种部署形态召回结果会漂移）
- payload：原样字段，供召回后组装提示词与依赖表

粒度：表/口径/知识条目各一条文档，**额外的二级文档是字段**（`type=column`，
`uid=column:{table}.{col}`），不是把表文档切碎。表文档平均约 180 字，切碎会让 SQL
生成阶段拿不到完整字段；字段文档单独建，是为了「问题提到某个字段时能定位到它」。

用法：
  python -m importer.export_vector_docs --out data/vector_docs.jsonl
"""
import argparse
import json
from pathlib import Path


def build_docs(tables: list[dict], calibers: list[dict],
               knowledge: list[dict] | None = None) -> list[dict]:
    from app.services.vector_recall import (caliber_doc_text, column_doc_text,
                                            flatten_columns, knowledge_doc_text,
                                            table_doc_text)

    docs: list[dict] = []
    for t in tables:
        docs.append({
            "uid": f"table:{t.get('table_name', '')}",
            "type": "table",
            "text": table_doc_text(t),
            "payload": t,
        })
    # 二级文档：字段一条一篇。挂在表文档之后，便于按 type 过滤统计
    for cm in flatten_columns(tables):
        docs.append({
            "uid": f"column:{cm['table_name']}.{cm['name']}",
            "type": "column",
            "text": column_doc_text(cm),
            "payload": cm,
        })
    for c in calibers:
        docs.append({
            "uid": f"caliber:{c.get('metric_id', '')}",
            "type": "caliber",
            "text": caliber_doc_text(c),
            "payload": c,
        })
    for k in knowledge or []:
        docs.append({
            "uid": f"knowledge:{k.get('id', k.get('term', ''))}",
            "type": "knowledge",
            "text": knowledge_doc_text(k),
            "payload": k,
        })
    return docs