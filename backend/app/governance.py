"""Governance controls: PII redaction, prompt-injection screening, tamper-evident audit, rate limits."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import db

# --- PII redaction -----------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")),
]


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII with typed placeholders. Card numbers must pass a Luhn check."""
    counts: dict[str, int] = defaultdict(int)
    for label, pattern in _PII_PATTERNS:
        def sub(m: re.Match[str], label: str = label) -> str:
            if label == "CARD" and not _luhn_ok(m.group(0)):
                return m.group(0)
            counts[label] += 1
            return f"[REDACTED:{label}]"

        text = pattern.sub(sub, text)
    return text, dict(counts)


# --- prompt injection ------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?|rules)",
    r"disregard (all |the )?(previous|prior|above|system)",
    r"you are now (a|an|in) ",
    r"(reveal|print|show|repeat) (your|the) (system prompt|hidden instructions|hidden prompt)",
    r"(reveal|print|repeat) your (instructions|prompt)",
    r"</?(system|assistant|instructions?)>",
    r"act as (an? )?(unrestricted|jailbroken|dan)",
    r"do not (cite|mention) (the )?sources",
    r"exfiltrate|send (this|the data) to http",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def injection_signals(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _INJECTION_RE.finditer(text)})


# --- audit trail ---------------------------------------------------------------------

GENESIS = "0" * 64


def _row_hash(prev_hash: str, org_id: int, user_id: int | None, action: str, resource: str,
              detail: str, ts: str) -> str:
    material = json.dumps([prev_hash, org_id, user_id, action, resource, detail, ts], separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def audit(org_id: int, user_id: int | None, action: str, resource: str, detail: dict | None = None) -> None:
    detail_json = json.dumps(detail or {}, sort_keys=True, default=str)
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db.write() as conn:
        last = conn.execute("SELECT hash FROM audit_log WHERE org_id = ? ORDER BY id DESC LIMIT 1",
                            (org_id,)).fetchone()
        prev = last["hash"] if last else GENESIS
        h = _row_hash(prev, org_id, user_id, action, resource, detail_json, ts)
        conn.execute(
            "INSERT INTO audit_log (org_id, user_id, action, resource, detail, ts, prev_hash, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, user_id, action, resource, detail_json, ts, prev, h),
        )


def verify_audit_chain(org_id: int) -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log WHERE org_id = ? ORDER BY id", (org_id,)).fetchall()
    prev = GENESIS
    for row in rows:
        expected = _row_hash(prev, row["org_id"], row["user_id"], row["action"], row["resource"],
                             row["detail"], row["ts"])
        if row["prev_hash"] != prev or row["hash"] != expected:
            return {"valid": False, "entries": len(rows), "broken_at_id": row["id"]}
        prev = row["hash"]
    return {"valid": True, "entries": len(rows), "head": prev}


# --- rate limiting ---------------------------------------------------------------------


class TokenBucket:
    """Per-key token bucket; refills `rate_per_min` tokens per minute up to that capacity."""

    def __init__(self, rate_per_min: int):
        self.capacity = float(rate_per_min)
        self.refill_per_sec = rate_per_min / 60.0
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, seconds_until_next_token)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_sec)
            if tokens >= 1:
                self._state[key] = (tokens - 1, now)
                return True, 0.0
            self._state[key] = (tokens, now)
            return False, (1 - tokens) / self.refill_per_sec
