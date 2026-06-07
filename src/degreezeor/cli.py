"""Command-line interface for the degreezeor MVP slice.

Commands:
  initdb                      create the schema in the configured database
  score <congress> <law_no>   ingest + score one enacted law, end-to-end
  verify-audit                replay and verify the audit hash chain
  list                        list scored evaluation units
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from degreezeor.core import audit
from degreezeor.core.db import engine, session_scope
from degreezeor.core.models import Action, Base, EUScore, EvaluationUnit, ScoreRun
from degreezeor.pipeline import score_law


def cmd_initdb(_: argparse.Namespace) -> int:
    Base.metadata.create_all(engine)
    print("schema created/verified")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    with session_scope() as s:
        result = score_law(s, args.congress, args.law_number, args.law_type)
    print(f"action_id={result.action_id} eu_id={result.eu_id} status={result.status}")
    print(f"score_run_id={result.score_run_id} reproducible_hash={result.reproducible_hash}")
    return 0


def cmd_verify_audit(_: argparse.Namespace) -> int:
    with session_scope() as s:
        ok, broken = audit.verify_chain(s)
    print(f"audit_chain_ok={ok} first_broken_id={broken}")
    return 0 if ok else 1


def cmd_list(_: argparse.Namespace) -> int:
    with session_scope() as s:
        rows = s.execute(
            select(EvaluationUnit, Action).join(Action, Action.id == EvaluationUnit.action_id)
        ).all()
        for eu, action in rows:
            run = s.execute(
                select(ScoreRun).where(ScoreRun.eu_id == eu.id).order_by(ScoreRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            sc = (
                s.execute(select(EUScore).where(EUScore.score_run_id == run.id)).scalar_one_or_none()
                if run
                else None
            )
            comp = (str(sc.composite) if sc and sc.composite is not None else "—")
            conf = (str(sc.confidence) if sc else "—")
            print(f"[{eu.status:>22}] {action.native_identifier or action.id}  "
                  f"composite={comp}  confidence={conf}  :: {action.title[:60]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="degreezeor")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(func=cmd_initdb)
    sub.add_parser("verify-audit").set_defaults(func=cmd_verify_audit)
    sub.add_parser("list").set_defaults(func=cmd_list)

    sp = sub.add_parser("score")
    sp.add_argument("congress", type=int)
    sp.add_argument("law_number", type=int)
    sp.add_argument("--law-type", default="pub", choices=["pub", "priv"])
    sp.set_defaults(func=cmd_score)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
