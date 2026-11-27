"""预热元数据向量索引（A05）：编码表/口径/字段三类文档并写入 `meta_embedding`。

为什么需要它：向量走 API embedding 时，首次召回要编码约 130 份文档（2~5 秒）。
如果不预热，这笔开销就落在**第一个真实用户**的提问上——而 `TIMEOUT_RECALL` 只有 3 秒，
超时后该轮会静默退化为纯关键词召回，现象是「刚部署时召回特别差，过一会儿才好」。

用法：
  python -m importer.warm_vectors
  python -m importer.warm_vectors --prune     # 顺带清理已下线表/口径的残留向量
"""
import argparse
import asyncio
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true", help="清理已不存在的文档缓存")
    args = ap.parse_args()

    from app.config import settings

    if not settings.vector_recall_enabled:
        raise SystemExit("VECTOR_RECALL_ENABLED=false：先打开向量召回再预热")

    from app.services.metadata_store import metadata_store
    from app.services.vector_recall import (caliber_uid, column_uid, flatten_columns,
                                            table_uid, vector_recall)

    async def run() -> None:
        started = time.monotonic()
        stats = await vector_recall.prewarm()
        print(f"预热完成：{stats}（{time.monotonic() - started:.2f}s，"
              f"encoder={settings.vector_encoder}）")
        if not args.prune:
            return
        from app.services.embedding_store import embedding_store

        tables = metadata_store.tables()
        keep = ({table_uid(t) for t in tables}
                | {caliber_uid(c) for c in metadata_store.calibers()}
                | {column_uid(c) for c in flatten_columns(tables)})
        removed = await asyncio.to_thread(embedding_store.prune, keep, None)
        print(f"清理残留向量 {removed} 条")

    asyncio.run(run())


if __name__ == "__main__":
    main()
