from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from schemagate import __version__
from schemagate.drift.remediate import propose_expand
from schemagate.errors import DriftBlockedError, NotFoundError, SchemagateError
from schemagate.materialize.dialects import DIALECTS, get_dialect
from schemagate.materialize.executor import execute_plan
from schemagate.materialize.plan import build_plan
from schemagate.project import Project
from schemagate.store.audit import chain_head, clean_actor


def table(rows: Sequence[dict[str, Any]], columns: Sequence[str], max_width: int = 48) -> str:
    if not rows:
        return "(none)"

    def cell(v: Any) -> str:
        s = "" if v is None else str(v)
        return s if len(s) <= max_width else s[: max_width - 3] + "..."

    widths = {c: max(len(c), *(len(cell(r.get(c))) for r in rows)) for c in columns}
    lines = ["  ".join(c.ljust(widths[c]) for c in columns).rstrip(),
             "  ".join("-" * widths[c] for c in columns)]
    for r in rows:
        lines.append("  ".join(cell(r.get(c)).ljust(widths[c]) for c in columns).rstrip())
    return "\n".join(lines)


def _actor(args: argparse.Namespace) -> str:
    if args.actor is not None:
        return clean_actor(args.actor)
    return clean_actor(os.environ.get("SCHEMAGATE_ACTOR") or getpass.getuser())


def _project(args: argparse.Namespace) -> Project:
    return Project.load(Path(args.project))


