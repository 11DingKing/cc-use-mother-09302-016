"""启动传统技艺师徒计划后端服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mentorship.api import make_server
from mentorship.db import Database
from mentorship.service import MentorshipService


def main() -> None:
    parser = argparse.ArgumentParser(description="传统技艺师徒计划后端服务")
    parser.add_argument("--db", default=str(ROOT / "mentorship.sqlite3"), help="SQLite 数据库文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    service = MentorshipService(Database(args.db))
    server = make_server(service, args.host, args.port)
    print(f"服务已启动：http://{args.host}:{args.port}（数据库：{args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
