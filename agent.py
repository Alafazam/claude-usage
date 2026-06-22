"""
agent.py - Team-mode client: scan locally, push metrics-only to the server.

Reads new turns from the LOCAL scanner DB, projects them onto the metrics-only
payload (metrics.py), and POSTs them to the team server with the developer's
access key. The full cwd, git branch, and all content stay on the machine — the
forbidden-field guard runs before every POST.

Durable client state lives in a properties file (~/.claude/team.conf): the
server URL, the access key, the developer's declared email, a once-generated
install_uuid, and a push watermark (the highest local turn id already sent).
Env vars override the file for injection (CI / secret managers).
"""

import configparser
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import metrics
import scanner

logger = logging.getLogger("claude_usage.agent")

CONFIG_PATH = Path.home() / ".claude" / "team.conf"

# Fixed cadence for the looped agent (no per-run flag, by design).
PUSH_INTERVAL_SECONDS = 300

# Chunk large backlogs so a single request stays well under the server cap.
PUSH_BATCH_SIZE = 1000


class PushError(Exception):
    """Raised when a push cannot complete (bad config, auth, or network)."""


# ── Config (properties file + env overrides) ────────────────────────────────────

def _load_parser(path):
    parser = configparser.ConfigParser()
    if Path(path).exists():
        parser.read(path)
    if not parser.has_section("team"):
        parser.add_section("team")
    return parser


def _save_parser(parser, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)


def resolve_settings(parser):
    """Resolve server_url / key / email with env taking precedence over file."""
    def file_value(key):
        return parser.get("team", key, fallback=None) or None
    return {
        "server_url": os.environ.get("CLAUDE_USAGE_SERVER_URL") or file_value("server_url"),
        "key": os.environ.get("CLU_KEY") or file_value("key"),
        "email": os.environ.get("CLU_EMAIL") or file_value("email"),
    }


def ensure_install_uuid(parser, path):
    """Return the install_uuid, generating and persisting one on first run."""
    existing = parser.get("team", "install_uuid", fallback=None)
    if existing:
        return existing
    new_uuid = str(uuid.uuid4())
    parser.set("team", "install_uuid", new_uuid)
    _save_parser(parser, path)
    return new_uuid


def get_watermark(parser):
    try:
        return parser.getint("team", "last_pushed_id", fallback=0)
    except ValueError:
        return 0


def set_watermark(parser, path, value):
    parser.set("team", "last_pushed_id", str(value))
    _save_parser(parser, path)


# ── Local DB read ───────────────────────────────────────────────────────────────

def _read_new_turns(db_path, watermark):
    """Read local turns past the watermark, metric columns only (+ cwd, used
    solely to derive the basename project_name and never transmitted)."""
    db_path = db_path or scanner.DB_PATH
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("""
            SELECT id, session_id, message_id, timestamp, model,
                   input_tokens, output_tokens, cache_read_tokens,
                   cache_creation_tokens, tool_name, cwd
            FROM turns
            WHERE message_id IS NOT NULL AND message_id != '' AND id > ?
            ORDER BY id
        """, (watermark,)).fetchall()
    finally:
        conn.close()


def _chunks(rows, size):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


# ── Network ─────────────────────────────────────────────────────────────────────

def _post_ingest(server_url, key, payload):
    url = server_url.rstrip("/") + "/api/ingest"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Push ─────────────────────────────────────────────────────────────────────────

def push_once(db_path=None, config_path=CONFIG_PATH):
    """Push all local turns past the watermark. Returns {received, inserted,
    deduped}. The watermark advances only for batches the server accepted, so a
    failure is safely retried next run (ingest is idempotent)."""
    parser = _load_parser(config_path)
    settings = resolve_settings(parser)
    if not settings["server_url"] or not settings["key"]:
        raise PushError(
            "server_url and access key required (set in %s or via "
            "CLAUDE_USAGE_SERVER_URL / CLU_KEY)" % config_path)
    if not settings["email"]:
        raise PushError("email required (set in %s or via CLU_EMAIL)" % config_path)

    install_uuid = ensure_install_uuid(parser, config_path)
    watermark = get_watermark(parser)
    rows = _read_new_turns(db_path, watermark)
    if not rows:
        logger.info("nothing new to push (watermark id=%d)", watermark)
        return {"received": 0, "inserted": 0, "deduped": 0}

    total = {"received": 0, "inserted": 0, "deduped": 0}
    for chunk in _chunks(rows, PUSH_BATCH_SIZE):
        payload = metrics.build_push_payload(chunk, install_uuid, settings["email"])
        try:
            result = _post_ingest(settings["server_url"], settings["key"], payload)
        except urllib.error.HTTPError as exc:
            raise PushError("server returned HTTP %d" % exc.code)
        except urllib.error.URLError as exc:
            raise PushError("could not reach server: %s" % exc.reason)
        for key in total:
            total[key] += int(result.get(key, 0) or 0)
        # Advance the watermark only after the server confirms this chunk.
        set_watermark(parser, config_path, chunk[-1]["id"])

    logger.info("pushed received=%d inserted=%d deduped=%d",
                total["received"], total["inserted"], total["deduped"])
    return total


def scan_and_push(db_path=None, config_path=CONFIG_PATH):
    """Scan the local transcripts, then push. Used by both `push` and `agent`."""
    db_path = db_path or scanner.DB_PATH
    scanner.scan(db_path=db_path, verbose=False)
    return push_once(db_path=db_path, config_path=config_path)


def run_agent_loop(db_path=None, config_path=CONFIG_PATH, interval=PUSH_INTERVAL_SECONDS):
    """Scan + push on a fixed cadence until interrupted. Push failures are
    logged and retried next cycle rather than killing the loop."""
    logger.info("agent loop started (interval=%ds)", interval)
    try:
        while True:
            try:
                scan_and_push(db_path=db_path, config_path=config_path)
            except PushError as exc:
                logger.error("push failed: %s", exc)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("agent stopped")
