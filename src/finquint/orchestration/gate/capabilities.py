"""Short-lived signed capability tokens, each bound to one tool and one fingerprint (ADR-003)."""
from dataclasses import asdict, dataclass
import hmac
from hashlib import sha256
import secrets
import time

from finquint.autonomous.models import canonical

from .decide import GateDecision, fingerprint


class CapabilityError(PermissionError):
    """A side-effecting tool was called without a valid token for this exact action."""


@dataclass(frozen=True)
class CapabilityToken:
    tool: str
    fingerprint: str
    issued_at: float
    expires_at: float
    nonce: str
    signature: str

    def claims(self):
        data = asdict(self)
        del data["signature"]
        return data


class CapabilityAuthority:
    """Mints and verifies tokens. The secret and TTL come from the host, never from code.

    ttl_seconds is a (tune) value; the spec's starting point is 600 (10 minutes).
    """

    def __init__(self, secret: bytes, *, ttl_seconds: float, clock=time.time):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("capability secret must be at least 32 bytes")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._secret, self.ttl_seconds, self._clock = secret, ttl_seconds, clock

    def _sign(self, claims):
        return hmac.new(self._secret, canonical(claims).encode(), sha256).hexdigest()

    def mint(self, tool, action_fingerprint):
        if not isinstance(tool, str) or not tool or not isinstance(action_fingerprint, str) or len(action_fingerprint) != 64:
            raise ValueError("tool name and sha256 fingerprint required")
        now = float(self._clock())
        claims = {"tool": tool, "fingerprint": action_fingerprint, "issued_at": now,
                  "expires_at": now + self.ttl_seconds, "nonce": secrets.token_hex(16)}
        return CapabilityToken(**claims, signature=self._sign(claims))

    def grant(self, action, decision):
        """Token for an action the gate let through automatically; None otherwise.

        REQUEST_HUMAN actions get their token only after a verified approval (Phase 3).
        """
        if decision is not GateDecision.AUTO_CONTINUE:
            return None
        return self.mint(action.action_type, fingerprint(action))

    def verify(self, token, *, tool, action_fingerprint):
        if not isinstance(token, CapabilityToken):
            raise CapabilityError(f"{tool} requires a capability token")
        if not hmac.compare_digest(self._sign(token.claims()), token.signature):
            raise CapabilityError("capability token signature is invalid")
        if token.tool != tool:
            raise CapabilityError("capability token was issued for a different tool")
        if not hmac.compare_digest(token.fingerprint, action_fingerprint):
            raise CapabilityError("capability token was issued for a different action")
        now = float(self._clock())
        if not token.issued_at <= now < token.expires_at:
            raise CapabilityError("capability token is expired or not yet valid")