def cmd_init(args: argparse.Namespace) -> int:
    project = Project.init(Path(args.project), [Path(s) for s in args.schema])
    print(f"initialized schemagate project in {project.root}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    project = _project(args)
    rows = []
    for f in args.files:
        for o in project.ingest(args.feed, args.schema, Path(f), _actor(args), args.header_row,
                                args.sheet or None):
            rows.append({"object": o.object_id, "file": o.table.source_file, "sheet": o.table.sheet or "",
                         "header_row": o.table.header_row + 1, "rows": len(o.table.rows),
                         "columns": len(o.table.columns),
                         "status": "ingested" if o.created else "skipped (same content hash)"})
    print(table(rows, ["object", "file", "sheet", "header_row", "rows", "columns", "status"]))
    return 0


def _latest_object(project: Project, feed_id: int) -> int:
    objs = project.service().source_objects(feed_id)
    if not objs:
        raise SchemagateError("feed has no ingested source objects")
    return int(objs[-1]["id"])


def cmd_propose(args: argparse.Namespace) -> int:
    project = _project(args)
    svc = project.service()
    feed = svc.feed(args.feed)
    feed_id = int(feed["id"])
    open_specs = svc.specs(feed_id, status="pending")
    if (feed["active_spec_id"] or open_specs) and not args.force:
        print("feed already has an active or pending spec; use `schemagate drift --remediate` "
              "to propose a new version (or --force)", file=sys.stderr)
        return 2
    object_id = args.object or _latest_object(project, feed_id)
    obj = svc.source_object(object_id)
    schema = svc.feed_schema(feed_id)
    result = project.chain(args.provider).run(schema, obj["profiles"], project.confidence)
    spec_id = svc.create_spec(feed_id, result.proposals, _actor(args), source_object_id=object_id,
                              source_profiles=obj["profiles"])
    tiers: dict[str, int] = {}
    for p in result.proposals:
        tiers[p.tier.value] = tiers.get(p.tier.value, 0) + 1
    print(f"proposed spec {spec_id} for feed {args.feed!r} from source object {object_id}: "
          + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    print(f"next: schemagate review --spec {spec_id}")
    return 0


def _print_spec(project: Project, spec_id: int, verbose: bool) -> None:
    svc = project.service()
    spec = svc.spec(spec_id)
    approvals = svc.approvals(spec_id)
    required, rule = svc.required_approvals(spec_id)
    print(f"spec {spec.id}  feed={spec.feed_name}  version={spec.version}  kind={spec.change_kind}  "
          f"table={spec.target_table}  status={spec.status}")
    print(f"proposed by {spec.created_by}; approvals {sum(a['decision'] == 'approve' for a in approvals)}"
          f"/{required} ({rule})"
          + (f"; approved by {', '.join(a['approver'] for a in approvals)}" if approvals else ""))
    rows = []
    for p in spec.proposals:
        needs = p["tier"] in ("model", "rename") and p["status"] == "proposed"
        rows.append({
            "id": p["id"], "source column": p["source_column"],
            "target": p["target_field"] or f"({p['extension_column']})",
            "tier": p["tier"], "conf": f"{p['confidence']:.2f}",
            "status": p["status"] + (" *" if needs else ""),
            "rationale": p["rationale"],
        })
    print(table(rows, ["id", "source column", "target", "tier", "conf", "status", "rationale"],
                max_width=200 if verbose else 60))
    mapped = {p["target_field"] for p in spec.proposals if p["target_field"]}
    missing = [f.name for f in spec.schema.fields if f.required and f.name not in mapped]
    if missing:
        print(f"warning: required fields not mapped: {', '.join(missing)}")
    if any(r["status"].endswith("*") for r in rows):
        print("* model/rename proposals need `schemagate accept`, `override` or `demote` before approval")


def cmd_review(args: argparse.Namespace) -> int:
    project = _project(args)
    svc = project.service()
    if args.spec:
        _print_spec(project, args.spec, args.verbose)
        return 0
    feed_id = int(svc.feed(args.feed)["id"]) if args.feed else None
    specs = svc.specs(feed_id, status=None if args.all else "pending")
    if not specs:
        print("no pending specs")
    for s in specs:
        _print_spec(project, int(s["id"]), args.verbose)
        print()
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    svc = _project(args).service()
    for pid in args.proposal:
        svc.accept_proposal(pid, _actor(args), args.comment or "")
        print(f"accepted proposal {pid}")
    return 0


def cmd_demote(args: argparse.Namespace) -> int:
    _project(args).service().demote_proposal(args.proposal, _actor(args), args.reason)
    print(f"demoted proposal {args.proposal} to an extension column")
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    _project(args).service().override_proposal(args.proposal, args.target, _actor(args), args.reason)
    print(f"proposal {args.proposal} now maps to {args.target} (tier=human)")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    svc = _project(args).service()
    status = svc.approve_spec(args.spec, _actor(args), args.comment or "")
    spec = svc.spec(args.spec)
    n = sum(a["decision"] == "approve" for a in svc.approvals(args.spec))
    print(f"spec {args.spec}: approval by {_actor(args)} recorded ({n}/{spec.required_approvals}); status={status}")
    feed = svc.feed(spec.feed_id)
    if status == "approved" and feed["active_spec_id"] == spec.id:
        print(f"spec {spec.id} is the active spec for feed {spec.feed_name!r}")
    elif status == "approved":
        print(f"spec {spec.id} approved but not active; run `schemagate switch {spec.id}`")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    _project(args).service().reject_spec(args.spec, _actor(args), args.comment)
    print(f"spec {args.spec} rejected")
    return 0


def cmd_switch(args: argparse.Namespace) -> int:
    svc = _project(args).service()
    svc.switch_active(args.spec, _actor(args))
    spec = svc.spec(args.spec)
    print(f"feed {spec.feed_name!r} now uses spec {spec.id} (v{spec.version})")
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    project = _project(args)
    svc = project.service()
    feed = svc.feed(args.feed)
    feed_id = int(feed["id"])
    spec_id = args.spec or feed["active_spec_id"]
    if spec_id is None:
        print(f"feed {args.feed!r} has no approved spec; nothing can be materialized", file=sys.stderr)
        return 3
    spec = svc.verify_approved(int(spec_id))
    done = {int(r["source_object_id"]) for r in svc.db.query(
        "SELECT source_object_id FROM materialization WHERE spec_id = ?", (spec.id,))}
    if args.object:
        objects = [args.object]
    else:
        objects = [int(o["id"]) for o in svc.source_objects(feed_id)]
        materialized_any = {int(r["source_object_id"]) for r in svc.db.query(
            "SELECT m.source_object_id FROM materialization m JOIN mapping_spec s ON s.id = m.spec_id"
            " WHERE s.feed_id = ?", (feed_id,))}
        objects = [o for o in objects if o not in materialized_any]
    if not objects:
        print("nothing to materialize")
        return 0
    rc = 0
    for obj_id in objects:
        if obj_id != spec.source_object_id:
            project.detect_drift(feed_id, obj_id, _actor(args), spec_id=spec.id)
        try:
            plan = build_plan(svc, spec.id, obj_id)
        except DriftBlockedError as exc:
            print(f"object {obj_id}: blocked: {exc}")
            rc = 3
            continue
        for w in plan.warnings:
            print(f"object {obj_id}: warning: {w}")
        if args.dialect != "sqlite" or args.sql_only:
            bundle = get_dialect(args.dialect).render(plan)
            if args.out:
                Path(args.out).write_text(bundle.script(), "utf-8")
                print(f"wrote {args.dialect} SQL for object {obj_id} to {args.out}")
            else:
                print(bundle.script())
            continue
        if obj_id in done and not args.force:
            print(f"object {obj_id}: already materialized with spec {spec.id}")
            continue
        result = execute_plan(project.warehouse(), plan, "sqlite")
        svc.record_materialization(spec.id, obj_id, "sqlite", plan.target_table, result.rows_written,
                                   _actor(args))
        print(f"object {obj_id}: upserted {result.rows_written} rows into {result.table} "
              f"(staged {result.rows_staged}, rejected for null key {result.rows_rejected_null_key}) "
              f"using spec {spec.id} v{spec.version}")
    return rc


def cmd_drift(args: argparse.Namespace) -> int:
    project = _project(args)
    svc = project.service()
    feed_id = int(svc.feed(args.feed)["id"])
    if args.remediate:
        active = svc.active_spec(feed_id)
        if active is None:
            raise SchemagateError("no active spec to remediate")
        events = svc.drift_events(feed_id)
        if not events:
            print("no open drift events")
            return 0
        object_id = args.object or max(int(e["source_object_id"]) for e in events)
        events = [e for e in events if e["source_object_id"] == object_id]
        spec_id = propose_expand(svc, active, object_id, events, project.chain(args.provider), _actor(args))
        print(f"proposed expand spec {spec_id} (parent spec {active.id}) for source object {object_id}")
        print(f"next: schemagate review --spec {spec_id}; after approval: schemagate switch {spec_id}")
        return 0
    object_id = args.object or _latest_object(project, feed_id)
    events = project.detect_drift(feed_id, object_id, _actor(args))
    rows = [{"kind": e["kind"], "severity": e["severity"], "column": e["source_column"],
             "detail": json.dumps(e["detail"], sort_keys=True)} for e in events]
    print(f"drift for source object {object_id} against active spec:")
    print(table(rows, ["kind", "severity", "column", "detail"], max_width=90))
    return 1 if any(e["severity"] == "breaking" for e in events) else 0


def cmd_audit(args: argparse.Namespace) -> int:
    svc = _project(args).service()
    if args.action == "verify":
        result = svc.audit.verify()
        head = chain_head(svc.db)
        if not result.ok:
            print(f"audit chain BROKEN at entry {result.first_bad_id}: {result.reason}")
            return 1
        if args.expect_head and args.expect_head != head:
            print(f"audit chain valid but head {head[:16]} != expected {args.expect_head[:16]} "
                  "(entries removed or appended since the anchor)")
            return 1
        print(f"audit chain OK: {result.entries} entries, head {head}")
        return 0
    rows = svc.audit.entries(args.entity_type, args.entity_id)
    if args.limit:
        rows = rows[-args.limit:]
    out = [{"id": r["id"], "ts": r["ts"][:19], "actor": r["actor"], "action": r["action"],
            "entity": f"{r['entity_type']}:{r['entity_id']}", "hash": r["hash"][:12],
            "payload": json.dumps(r["payload"], sort_keys=True)} for r in rows]
    print(table(out, ["id", "ts", "actor", "action", "entity", "hash", "payload"],
                max_width=200 if args.verbose else 70))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    svc = _project(args).service()
    rows = []
    for f in svc.feeds():
        objs = svc.source_objects(int(f["id"]))
        rows.append({"feed": f["name"], "schema": f["schema_name"], "active_spec": f["active_spec_id"],
                     "objects": len(objs), "open_drift": len(svc.drift_events(int(f["id"])))})
    print(table(rows, ["feed", "schema", "active_spec", "objects", "open_drift"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="schemagate", description=textwrap.dedent("""\
        Governed schema mapping: deterministic first, model last,
        nothing published without a recorded human decision."""))
    p.add_argument("--version", action="version", version=f"schemagate {__version__}")
    p.add_argument("-C", "--project", default=".", help="project directory (default: .)")
    p.add_argument("--actor", help="identity recorded in the audit log (default: $SCHEMAGATE_ACTOR or OS user)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a project")
    s.add_argument("--schema", action="append", default=[], help="target schema YAML to register")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("ingest", help="ingest CSV/XLSX files for a feed")
    s.add_argument("files", nargs="+")
    s.add_argument("--feed", required=True)
    s.add_argument("--schema", help="target schema name (required for a new feed)")
    s.add_argument("--header-row", type=int, help="0-based header row; detected when omitted")
    s.add_argument("--sheet", action="append", help="only ingest these sheets")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("propose", help="resolve columns and propose a mapping spec")
    s.add_argument("--feed", required=True)
    s.add_argument("--object", type=int)
    s.add_argument("--provider", choices=["heuristic", "anthropic", "openai", "none"])
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_propose)

    s = sub.add_parser("review", help="show pending specs with rationale")
    s.add_argument("--feed")
    s.add_argument("--spec", type=int)
    s.add_argument("--all", action="store_true", help="include decided specs")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("accept", help="accept model/rename proposals")
    s.add_argument("proposal", type=int, nargs="+")
    s.add_argument("--comment")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("demote", help="demote a proposal to an extension column")
    s.add_argument("proposal", type=int)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_demote)

    s = sub.add_parser("override", help="set a proposal's target by hand")
    s.add_argument("proposal", type=int)
    s.add_argument("target")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_override)

    s = sub.add_parser("approve", help="approve a spec version")
    s.add_argument("spec", type=int)
    s.add_argument("--comment")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("reject", help="reject a spec version")
    s.add_argument("spec", type=int)
    s.add_argument("--comment", required=True)
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser("switch", help="make an approved spec version the active one")
    s.add_argument("spec", type=int)
    s.set_defaults(func=cmd_switch)

    s = sub.add_parser("materialize", help="write approved mappings to the target table")
    s.add_argument("--feed", required=True)
    s.add_argument("--spec", type=int)
    s.add_argument("--object", type=int)
    s.add_argument("--dialect", choices=sorted(DIALECTS), default="sqlite")
    s.add_argument("--sql-only", action="store_true", help="print SQL instead of executing")
    s.add_argument("--out", help="write SQL to this file")
    s.add_argument("--force", action="store_true", help="re-run for already materialized objects")
    s.set_defaults(func=cmd_materialize)

    s = sub.add_parser("drift", help="detect drift against the active spec, or --remediate")
    s.add_argument("--feed", required=True)
    s.add_argument("--object", type=int)
    s.add_argument("--remediate", action="store_true", help="propose an expand spec for open drift")
    s.add_argument("--provider", choices=["heuristic", "anthropic", "openai", "none"])
    s.set_defaults(func=cmd_drift)

    s = sub.add_parser("audit", help="show or verify the audit log")
    s.add_argument("action", choices=["show", "verify"])
    s.add_argument("--entity-type")
    s.add_argument("--entity-id")
    s.add_argument("--limit", type=int)
    s.add_argument("--expect-head", help="anchored head hash to compare against")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("status", help="list feeds")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except NotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except (SchemagateError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
