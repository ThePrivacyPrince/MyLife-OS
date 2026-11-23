"""Command line entry point: `lifeos init | seed | today | review | dashboard`."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

from lifeos.config import get_settings
from lifeos.db import init_db, make_engine, session_factory, unit_of_work
from lifeos.services.planner import plan_day, review_day
from lifeos.services.seed import seed


def _print_plan(plan) -> None:
    print(f"\nLIFE OS - {plan.date:%A %b %d, %Y}\n" + "=" * 48)
    for w in plan.warnings:
        print(f"! {w}")
    for f in plan.flags:
        print(f"? {f.message}")
    if plan.protected:
        print("\nProtected: " + ", ".join(p.task.title for p in plan.protected))
    for prio in (1, 2, 3):
        print(f"\nP{prio}")
        if not plan.queue[prio]:
            print("  (clear)")
        for p in plan.queue[prio]:
            t = p.task
            tags = [t.pillar_code, "recurring" if t.is_recurring else "one-time"]
            if p.overdue:
                tags.append(f"OVERDUE since {t.due_date}")
            elif t.due_date:
                tags.append(f"due {t.due_date}")
            lock = "" if p.gate.allowed else "  [gated]"
            print(f"  #{t.id:<4} {t.title}  ({', '.join(tags)}){lock}")
    if plan.blocked or plan.waiting:
        print("\nBlocked / waiting")
        for t in plan.blocked + plan.waiting:
            print(f"  #{t.id:<4} {t.title} - {t.status}: {t.status_reason}")
    if plan.dependency_locked:
        print("\nWaiting on prerequisites")
        for p in plan.dependency_locked:
            print(f"  #{p.task.id:<4} {p.task.title} <- " + ", ".join(f"#{u.id}" for u in p.unmet))
    if plan.upcoming:
        print("\nUpcoming")
        for t in plan.upcoming[:10]:
            print(f"  {t.effective_date}  P{t.priority} {t.title}")
    print(f"\nCapacity: {plan.committed_minutes}/{plan.capacity_minutes} min committed to P1+P2\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lifeos")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database and the Five Pillars")
    sub.add_parser("seed", help="load starter goals, tasks and routines")
    p_today = sub.add_parser("today", help="print today's command center")
    p_today.add_argument("--date", type=dt.date.fromisoformat)
    p_review = sub.add_parser("review", help="print the end-of-day review")
    p_review.add_argument("--date", type=dt.date.fromisoformat)
    sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    p_imp = sub.add_parser("import-plan", help="import a plan file (vision, goals, objectives, key results, tasks)")
    p_imp.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "dashboard":
        app = Path(__file__).parent / "dashboard" / "app.py"
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])

    engine = make_engine()
    init_db(engine)
    factory = session_factory(engine)
    today = getattr(args, "date", None) or get_settings().today()

    from lifeos.errors import LifeOSError
    try:
        return _run(args, factory, today)
    except LifeOSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _run(args, factory, today) -> int:
    with unit_of_work(factory) as s:
        if args.cmd == "init":
            print(f"Database ready at {get_settings().database_url}")
        elif args.cmd == "seed":
            print(f"Seeded: {seed(s, today)}")
        elif args.cmd == "import-plan":
            import yaml
            from lifeos.services.plan_import import import_plan
            result = import_plan(s, yaml.safe_load(args.path.read_text()), today)
            print(f"Imported: {result['summary']}")
            for w in result["warnings"]:
                print(f"  note: {w}")
        elif args.cmd == "today":
            _print_plan(plan_day(s, today))
        elif args.cmd == "review":
            r = review_day(s, today)
            rate = "n/a" if r.p1_rate is None else f"{r.p1_rate:.0%}"
            print(f"Review {r.date}: completed {len(r.completed)}, incomplete {len(r.incomplete)}, "
                  f"missed {len(r.missed)}, blocked {len(r.blocked)}, P1 completion {rate}, "
                  f"gate overrides {r.gate_overrides}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
