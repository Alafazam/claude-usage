"""
auth.py - Access-key lifecycle and request authentication for the team server.

"Access keys" (not "tokens") are the per-developer credentials a manager hands
out — kept verbally distinct from AI *tokens* (the usage metric). Each key is a
`clu_`-prefixed random string, shown once on creation and stored only as a
sha256 hash.

Two authentication paths:
  - Ingest is ALWAYS access-key based (agents are non-interactive machines).
  - The manager UI uses resolve_ui_identity: in `local` mode the admin presents
    their access key as a bearer token; in `proxy` mode identity comes from a
    trusted reverse-proxy header (SSO terminated upstream).

Side effects (DB writes, clock reads, hashing) live here at the edge; callers
pass in the connection.
"""

import hashlib
import secrets
from datetime import datetime, timezone

KEY_PREFIX = "clu_"
DEFAULT_SSO_HEADER = "X-Forwarded-Email"
DEFAULT_ADMIN_EMAIL = "admin@local"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_email(email):
    return (email or "").strip().lower()


def generate_key():
    """Return a fresh raw access key. Shown to the admin once; never stored raw."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw):
    """sha256 hex of a raw key. The only form persisted server-side."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_user(conn, email, is_admin=False):
    """Return the user_id for `email`, creating the row if needed.

    Lowercases the email (canonical identity). Promotes an existing user to
    admin when is_admin is requested, but never demotes.
    """
    email = _norm_email(email)
    row = conn.execute(
        "SELECT user_id, is_admin FROM users WHERE email = ?", (email,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (email, is_admin, created_at) VALUES (?, ?, ?)",
            (email, 1 if is_admin else 0, _now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    if is_admin and not row["is_admin"]:
        conn.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (row["user_id"],))
        conn.commit()
    return row["user_id"]


def create_key(conn, email, label=None, is_admin=False):
    """Create an access key for `email` and return the RAW key (shown once)."""
    user_id = get_or_create_user(conn, email, is_admin=is_admin)
    raw = generate_key()
    conn.execute(
        "INSERT INTO access_keys (key_hash, user_id, label, created_at) VALUES (?, ?, ?, ?)",
        (hash_key(raw), user_id, label, _now_iso()),
    )
    conn.commit()
    return raw


def create_keys(conn, emails, label=None, is_admin=False):
    """Create one access key per email. Returns [{email, key}] with raw keys."""
    result = []
    for email in emails:
        email = _norm_email(email)
        if not email:
            continue
        raw = create_key(conn, email, label=label, is_admin=is_admin)
        result.append({"email": email, "key": raw})
    return result


def lookup_key(conn, raw):
    """Resolve a raw key to its identity, or None if unknown or revoked.

    Returns {key_id, user_id, email, is_admin}. Identity is derived entirely
    server-side from the key — callers must never trust a client-asserted email.
    """
    if not raw:
        return None
    row = conn.execute("""
        SELECT k.key_id, k.user_id, k.revoked_at, u.email, u.is_admin
        FROM access_keys k
        JOIN users u ON u.user_id = k.user_id
        WHERE k.key_hash = ?
    """, (hash_key(raw),)).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    return {
        "key_id": row["key_id"],
        "user_id": row["user_id"],
        "email": row["email"],
        "is_admin": bool(row["is_admin"]),
    }


def revoke_key(conn, key_id):
    """Mark a key revoked. Idempotent — re-revoking keeps the original time."""
    conn.execute(
        "UPDATE access_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE key_id = ?",
        (_now_iso(), key_id),
    )
    conn.commit()


def touch_last_seen(conn, key_id):
    """Record that a key just reported successfully (powers the adoption view)."""
    conn.execute(
        "UPDATE access_keys SET last_seen_at = ? WHERE key_id = ?",
        (_now_iso(), key_id),
    )
    conn.commit()


def list_keys(conn):
    """List key metadata for the admin UI. Never includes the hash or raw key."""
    rows = conn.execute("""
        SELECT k.key_id, u.email, k.label, k.created_at, k.last_seen_at, k.revoked_at
        FROM access_keys k
        JOIN users u ON u.user_id = k.user_id
        ORDER BY u.email, k.key_id
    """).fetchall()
    return [{
        "key_id": r["key_id"],
        "email": r["email"],
        "label": r["label"],
        "created_at": r["created_at"],
        "last_seen_at": r["last_seen_at"],
        "status": "revoked" if r["revoked_at"] else "active",
    } for r in rows]


def bootstrap_admin(conn, admin_key=None, admin_email=None, admin_emails=None):
    """Seed admin access on startup. Idempotent.

    `local` mode: `admin_key` (from CLAUDE_USAGE_ADMIN_KEY) is hashed and stored
    against an admin user, so the operator can use that known raw key as bearer.
    Re-running with the same key is a no-op (looked up by hash).

    `proxy` mode: `admin_emails` (from CLAUDE_USAGE_ADMIN_EMAILS) are marked
    is_admin so SSO-authenticated managers reach the dashboards.
    """
    admin_email = _norm_email(admin_email or DEFAULT_ADMIN_EMAIL)

    if admin_key:
        key_hash = hash_key(admin_key)
        existing = conn.execute(
            "SELECT key_id FROM access_keys WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        if existing is None:
            user_id = get_or_create_user(conn, admin_email, is_admin=True)
            conn.execute(
                "INSERT INTO access_keys (key_hash, user_id, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key_hash, user_id, "bootstrap-admin", _now_iso()),
            )
            conn.commit()

    for email in (admin_emails or []):
        if _norm_email(email):
            get_or_create_user(conn, email, is_admin=True)


def extract_bearer(headers):
    """Pull the bearer credential from an Authorization header, or None."""
    value = headers.get("Authorization", "") if headers else ""
    if value.startswith("Bearer "):
        return value[len("Bearer "):].strip()
    return None


def authenticate_bearer(conn, headers):
    """Authenticate a request by its bearer access key. Returns identity or None."""
    return lookup_key(conn, extract_bearer(headers))


def resolve_ui_identity(conn, headers, cfg):
    """Resolve the identity behind a manager-UI request.

    `local` mode → bearer access key (admin presents their key). `proxy` mode →
    the configured SSO header (default X-Forwarded-Email), trusted because an
    upstream proxy authenticated it. Returns {email, user_id, is_admin} or None.
    The caller decides 401 (no identity) vs 403 (identity but not admin).
    """
    mode = (cfg.get("auth_mode") or "local").lower()

    if mode == "proxy":
        header_name = cfg.get("sso_header") or DEFAULT_SSO_HEADER
        email = headers.get(header_name) if headers else None
        if not email:
            return None
        email = _norm_email(email)
        row = conn.execute(
            "SELECT user_id, is_admin FROM users WHERE email = ?", (email,)
        ).fetchone()
        if row is None:
            return {"email": email, "user_id": None, "is_admin": False}
        return {"email": email, "user_id": row["user_id"], "is_admin": bool(row["is_admin"])}

    return authenticate_bearer(conn, headers)
