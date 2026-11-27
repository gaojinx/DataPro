"""采集手工/回填入口。

    python -m worker.ingest_cli --code xianzhi --start 2026-11-27 --end 2026-11-27 --dry-run
    python -m worker.ingest_cli --code xianzhi --file tests/fixtures/xianzhi_sample.xlsx \
        --start 2026-11-27 --end 2026-11-27 --triggered-by manual:ops

--file 走 LocalXlsxDriver（归档回填/夹具）；不带 --file 走 PlaywrightDriver 真实登录导出。
输出 JSON 只含批次号、窗口、行数、状态与原因码，不含凭据。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from worker.xianzhi_collector import XianzhiCollector
from worker.xianzhi_driver import CollectorConfig

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "data" / "ingest" / "xianzhi.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DataPro 先知投放数据采集/回填")
    parser.add_argument("--code", default="xianzhi", choices=["xianzhi"], help="数据源编码")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="采集配置路径")
    parser.add_argument("--start", default="", help="回填起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="", help="回填结束日期 YYYY-MM-DD")
    parser.add_argument("--file", default="", help="本地归档 xlsx 路径（回填/夹具），走文件驱动")
    parser.add_argument("--dry-run", action="store_true", help="只解析与校验，不写任何库")
    parser.add_argument("--force", action="store_true",
                        help="忽略同窗口成功批次去重，强制重跑 upsert（回溯修正/验证幂等）")
    parser.add_argument("--triggered-by", default="manual:cli", help="触发来源，写 ingest_batch")
    parser.add_argument("--owner", default="", help="来源负责人（登记 ingest_source.owner）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = CollectorConfig.from_file(args.config)
    if args.start and args.end:
        config.date_mode = "range"
        config.date_start = args.start
        config.date_end = args.end
    elif bool(args.start) != bool(args.end):
        print(json.dumps({"status": "failed", "error_code": "BAD_DATE",
                          "detail": "--start 与 --end 必须同时提供"}, ensure_ascii=False))
        return 1

    collector = XianzhiCollector(config=config, triggered_by=args.triggered_by)
    if args.owner:
        collector.registrar.owner = args.owner
    result = collector.run(config=config, dry_run=args.dry_run, local_file=args.file,
                           skip_existing=not args.force)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("status") in ("success", "skipped", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
