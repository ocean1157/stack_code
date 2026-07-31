from __future__ import annotations

import argparse
import json

from .pipeline import run_pipeline
from .monitor import refresh_once
from .research import run_research


def main() -> int:
    parser = argparse.ArgumentParser(description="东方财富热度池量化研究与日频交易信号")
    parser.add_argument(
        "command",
        choices=["web", "run", "refresh", "research", "macro"],
        nargs="?",
        default="web",
        help="默认 web（只读服务）；run/refresh/research/macro 会采集并写入数据",
    )
    parser.add_argument("--no-db", action="store_true", help="不写入 PostgreSQL")
    parser.add_argument("--reuse-data", action="store_true", help="复用最近一次已下载的数据（故障恢复用）")
    parser.add_argument("--force", action="store_true", help="忽略交易时段限制执行一次刷新")
    parser.add_argument("--host", default="127.0.0.1", help="Web 监听地址")
    parser.add_argument("--port", type=int, default=8765, help="Web 监听端口")
    args = parser.parse_args()
    if args.command == "web":
        from .web import serve
        print("启动只读 Web 服务；不会执行数据采集、删表或模型写库。", flush=True)
        serve(args.host, args.port)
        return 0
    if args.command == "macro":
        from .macro import run_macro_collection
        report = run_macro_collection()
    elif args.command == "research":
        report = run_research(days=3)
    elif args.command == "refresh":
        report = refresh_once(force=args.force)
    else:
        report = run_pipeline(persist=not args.no_db, reuse_data=args.reuse_data)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
