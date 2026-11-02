"""Signed approval records (spec: "Approval path", steps 2-6 and 8).

A REQUEST_HUMAN action becomes an ApprovalRequest. A reviewer's decision is stored as an
immutable ApprovalRecord signed with the approval service's HMAC key. verify() checks the
record (signature, reviewer authority, fingerprint, expiry, single use) and only then mints
a capability token for that exact action.

Keep the approval key out of every agent process: anything holding it can sign approvals.
Reviewer identity is asserted by the host entry point that calls decide() (the approval
CLI or an authenticated UI), never by an agent.

Times are seconds since the epoch from an injected clock, so expiry is testable.
"""
from dataclasses import asdict, dataclass, field
from enum import Enum
import hmac
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sqlite3
import time
from types import MappingProxyType
from typing import Mapping

from finquint.autonomous.models import canonical

from .decide import GateDecision, ProposedAction, fingerprint


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"     # request revision, with notes
    REJECT = "reject"


class RequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISE = "revise"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalOutcome(str, Enum):
    APPROVED = "approved"   # token minted
    PENDING = "pending"     # more approvals needed
    REVISE = "revise"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALID = "invalid"     # tampered, wrong action, unauthorized, or already used


@dataclass(frozen=True)
class Requirement:
    role: str
    approvals: int = 1  # distinct reviewers needed (tune)

    def __post_init__(self):
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("requirement role is required")
        if type(self.approvals) is not int or self.approvals < 1:
            raise ValueError("approvals must be a positive integer")


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ApprovalPolicy:
    """Who may approve what. TTLs and approval counts are (tune) values.

    request_ttl_seconds: how long a request waits for a decision (spec: 24 h).
    approval_ttl_seconds: how long an approve record stays usable after it is signed.
    on_expiry: per action type, ESCALATE (default) or BLOCK.
    """
    reviewers: Mapping[str, frozenset]
    requirements: Mapping[str, Requirement]
    request_ttl_seconds: float = 24 * 3600
    approval_ttl_seconds: float = 3600
    on_expiry: Mapping[str, GateDecision] = field(default_factory=dict)
    version: str = "1"

    def __post_init__(self):
        reviewers = {}
        for name, roles in dict(self.reviewers).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("reviewer names must be non-empty strings")
            roles = frozenset(roles)
            if not roles or not all(isinstance(r, str) and r.strip() for r in roles):
                raise ValueError(f"reviewer {name} needs at least one role")
            reviewers[name] = roles
        requirements = dict(self.requirements)
        if not all(isinstance(r, Requirement) for r in requirements.values()):
            raise ValueError("requirements must be Requirement objects")
        expiry = {k: GateDecision(v) for k, v in dict(self.on_expiry).items()}
        if any(v not in (GateDecision.ESCALATE, GateDecision.BLOCK) for v in expiry.values()):
            raise ValueError("on_expiry must be escalate or block")
        _positive(self.request_ttl_seconds, "request_ttl_seconds")
        _positive(self.approval_ttl_seconds, "approval_ttl_seconds")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version is required")
        object.__setattr__(self, "reviewers", MappingProxyType(reviewers))
        object.__setattr__(self, "requirements", MappingProxyType(requirements))
        object.__setattr__(self, "on_expiry", MappingProxyType(expiry))

    def requirement(self, action_type):
        if action_type not in self.requirements:
            raise ValueError(f"no approval requirement for action: {action_type}")
        return self.requirements[action_type]

    def may_approve(self, reviewer, action_type):
        requirement = self.requirements.get(action_type)
        return requirement is not None and requirement.role in self.reviewers.get(reviewer, ())

    def expiry_decision(self, action_type):
        return self.on_expiry.get(action_type, GateDecision.ESCALATE)

    @classmethod
    def from_dict(cls, data):
        return cls(reviewers={name: frozenset(roles) for name, roles in data["reviewers"].items()},
                   requirements={action: Requirement(**req) for action, req in data["requirements"].items()},
                   request_ttl_seconds=data.get("request_ttl_seconds", 24 * 3600),
                   approval_ttl_seconds=data.get("approval_ttl_seconds", 3600),
                   on_expiry=data.get("on_expiry", {}), version=str(data.get("version", "1")))

    @classmethod
    def from_file(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class ApprovalRequest:
    """What the reviewer sees: the exact action, its fingerprint, reason and evidence."""
    request_id: str
    run_id: str
    branch_id: str
    action: dict
    fingerprint: str
    reason: str
    evidence: dict
    created_at: float
    expires_at: float
    status: RequestStatus


@dataclass(frozen=True)
class ApprovalRecord:
    record_id: str
    request_id: str
    fingerprint: str
    reviewer: str
    role: str
    decision: ApprovalDecision
    notes: str
    decided_at: float
    expires_at: float
    single_use: bool
    signature: str

    def claims(self):
        data = asdict(self)
        del data["signature"]
        data["decision"] = self.decision.value
        return data


@dataclass(frozen=True)
class Verification:
    outcome: ApprovalOutcome
    reason: str
    request: ApprovalRequest | None = None
    record: ApprovalRecord | None = None
    token: object = None   # CapabilityToken when outcome is APPROVED
    notes: str = ""        # reviewer notes for REVISE / REJECT


class ApprovalError(PermissionError):
    """A decision the approval service refuses to record."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, branch_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL, policy_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_records (
    record_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(request_id),
    fingerprint TEXT NOT NULL, reviewer TEXT NOT NULL, role TEXT NOT NULL, decision TEXT NOT NULL,
    notes TEXT NOT NULL, decided_at REAL NOT NULL, expires_at REAL NOT NULL, single_use INTEGER NOT NULL,
    signature TEXT NOT NULL, UNIQUE (request_id, reviewer)
);
CREATE TRIGGER IF NOT EXISTS approval_records_no_update BEFORE UPDATE ON approval_records
BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_records_no_delete BEFORE DELETE ON approval_records
BEGIN SELECT RAISE(ABORT, 'approval records are immutable'); END;
CREATE TABLE IF NOT EXISTS approval_uses (
    request_id TEXT PRIMARY KEY REFERENCES approval_requests(request_id),
    record_id TEXT NOT NULL, used_at REAL NOT NULL
);
"""


class ApprovalService:
    """Approval requests, signed records and verification, on a SQLite connection.

    secret: the approval signing key (>= 32 bytes), distinct from the capability key.
    authority: the CapabilityAuthority that mints tokens after a verified approval. It may be
    None for a reviewer-only process (the approval CLI), which records decisions but cannot verify.
    audit(event_type, subject_id, payload) runs inside the same transaction.
    """

    def __init__(self, connection, *, secret, policy, authority, clock=time.time, audit=None):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("approval secret must be at least 32 bytes")
        if not isinstance(policy, ApprovalPolicy):
            raise TypeError("expected ApprovalPolicy")
        self.db, self._secret, self.policy = connection, secret, policy
        self.authority, self._clock, self._audit = authority, clock, audit
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # --- helpers -------------------------------------------------------------------------

    def _now(self):
        return float(self._clock())

    def _event(self, event_type, subject_id, payload):
        if self._audit is not None:
            self._audit(event_type, subject_id, payload)

    def _sign(self, claims):
        return hmac.new(self._secret, canonical(claims).encode(), sha256).hexdigest()

    def _signed(self, record):
        return hmac.compare_digest(self._sign(record.claims()), record.signature)

    @staticmethod
    def _request(row):
        if row is None:
            return None
        rid, run, branch, fp, action, reason, evidence, created, expires, status = tuple(row)[:10]
        return ApprovalRequest(rid, run, branch, json.loads(action), fp, reason, json.loads(evidence),
                               created, expires, RequestStatus(status))

    @staticmethod
    def _record(row):
        if row is None:
            return None
        values = list(tuple(row))
        values[5] = ApprovalDecision(values[5])
        values[9] = bool(values[9])
        return ApprovalRecord(*values)

    _REQUEST_COLUMNS = ("request_id,run_id,branch_id,fingerprint,action,reason,evidence,"
                        "created_at,expires_at,status")
    _RECORD_COLUMNS = ("record_id,request_id,fingerprint,reviewer,role,decision,notes,decided_at,"
                       "expires_at,single_use,signature")

    def get_request(self, request_id):
        return self._request(self.db.execute(
            f"SELECT {self._REQUEST_COLUMNS} FROM approval_requests WHERE request_id=?", (request_id,)).fetchone())

    def get_record(self, record_id):
        return self._record(self.db.execute(
            f"SELECT {self._RECORD_COLUMNS} FROM approval_records WHERE record_id=?", (record_id,)).fetchone())

    def records_for(self, request_id):
        rows = self.db.execute(f"SELECT {self._RECORD_COLUMNS} FROM approval_records WHERE request_id=? "
                               "ORDER BY rowid", (request_id,)).fetchall()
        return tuple(self._record(r) for r in rows)

    def requests(self, *, status=None, run_id=None):
        query, params, where = f"SELECT {self._REQUEST_COLUMNS} FROM approval_requests", [], []
        if status is not None:
            where.append("status=?")
            params.append(RequestStatus(status).value)
        if run_id is not None:
            where.append("run_id=?")
            params.append(run_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        return tuple(self._request(r) for r in self.db.execute(query + " ORDER BY rowid", params).fetchall())

    def _set_status(self, request_id, status):
        self.db.execute("UPDATE approval_requests SET status=? WHERE request_id=?", (status.value, request_id))

    # --- step 1-2: open a request --------------------------------------------------------

    def request(self, run_id, branch_id, action, *, reason, evidence=None):
        """Open a request, or return the pending one for the same run, branch and action
        (a re-run node must not open a second request)."""
        if not isinstance(action, ProposedAction):
            raise TypeError("expected ProposedAction")
        if not all(isinstance(v, str) and v.strip() for v in (run_id, branch_id, reason)):
            raise ValueError("run_id, branch_id and reason are required")
        self.policy.requirement(action.action_type)  # fail closed: no rule, no request
        evidence = {} if evidence is None else evidence
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be a JSON object")
        canonical(evidence)
        fp = fingerprint(action)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                f"SELECT {self._REQUEST_COLUMNS} FROM approval_requests WHERE run_id=? AND branch_id=? "
                "AND fingerprint=? AND status=? ORDER BY rowid DESC LIMIT 1",
                (run_id, branch_id, fp, RequestStatus.PENDING.value)).fetchone()
            if row is not None:
                return self._request(row)
            now = self._now()
            request_id = secrets.token_hex(16)
            self.db.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request_id, run_id, branch_id, fp, canonical(action.to_dict()), reason,
                             canonical(evidence), now, now + self.policy.request_ttl_seconds,
                             RequestStatus.PENDING.value, self.policy.version))
            self._event("approval_requested", run_id,
                        {"request_id": request_id, "branch_id": branch_id, "fingerprint": fp,
                         "action_type": action.action_type, "reason": reason})
        return self.get_request(request_id)

    # --- step 3: the reviewer's decision (host entry point only) ------------------------

    def decide(self, request_id, *, reviewer, decision, notes=""):
        """Record a signed decision. Called only by the authenticated host entry point."""
        decision = ApprovalDecision(decision)
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ApprovalError("reviewer identity is required")
        if not isinstance(notes, str) or (decision is not ApprovalDecision.APPROVE and not notes.strip()):
            raise ApprovalError("revise and reject need notes for the agent")
        # Refusals are raised after the transaction commits, so their audit events persist.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            refusal = self._decide(request_id, reviewer, decision, notes)
        if isinstance(refusal, ApprovalError):
            raise refusal
        return self.get_record(refusal)

    def _decide(self, request_id, reviewer, decision, notes):
        """Inside the decide() transaction. Returns the new record id, or an ApprovalError."""
        request = self.get_request(request_id)
        if request is None:
            return ApprovalError("unknown approval request")
        now = self._now()
        if request.status is RequestStatus.PENDING and now >= request.expires_at:
            self._set_status(request_id, RequestStatus.EXPIRED)
            self._event("approval_expired", request.run_id, {"request_id": request_id})
            return ApprovalError("approval request has expired")
        if request.status is not RequestStatus.PENDING:
            return ApprovalError(f"approval request is {request.status.value}")
        action_type = request.action["action_type"]
        if not self.policy.may_approve(reviewer, action_type):
            self._event("approval_refused", request.run_id,
                        {"request_id": request_id, "reviewer": reviewer, "reason": "not authorized"})
            return ApprovalError(f"{reviewer} may not decide {action_type}")
        requirement = self.policy.requirement(action_type)
        claims = {"record_id": secrets.token_hex(16), "request_id": request_id,
                  "fingerprint": request.fingerprint, "reviewer": reviewer, "role": requirement.role,
                  "decision": decision.value, "notes": notes, "decided_at": now,
                  "expires_at": now + self.policy.approval_ttl_seconds, "single_use": True}
        try:
            self.db.execute("INSERT INTO approval_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (*claims.values(), self._sign(claims)))
        except sqlite3.IntegrityError:
            return ApprovalError(f"{reviewer} already decided this request")
        if decision is ApprovalDecision.APPROVE:
            approvals = self.db.execute(
                "SELECT COUNT(DISTINCT reviewer) FROM approval_records WHERE request_id=? AND decision=?",
                (request_id, ApprovalDecision.APPROVE.value)).fetchone()[0]
            if approvals >= requirement.approvals:
                self._set_status(request_id, RequestStatus.APPROVED)
        else:
            self._set_status(request_id, RequestStatus.REVISE if decision is ApprovalDecision.REVISE
                             else RequestStatus.REJECTED)
        self._event("approval_decided", request.run_id,
                    {"request_id": request_id, "record_id": claims["record_id"], "reviewer": reviewer,
                     "decision": decision.value, "notes": notes, "fingerprint": request.fingerprint})
        return claims["record_id"]

    # --- step 5-6: verify and mint -------------------------------------------------------

    def _invalid(self, reason, request=None, record=None):
        self._event("approval_invalid", request.run_id if request else "-",
                    {"reason": reason, "record_id": record.record_id if record else None})
        return Verification(ApprovalOutcome.INVALID, reason, request, record)

    def verify(self, approval_record_id, action):
        """Check one record against the exact action. On APPROVED, consume the request
        (single use) and return a capability token for this fingerprint."""
        if not isinstance(action, ProposedAction):
            raise TypeError("expected ProposedAction")
        if self.authority is None:
            raise ValueError("this approval service has no capability authority; it cannot verify")
        if not isinstance(approval_record_id, str) or not approval_record_id:
            return self._invalid("approval record id is required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            record = self.get_record(approval_record_id)
            if record is None:
                return self._invalid("unknown approval record")
            request = self.get_request(record.request_id)
            if request is None or not self._signed(record):
                return self._invalid("approval record signature is invalid", request, record)
            action_fp = fingerprint(action)
            if not (hmac.compare_digest(record.fingerprint, action_fp)
                    and hmac.compare_digest(request.fingerprint, action_fp)):
                return self._invalid("approval was given for a different action", request, record)
            requirement = self.policy.requirements.get(action.action_type)
            if (requirement is None or record.role != requirement.role
                    or not self.policy.may_approve(record.reviewer, action.action_type)):
                return self._invalid("reviewer is no longer authorized for this action", request, record)

            now = self._now()
            if request.status is RequestStatus.PENDING and now >= request.expires_at:
                self._set_status(request.request_id, RequestStatus.EXPIRED)
                self._event("approval_expired", request.run_id, {"request_id": request.request_id})
                request = self.get_request(request.request_id)
            if request.status is RequestStatus.EXPIRED:
                return Verification(ApprovalOutcome.EXPIRED, "approval request expired", request, record)
            if request.status in (RequestStatus.REVISE, RequestStatus.REJECTED):
                closing = next((r for r in self.records_for(request.request_id)
                                if r.decision is not ApprovalDecision.APPROVE and self._signed(r)), record)
                outcome = (ApprovalOutcome.REVISE if request.status is RequestStatus.REVISE
                           else ApprovalOutcome.REJECTED)
                return Verification(outcome, f"reviewer chose {closing.decision.value}", request, closing,
                                    notes=closing.notes)
            if request.status is RequestStatus.PENDING:
                return Verification(ApprovalOutcome.PENDING, "more approvals are needed", request, record)

            # APPROVED: every counted approval must still be valid now.
            approvals = [r for r in self.records_for(request.request_id)
                         if r.decision is ApprovalDecision.APPROVE and self._signed(r)
                         and r.fingerprint == action_fp and self.policy.may_approve(r.reviewer, action.action_type)]
            if len({r.reviewer for r in approvals}) < requirement.approvals:
                return self._invalid("not enough valid approvals", request, record)
            if len({r.reviewer for r in approvals if now < r.expires_at}) < requirement.approvals:
                return Verification(ApprovalOutcome.EXPIRED, "approval has expired", request, record)
            try:
                self.db.execute("INSERT INTO approval_uses VALUES (?,?,?)",
                                (request.request_id, record.record_id, now))
            except sqlite3.IntegrityError:
                return self._invalid("approval has already been used", request, record)
            token = self.authority.mint(action.action_type, action_fp)
            self._event("approval_verified", request.run_id,
                        {"request_id": request.request_id, "record_id": record.record_id,
                         "fingerprint": action_fp, "token_expires_at": token.expires_at})
        return Verification(ApprovalOutcome.APPROVED, "approved", request, record, token=token)

    # --- step 8: expiry ------------------------------------------------------------------

    def expire_stale(self):
        """Expire pending requests past their deadline. Returns (request, GateDecision) pairs,
        the decision being ESCALATE or BLOCK per policy."""
        expired = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(f"SELECT {self._REQUEST_COLUMNS} FROM approval_requests "
                                   "WHERE status=? AND expires_at<=? ORDER BY rowid",
                                   (RequestStatus.PENDING.value, self._now())).fetchall()
            for row in rows:
                request = self._request(row)
                self._set_status(request.request_id, RequestStatus.EXPIRED)
                decision = self.policy.expiry_decision(request.action["action_type"])
                self._event("approval_expired", request.run_id,
                            {"request_id": request.request_id, "route": decision.value})
                expired.append((self.get_request(request.request_id), decision))
        return tuple(expired)
