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


def cmd_ingest_state_policies(args: argparse.Namespace) -> int:
    from collections import Counter

    from degreezeor.pipeline import ingest_state_policies
    with session_scope() as s:
        rs = ingest_state_policies(s)
    print("state policies:", dict(Counter(r.status for r in rs)), "total", len(rs))
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


def cmd_court_survival(args: argparse.Namespace) -> int:
    from collections import Counter

    from degreezeor.pipeline import COURT_SURVIVAL_SPECS, score_court_survival
    keys = [args.key] if args.key else list(COURT_SURVIVAL_SPECS)
    rs = []
    with session_scope() as s:
        for k in keys:
            spec = COURT_SURVIVAL_SPECS.get(k)
            if spec is None:
                print(f"unknown key {k!r}; known: {', '.join(COURT_SURVIVAL_SPECS)}")
                return 2
            rs.append(score_court_survival(s, spec))
    print("court survival:", dict(Counter(r.status for r in rs)), "total", len(rs))
    return 0


def cmd_budget_execution(args: argparse.Namespace) -> int:
    from collections import Counter

    from degreezeor.pipeline import ingest_budget_execution
    with session_scope() as s:
        rs = ingest_budget_execution(s, args.fiscal_year, realized_kind=args.kind, limit=args.limit)
    print("budget execution:", dict(Counter(r.status for r in rs)), "total", len(rs))
    return 0


def cmd_enrich_names(args: argparse.Namespace) -> int:
    from degreezeor.ingestion.loader import enrich_official_names

    with session_scope() as s:
        n = enrich_official_names(s, limit=args.limit)
    print(f"enriched {n} official names")
    return 0


def cmd_batch_laws(args: argparse.Namespace) -> int:
    from collections import Counter

    from degreezeor.pipeline import batch_score_laws
    with session_scope() as s:
        rs = batch_score_laws(s, args.congress, limit=args.limit)
    print("batch laws:", dict(Counter(r.status for r in rs)), "total", len(rs))
    return 0


def cmd_batch_eos(args: argparse.Namespace) -> int:
    from collections import Counter

    from degreezeor.pipeline import batch_score_executive_orders
    with session_scope() as s:
        rs = batch_score_executive_orders(s, limit=args.limit)
    print("batch EOs:", dict(Counter(r.status for r in rs)), "total", len(rs))
    return 0


def cmd_ingest_defc(args: argparse.Namespace) -> int:
    from degreezeor.pipeline import ingest_defc_delivery

    with session_scope() as s:
        rs = ingest_defc_delivery(s, limit=args.limit)
    from collections import Counter
    print("DEFC delivery:", dict(Counter(r.status for r in rs)), "total", len(rs))
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


def cmd_migrate(_: argparse.Namespace) -> int:
    """Apply database migrations (production schema management)."""
    from alembic import command
    from alembic.config import Config

    from degreezeor.config import REPO_ROOT
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    print("migrations applied (head)")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Idempotent full ingestion/scoring pass — the production cron entrypoint."""
    from degreezeor.pipeline import refresh_all
    with session_scope() as s:
        counts = refresh_all(s, budget_fiscal_year=args.fiscal_year, congress=args.congress,
                             law_limit=args.law_limit, eo_limit=args.eo_limit)
    print("refresh complete:", counts)
    return 0


def cmd_verify_audit(_: argparse.Namespace) -> int:
    with session_scope() as s:
        ok, broken = audit.verify_chain(s)
    print(f"audit_chain_ok={ok} first_broken_id={broken}")
    return 0 if ok else 1


def cmd_party_symmetry(_: argparse.Namespace) -> int:
    """Integrity-at-scale monitoring (PLAN §9.12): party-level score distribution."""
    from degreezeor.integrity import party_symmetry_report

    with session_scope() as s:
        report = party_symmetry_report(s)
    for p in report.parties:
        print(f"  {p.abbrev:>4}: attributed={p.attributed_eus:>4} scored={p.scored_eus:>3} "
              f"share={p.scored_share} mean_composite={p.mean_composite} "
              f"mean_confidence={p.mean_confidence}")
    print(f"composite_gap={report.composite_gap} scored_share_gap={report.scored_share_gap}")
    print(f"review_required={report.review_required}")
    for r in report.review_reasons:
        print(f"  REVIEW: {r}")
    return 0


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
    sub.add_parser("migrate", help="apply DB migrations (alembic upgrade head)").set_defaults(func=cmd_migrate)
    sub.add_parser("verify-audit").set_defaults(func=cmd_verify_audit)
    sub.add_parser("party-symmetry",
                   help="integrity monitoring: party-level score distribution (PLAN §9.12)"
                   ).set_defaults(func=cmd_party_symmetry)
    sub.add_parser("list").set_defaults(func=cmd_list)

    rf = sub.add_parser("refresh", help="idempotent full ingestion/scoring pass (cron entrypoint)")
    rf.add_argument("--fiscal-year", type=int, default=2024)
    rf.add_argument("--congress", type=int, default=117)
    rf.add_argument("--law-limit", type=int, default=25)
    rf.add_argument("--eo-limit", type=int, default=15)
    rf.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("score")
    sp.add_argument("congress", type=int)
    sp.add_argument("law_number", type=int)
    sp.add_argument("--law-type", default="pub", choices=["pub", "priv"])
    sp.set_defaults(func=cmd_score)

    ss = sub.add_parser("score-state")
    ss.add_argument("key", help=f"state policy key (one of: {', '.join(STATE_POLICIES)})")
    ss.set_defaults(func=cmd_score_state)

    sps = sub.add_parser("ingest-state-policies", help="score all curated state policies (synthetic control)")
    sps.set_defaults(func=lambda a: cmd_ingest_state_policies(a))

    se = sub.add_parser("score-eo")
    se.add_argument("document_number", nargs="?", help="Federal Register document number, e.g. 2021-09263")
    se.add_argument("--eo-number", type=int, help="resolve by executive order number, e.g. 14026")
    se.set_defaults(func=cmd_score_eo)

    st = sub.add_parser("score-target")
    st.add_argument("key", help=f"target spec key (one of: {', '.join(TARGET_SPECS)})")
    st.set_defaults(func=cmd_score_target)

    di = sub.add_parser("ingest-defc", help="batch verifiable delivery scores for DEFC-tagged laws")
    di.add_argument("--limit", type=int, default=None)
    di.set_defaults(func=cmd_ingest_defc)

    bl = sub.add_parser("batch-laws", help="batch-ingest+score enacted laws for a congress")
    bl.add_argument("congress", type=int)
    bl.add_argument("--limit", type=int, default=25)
    bl.set_defaults(func=cmd_batch_laws)

    be = sub.add_parser("batch-eos", help="batch-ingest+score recent executive orders")
    be.add_argument("--limit", type=int, default=25)
    be.set_defaults(func=cmd_batch_eos)

    cs = sub.add_parser("court-survival", help="score executive-order survival of judicial review (curated)")
    cs.add_argument("key", nargs="?", help="court-survival spec key (default: all)")
    cs.set_defaults(func=cmd_court_survival)

    bx = sub.add_parser("budget-execution", help="score agency budget execution (obligation/outlay rate) for a FY")
    bx.add_argument("fiscal_year", type=int)
    bx.add_argument("--kind", default="obligated", choices=["obligated", "outlayed"])
    bx.add_argument("--limit", type=int, default=None)
    bx.set_defaults(func=cmd_budget_execution)

    en = sub.add_parser("enrich-names", help="fill in full official names from Congress.gov")
    en.add_argument("--limit", type=int, default=None)
    en.set_defaults(func=cmd_enrich_names)

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
