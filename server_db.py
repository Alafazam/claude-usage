"""
server_db.py - Team-server SQLite schema and queries.

This is a SEPARATE schema from the local scanner DB (scanner.init_db), for two
reasons:
  1. The local unique index dedupes on message_id alone; a shared server must
     dedupe on (user_id, message_id) so two developers' identical message ids
     don't collide.
  2. The server stores metrics ONLY — it has no `cwd` or `git_branch` column,
     so the schema itself documents the privacy contract.

Metric column names match the local schema so the manager-dashboard SQL stays
close to dashboard.get_dashboard_data and the dashboard JS (PRICING/calcCost)
can be reused unchanged.

Side effects here are SQLite-only. Identity/clock writes (created_at,
last_seen_at) live in auth.py; "now"/staleness windows are the caller's concern.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "team-usage.db"

# Wait up to this long for a writer to release the lock before erroring with
# "database is locked". WAL handles readers; this covers concurrent ingest.
BUSY_TIMEOUT_MS = 5000

# Normalised model expression — collapses NULL and '' into a single 'unknown'
# bucket (same guard as the local dashboard, see dashboard.py).
_MODEL_EXPR = "COALESCE(NULLIF(model, ''), 'unknown')"


def get_conn(db_path=None):
    """Open a server DB connection with row access and a busy timeout."""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % BUSY_TIMEOUT_MS)
    return conn


def _ensure_column(conn, table, column, decl):
    """Additive migration: add `column` to `table` if it isn't already present.

    Mirrors the scanner's ad-hoc upgrade idiom so existing server DBs keep
    working when the schema grows.
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def init_server_db(conn):
    """Create the server schema (idempotent) and enable WAL."""
    # WAL persists on the DB file; setting it each start is harmless. Must run
    # outside an open transaction, which a fresh connection satisfies.
    conn.execute("PRAGMA journal_mode = WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE NOT NULL,
            is_admin    INTEGER DEFAULT 0,
            created_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS access_keys (
            key_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash     TEXT UNIQUE NOT NULL,
            user_id      INTEGER NOT NULL,
            label        TEXT,
            created_at   TEXT,
            last_seen_at TEXT,
            revoked_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS server_turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                 INTEGER NOT NULL,
            install_uuid            TEXT,
            session_id              TEXT,
            project_name            TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            message_id              TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_server_turns_user ON server_turns(user_id);
        CREATE INDEX IF NOT EXISTS idx_server_turns_ts   ON server_turns(timestamp);

        -- Dedup discipline: like the local conditional unique index, but keyed
        -- on (user_id, message_id) so identical message ids from different
        -- developers both insert.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_server_turns_user_msg
            ON server_turns(user_id, message_id)
            WHERE message_id IS NOT NULL AND message_id != '';
    """)
    conn.commit()


def upsert_turn_metrics(conn, user_id, install_uuid, turns):
    """Insert metric rows for a user, deduping on (user_id, message_id).

    Idempotent: re-pushing the same batch inserts nothing. Returns
    {received, inserted, deduped}. `deduped` includes any row whose
    (user_id, message_id) already existed.
    """
    received = len(turns)
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO server_turns
            (user_id, install_uuid, session_id, project_name, timestamp, model,
             input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
             tool_name, message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            user_id,
            install_uuid,
            t.get("session_id"),
            t.get("project_name"),
            t.get("timestamp"),
            t.get("model"),
            int(t.get("input_tokens") or 0),
            int(t.get("output_tokens") or 0),
            int(t.get("cache_read_tokens") or 0),
            int(t.get("cache_creation_tokens") or 0),
            t.get("tool_name"),
            t.get("message_id"),
        )
        for t in turns
    ])
    conn.commit()
    inserted = conn.total_changes - before
    return {"received": received, "inserted": inserted, "deduped": received - inserted}


def mcp_server_from_tool(tool_name):
    """Extract the server segment from an MCP tool name `mcp__<server>__<tool>`.

    Returns the server name, or None if `tool_name` isn't an MCP tool.
    """
    if not tool_name or not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__")
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    return None


# ── Query building blocks (org-wide when user_id is None) ───────────────────────

def _where(user_id):
    """Return (sql_clause, params) restricting to a user, or org-wide."""
    if user_id is None:
        return "", ()
    return "WHERE user_id = ?", (user_id,)


def _all_models(conn, user_id):
    clause, params = _where(user_id)
    rows = conn.execute("""
        SELECT %s as model
        FROM server_turns %s
        GROUP BY %s
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """ % (_MODEL_EXPR, clause, _MODEL_EXPR), params).fetchall()
    return [r["model"] for r in rows]


def _daily_by_model(conn, user_id):
    clause, params = _where(user_id)
    rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            %s                         as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM server_turns %s
        GROUP BY day, %s
        ORDER BY day, model
    """ % (_MODEL_EXPR, clause, _MODEL_EXPR), params).fetchall()
    return [{
        "day": r["day"],
        "model": r["model"],
        "input": r["input"] or 0,
        "output": r["output"] or 0,
        "cache_read": r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns": r["turns"] or 0,
    } for r in rows]


def _hourly_by_model(conn, user_id):
    clause, params = _where(user_id)
    # length(timestamp) >= 13 guards the hour substring, matching the dashboard.
    extra = "AND" if clause else "WHERE"
    rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)                  as day,
            CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
            %s                                        as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM server_turns %s %s timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, %s
        ORDER BY day, hour, model
    """ % (_MODEL_EXPR, clause, extra, _MODEL_EXPR), params).fetchall()
    return [{
        "day": r["day"],
        "hour": r["hour"] if r["hour"] is not None else 0,
        "model": r["model"],
        "output": r["output"] or 0,
        "turns": r["turns"] or 0,
    } for r in rows]


def _tool_freq(conn, user_id):
    clause, params = _where(user_id)
    extra = "AND" if clause else "WHERE"
    rows = conn.execute("""
        SELECT tool_name, COUNT(*) as turns
        FROM server_turns %s %s tool_name IS NOT NULL AND tool_name != ''
        GROUP BY tool_name
        ORDER BY turns DESC
    """ % (clause, extra), params).fetchall()
    return [{"tool_name": r["tool_name"], "turns": r["turns"] or 0} for r in rows]


def _mcp_servers(conn, user_id):
    """Aggregate MCP-tool rows into per-server {turns, users} counts."""
    clause, params = _where(user_id)
    extra = "AND" if clause else "WHERE"
    rows = conn.execute("""
        SELECT tool_name, user_id, COUNT(*) as turns
        FROM server_turns %s %s tool_name GLOB 'mcp__*'
        GROUP BY tool_name, user_id
    """ % (clause, extra), params).fetchall()
    by_server = {}
    for r in rows:
        server = mcp_server_from_tool(r["tool_name"])
        if not server:
            continue
        agg = by_server.setdefault(server, {"server": server, "turns": 0, "_users": set()})
        agg["turns"] += r["turns"] or 0
        agg["_users"].add(r["user_id"])
    result = [{"server": a["server"], "turns": a["turns"], "users": len(a["_users"])}
              for a in by_server.values()]
    result.sort(key=lambda a: a["turns"], reverse=True)
    return result


def _projects(conn, user_id):
    clause, params = _where(user_id)
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(project_name, ''), 'unknown') as project_name,
            SUM(input_tokens)  as input,
            SUM(output_tokens) as output,
            COUNT(*)           as turns,
            COUNT(DISTINCT user_id) as users
        FROM server_turns %s
        GROUP BY COALESCE(NULLIF(project_name, ''), 'unknown')
        ORDER BY (SUM(input_tokens) + SUM(output_tokens)) DESC
    """ % clause, params).fetchall()
    return [{
        "project_name": r["project_name"],
        "input": r["input"] or 0,
        "output": r["output"] or 0,
        "turns": r["turns"] or 0,
        "users": r["users"] or 0,
    } for r in rows]


