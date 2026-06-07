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
from degreezeor.ingestion.adapters.federalregister import federal_register_adapter
from degreezeor.pipeline import (
    STATE_POLICIES,
    TARGET_SPECS,
    score_executive_order,
    score_law,
    score_state_policy,
    score_target,
)


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


def cmd_score_state(args: argparse.Namespace) -> int:
    spec = STATE_POLICIES.get(args.key)
    if spec is None:
        print(f"unknown state policy {args.key!r}; known: {', '.join(STATE_POLICIES)}")
        return 2
    with session_scope() as s:
        result = score_state_policy(s, spec)
    print(f"action_id={result.action_id} eu_id={result.eu_id} status={result.status}")
    print(f"score_run_id={result.score_run_id} reproducible_hash={result.reproducible_hash}")
    return 0


def cmd_score_eo(args: argparse.Namespace) -> int:
    doc = args.document_number
    if args.eo_number:
        doc = federal_register_adapter.find_executive_order(args.eo_number)
        if doc is None:
            print(f"could not resolve EO number {args.eo_number} to a Federal Register document")
            return 2
    with session_scope() as s:
        result = score_executive_order(s, doc)
    print(f"action_id={result.action_id} eu_id={result.eu_id} status={result.status}")
    print(f"score_run_id={result.score_run_id} reproducible_hash={result.reproducible_hash}")
    return 0


def cmd_score_target(args: argparse.Namespace) -> int:
    spec = TARGET_SPECS.get(args.key)
    if spec is None:
        print(f"unknown target {args.key!r}; known: {', '.join(TARGET_SPECS)}")
        return 2
    with session_scope() as s:
        result = score_target(s, spec)
    print(f"action_id={result.action_id} eu_id={result.eu_id} status={result.status}")
    print(f"score_run_id={result.score_run_id} reproducible_hash={result.reproducible_hash}")
    return 0


def cmd_dispute(args: argparse.Namespace) -> int:
    from degreezeor.disputes import file_dispute, resolve_dispute

    with session_scope() as s:
        d = file_dispute(s, eu_id=args.eu_id, filer=args.filer, claim=args.claim)
        print(f"filed dispute {d.id} on EU {d.eu_id} (status={d.status})")
        if args.resolve:
            r = resolve_dispute(s, dispute_id=d.id)
            print(f"resolved: status={r.status} reproduced={r.reproduced}")
            print(f"public_diff: {r.public_diff['summary']}")
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

    ss = sub.add_parser("score-state")
    ss.add_argument("key", help=f"state policy key (one of: {', '.join(STATE_POLICIES)})")
    ss.set_defaults(func=cmd_score_state)

    se = sub.add_parser("score-eo")
    se.add_argument("document_number", nargs="?", help="Federal Register document number, e.g. 2021-09263")
    se.add_argument("--eo-number", type=int, help="resolve by executive order number, e.g. 14026")
    se.set_defaults(func=cmd_score_eo)

    st = sub.add_parser("score-target")
    st.add_argument("key", help=f"target spec key (one of: {', '.join(TARGET_SPECS)})")
    st.set_defaults(func=cmd_score_target)

    dp = sub.add_parser("dispute")
    dp.add_argument("eu_id", type=int)
    dp.add_argument("--filer", default="anonymous")
    dp.add_argument("--claim", default="Challenge: please re-run and verify this score.")
    dp.add_argument("--resolve", action="store_true", help="immediately resolve via reproducible re-run")
    dp.set_defaults(func=cmd_dispute)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
