"""把向量库文档灌入 Milvus（A05）。

前置：
- 安装 `pymilvus`；编码器按 `VECTOR_ENCODER` 选择（api 复用 embed_client，local 需
  openvino + transformers 并配好 `VECTOR_IR_PATH`）
- 先 `python -m importer.export_vector_docs` 生成 data/vector_docs.jsonl
- VECTOR_RECALL_ENABLED=true，VECTOR_BACKEND=milvus

用法：
  python -m importer.ingest_vectors --dry-run
  python -m importer.ingest_vectors --recreate

注意：`VECTOR_BACKEND=memory`（默认）不需要本脚本，向量在首次召回时自动编码并落
`meta_embedding` 缓存；只有要上 Milvus 才需要灌库。
"""
import argparse
import asyncio
import json
from pathlib import Path


def load_docs(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"文档不存在：{p}，先运行 importer.export_vector_docs")
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="data/vector_docs.jsonl")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--recreate", action="store_true", help="删除并重建集合")
    ap.add_argument("--dry-run", action="store_true", help="只编码不写库")
    args = ap.parse_args()

    from app.config import settings
    from app.services.vector_recall import load_encoder

    docs = load_docs(args.docs)
    encoder = load_encoder()
    if encoder is None:
        raise SystemExit("编码器不可用：确认 VECTOR_ENCODER 配置（api 需 EMBED_*，local 需 VECTOR_IR_PATH）")
    vectors = asyncio.run(encoder.encode([d["text"] for d in docs]))
    dim = len(vectors[0])
    print(f"文档 {len(docs)} 条，向量维度 {dim}，编码器 {encoder.model}")
    if args.dry_run:
        print("dry-run：未写 Milvus")
        return

    from pymilvus import MilvusClient

    client = MilvusClient(uri=settings.milvus_uri)
    if args.recreate and client.has_collection(settings.milvus_collection):
        client.drop_collection(settings.milvus_collection)
    if not client.has_collection(settings.milvus_collection):
        client.create_collection(collection_name=settings.milvus_collection, dimension=dim,
                                 auto_id=True, enable_dynamic_field=True)
    for start in range(0, len(docs), args.batch):
        chunk = docs[start:start + args.batch]
        rows = [{"vector": [float(x) for x in vectors[i]], "uid": d["uid"], "type": d["type"],
                 "text": d["text"], "payload": d["payload"]}
                for i, d in enumerate(chunk, start=start)]
        client.insert(collection_name=settings.milvus_collection, data=rows)
        print(f"已写入 {min(start + args.batch, len(docs))}/{len(docs)}")
    print("完成：", settings.milvus_collection)


if __name__ == "__main__":
    main()