def _totals(conn, user_id):
    clause, params = _where(user_id)
    r = conn.execute("""
        SELECT
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns,
            COUNT(DISTINCT session_id) as sessions,
            COUNT(DISTINCT substr(timestamp, 1, 10)) as active_days
        FROM server_turns %s
    """ % clause, params).fetchone()
    return {
        "input": r["input"] or 0,
        "output": r["output"] or 0,
        "cache_read": r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns": r["turns"] or 0,
        "sessions": r["sessions"] or 0,
        "active_days": r["active_days"] or 0,
    }


def _leaderboard(conn):
    """Per-developer usage roll-up joined to enrollment, for the org view."""
    rows = conn.execute("""
        SELECT
            u.user_id                  as user_id,
            u.email                    as email,
            SUM(t.input_tokens)        as input,
            SUM(t.output_tokens)       as output,
            SUM(t.cache_read_tokens)   as cache_read,
            SUM(t.cache_creation_tokens) as cache_creation,
            COUNT(t.id)                as turns,
            COUNT(DISTINCT t.session_id) as sessions
        FROM users u
        LEFT JOIN server_turns t ON t.user_id = u.user_id
        GROUP BY u.user_id, u.email
        ORDER BY (COALESCE(SUM(t.input_tokens), 0) + COALESCE(SUM(t.output_tokens), 0)) DESC
    """).fetchall()
    return [{
        "user_id": r["user_id"],
        "email": r["email"],
        "input": r["input"] or 0,
        "output": r["output"] or 0,
        "cache_read": r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns": r["turns"] or 0,
        "sessions": r["sessions"] or 0,
    } for r in rows]


