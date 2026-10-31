"""Authentication (PBKDF2 + JWT) and authorization (RBAC for actions, ABAC clearance for data)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from enum import Enum

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import settings

CLEARANCE_LEVELS = ["public", "internal", "confidential", "restricted"]


def clearance_rank(level: str) -> int:
    return CLEARANCE_LEVELS.index(level)


def allowed_classifications(clearance: str) -> list[str]:
    return CLEARANCE_LEVELS[: clearance_rank(clearance) + 1]


class Perm(str, Enum):
    QUERY = "query"
    READ_DOCS = "docs:read"
    WRITE_DOCS = "docs:write"
    MANAGE_USERS = "users:manage"
    READ_AUDIT = "audit:read"
    READ_ANALYTICS = "analytics:read"
    MANAGE_ORG = "org:manage"


ROLE_PERMISSIONS: dict[str, set[Perm]] = {
    "viewer": {Perm.QUERY, Perm.READ_DOCS},
    "editor": {Perm.QUERY, Perm.READ_DOCS, Perm.WRITE_DOCS, Perm.READ_ANALYTICS},
    "admin": set(Perm),
}


@dataclass(frozen=True)
class Principal:
    user_id: int
    org_id: int
    email: str
    name: str
    role: str
    clearance: str

    def can(self, perm: Perm) -> bool:
        return perm in ROLE_PERMISSIONS[self.role]

    @property
    def classifications(self) -> list[str]:
        return allowed_classifications(self.clearance)


# --- passwords -------------------------------------------------------------------

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
    return hmac.compare_digest(digest.hex(), digest_hex)


# --- tokens ----------------------------------------------------------------------


def issue_token(user_id: int) -> str:
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + settings.jwt_ttl_minutes * 60}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


_bearer = HTTPBearer(auto_error=False)


def load_principal(user_id: int) -> Principal | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, org_id, email, name, role, clearance, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None or not row["active"]:
        return None
    return Principal(row["id"], row["org_id"], row["email"], row["name"], row["role"], row["clearance"])


def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise unauthorized
    # Role and clearance are re-read on every request, so revocations apply immediately.
    principal = load_principal(int(payload["sub"]))
    if principal is None:
        raise unauthorized
    return principal


def require(perm: Perm):
    def dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(perm):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role '{principal.role}' lacks permission '{perm.value}'")
        return principal

    return dep
