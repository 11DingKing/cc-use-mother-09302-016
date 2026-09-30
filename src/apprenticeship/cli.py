"""命令行管理工具。

示例：
  python -m apprenticeship.cli --db data/app.db init
  python -m apprenticeship.cli --db data/app.db seed
  python -m apprenticeship.cli --db data/app.db capacity <plan_id> <mentor_id>
  python -m apprenticeship.cli --db data/app.db history <enrollment_id>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import connect, initialize_database
from .service import ApprenticeshipService


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def cmd_init(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        initialize_database(conn)
    finally:
        conn.close()
    print(f"数据库已初始化：{args.db}")


def cmd_capacity(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        _print(ApprenticeshipService(conn).capacity_summary(args.plan_id, args.mentor_id))
    finally:
        conn.close()


def cmd_history(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        _print(ApprenticeshipService(conn).history(args.enrollment_id))
    finally:
        conn.close()


def cmd_lineage(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        _print(ApprenticeshipService(conn).lineage(args.enrollment_id))
    finally:
        conn.close()


def cmd_enrollment(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        _print(ApprenticeshipService(conn).get_enrollment(args.enrollment_id, args.access_code))
    finally:
        conn.close()


def cmd_reconstruct(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        _print(ApprenticeshipService(conn).reconstruct_assessment(args.assessment_id))
    finally:
        conn.close()


def cmd_seed(args: argparse.Namespace) -> None:
    """写入一套可演示的学期数据，返回全部关键 ID。"""
    conn = connect(args.db)
    initialize_database(conn)
    svc = ApprenticeshipService(conn)
    try:
        m1 = svc.create_mentor("陆师傅", "竹编", "Q-2026-001")
        m2 = svc.create_mentor("沈师傅", "竹编", "Q-2026-002")
        plan = svc.create_plan("竹编", "2026春", 1, "竹编传承计划 v1")
        svc.add_stage(plan["id"], 1, "S1", "选材与处理", "识别竹材并完成杀青处理", "2026-04-30")
        svc.add_stage(plan["id"], 2, "S2", "基础编织", "完成三种基础纹样", "2026-06-15")
        svc.set_capacity(plan["id"], m1["id"], 2)
        svc.set_capacity(plan["id"], m2["id"], 1)
        svc.publish_plan(plan["id"])

        adult = svc.create_student("韩梅", "2005-03-01")
        minor = svc.create_student("李小军", "2012-08-20")
        svc.grant_consent(minor["id"], "李父", "GUARDIAN-CODE-001", "13800000000")

        e1 = svc.admit(adult["id"], plan["id"], m1["id"])
        e2 = svc.admit(minor["id"], plan["id"], m1["id"])
        _print({
            "mentors": {"lu": m1["id"], "shen": m2["id"]},
            "plan": plan["id"],
            "students": {"adult": adult["id"], "minor": minor["id"]},
            "enrollments": {"e1": e1["id"], "e2": e2["id"]},
            "capacity_lu": svc.capacity_summary(plan["id"], m1["id"]),
        })
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="师徒计划管理 CLI")
    parser.add_argument("--db", default="data/app.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("seed").set_defaults(func=cmd_seed)

    p = sub.add_parser("capacity")
    p.add_argument("plan_id")
    p.add_argument("mentor_id")
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("history")
    p.add_argument("enrollment_id")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("lineage")
    p.add_argument("enrollment_id")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("enrollment")
    p.add_argument("enrollment_id")
    p.add_argument("--access-code")
    p.set_defaults(func=cmd_enrollment)

    p = sub.add_parser("reconstruct")
    p.add_argument("assessment_id")
    p.set_defaults(func=cmd_reconstruct)

    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
