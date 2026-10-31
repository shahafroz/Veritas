"""Demo tenant: a fictional company, four users at different roles/clearances, and a classified corpus."""
from __future__ import annotations

import os
from pathlib import Path

from . import db, rag
from .retrieval import IndexManager
from .security import Principal, hash_password

SEED_DIR = Path(__file__).resolve().parent.parent / "seed_docs"
DEMO_ORG = "Helix Systems"
# Local demo credentials only; production deploys set SEED_DEMO=false.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "veritas-demo")

DEMO_USERS = [
    ("Ada Admin", "admin@helix.demo", "admin", "restricted"),
    ("Erin Editor", "editor@helix.demo", "editor", "confidential"),
    ("Iris Analyst", "analyst@helix.demo", "viewer", "confidential"),
    ("Victor Viewer", "viewer@helix.demo", "viewer", "internal"),
]

# filename -> classification
DEMO_DOCS = {
    "product-faq.md": "public",
    "employee-handbook.md": "internal",
    "information-security-policy.md": "internal",
    "expense-and-travel-policy.md": "internal",
    "incident-response-runbook.md": "confidential",
    "vendor-contract-summary.md": "confidential",
    "compensation-bands-2026.md": "restricted",
    "board-memo-q3-2026.md": "restricted",
}


def seed_demo(indexes: IndexManager) -> int:
    """Create the demo org if missing. Returns its org id."""
    with db.write() as conn:
        org = conn.execute("SELECT id FROM orgs WHERE name = ?", (DEMO_ORG,)).fetchone()
        if org:
            return org["id"]
        org_id = conn.execute("INSERT INTO orgs (name, monthly_budget_usd) VALUES (?, 25)", (DEMO_ORG,)).lastrowid
        for name, email, role, clearance in DEMO_USERS:
            conn.execute("INSERT INTO users (org_id, email, name, password_hash, role, clearance) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (org_id, email, name, hash_password(DEMO_PASSWORD), role, clearance))
        admin_id = conn.execute("SELECT id FROM users WHERE email = 'admin@helix.demo'").fetchone()["id"]

    admin = Principal(admin_id, org_id, "admin@helix.demo", "Ada Admin", "admin", "restricted")
    for filename, classification in DEMO_DOCS.items():
        rag.ingest_document(indexes, admin, filename, (SEED_DIR / filename).read_bytes(), None, classification)
    return org_id