def get_enrollment(conn):
    """One row per enrolled developer with the most-recent report time.

    `last_seen_at` is the max across that user's active access keys (NULL if
    they hold a key but have never reported). The caller decides the staleness
    window for the "who hasn't reported" view, keeping this clock-free.
    """
    rows = conn.execute("""
        SELECT
            u.user_id            as user_id,
            u.email              as email,
            MAX(k.last_seen_at)  as last_seen_at,
            COUNT(k.key_id)      as active_keys
        FROM users u
        JOIN access_keys k ON k.user_id = u.user_id AND k.revoked_at IS NULL
        GROUP BY u.user_id, u.email
        ORDER BY u.email
    """).fetchall()
    return [{
        "user_id": r["user_id"],
        "email": r["email"],
        "last_seen_at": r["last_seen_at"],
        "active_keys": r["active_keys"] or 0,
    } for r in rows]


def get_team_data(conn):
    """Org-wide manager snapshot. Keys `all_models`/`daily_by_model`/
    `hourly_by_model` match the local dashboard shape so the SPA reuses them.
    """
    return {
        "all_models": _all_models(conn, None),
        "daily_by_model": _daily_by_model(conn, None),
        "hourly_by_model": _hourly_by_model(conn, None),
        "tool_freq": _tool_freq(conn, None),
        "mcp_servers": _mcp_servers(conn, None),
        "projects": _projects(conn, None),
        "totals": _totals(conn, None),
        "users": _leaderboard(conn),
        "enrollment": get_enrollment(conn),
    }


def get_user_detail(conn, user_id):
    """Per-developer drill-down. Returns None if the user doesn't exist."""
    user = conn.execute(
        "SELECT user_id, email FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if user is None:
        return None
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "all_models": _all_models(conn, user_id),
        "daily_by_model": _daily_by_model(conn, user_id),
        "hourly_by_model": _hourly_by_model(conn, user_id),
        "tool_freq": _tool_freq(conn, user_id),
        "mcp_servers": _mcp_servers(conn, user_id),
        "projects": _projects(conn, user_id),
        "totals": _totals(conn, user_id),
    }
