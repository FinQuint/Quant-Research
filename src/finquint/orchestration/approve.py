"""Reviewer entry point: list, inspect and decide approval requests.

    python -m finquint.orchestration.approve --db research.sqlite list
    python -m finquint.orchestration.approve --db research.sqlite show REQUEST_ID
    python -m finquint.orchestration.approve --db research.sqlite decide REQUEST_ID \\
        --reviewer walleva1715 --decision approve|revise|reject [--notes "..."]

The signing key comes from the FINQUINT_APPROVAL_KEY environment variable (hex, at least
32 bytes). Run this only in the reviewer's own shell: never give the key to an agent process,
because anyone holding it can sign approvals. Prints the approval record ID to resume with.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from .gate.approvals import ApprovalError, ApprovalPolicy, ApprovalService, RequestStatus

DEFAULT_POLICY = Path("configs") / "approval_policy.json"
KEY_VARIABLE = "FINQUINT_APPROVAL_KEY"


def _key():
    raw = os.environ.get(KEY_VARIABLE, "")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) < 32:
        raise SystemExit(f"{KEY_VARIABLE} must hold at least 32 bytes as hex")
    return key


def main(argv=None, *, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Review approval requests.")
    parser.add_argument("--db", required=True, help="SQLite database shared with the workflow")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--all", action="store_true", help="include decided and expired requests")
    show = commands.add_parser("show")
    show.add_argument("request_id")
    decide = commands.add_parser("decide")
    decide.add_argument("request_id")
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--decision", required=True, choices=["approve", "revise", "reject"])
    decide.add_argument("--notes", default="")
    args = parser.parse_args(argv)

    connection = sqlite3.connect(args.db)
    try:
        service = ApprovalService(connection, secret=_key(), policy=ApprovalPolicy.from_file(args.policy),
                                  authority=None)
        if args.command == "list":
            requests = service.requests(status=None if args.all else RequestStatus.PENDING)
            for r in requests:
                print(f"{r.request_id}  {r.status.value:<8}  {r.action['action_type']:<24}  "
                      f"{r.action['target']}  run={r.run_id} branch={r.branch_id}", file=out)
            if not requests:
                print("no pending approval requests", file=out)
            return 0
        if args.command == "show":
            request = service.get_request(args.request_id)
            if request is None:
                print("unknown approval request", file=sys.stderr)
                return 1
            print(json.dumps({"request_id": request.request_id, "status": request.status.value,
                              "run_id": request.run_id, "branch_id": request.branch_id,
                              "fingerprint": request.fingerprint, "reason": request.reason,
                              "expires_at": request.expires_at, "action": request.action,
                              "evidence": request.evidence}, indent=2, sort_keys=True), file=out)
            return 0
        try:
            record = service.decide(args.request_id, reviewer=args.reviewer, decision=args.decision,
                                    notes=args.notes)
        except ApprovalError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"approval_record_id": record.record_id, "decision": record.decision.value,
                          "request_status": service.get_request(record.request_id).status.value}), file=out)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
