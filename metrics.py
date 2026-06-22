"""
metrics.py - Pure metric extraction for team mode.

Transforms locally-scanned turn rows into the metrics-only payload that the
agent pushes to a team server. No prompt text, code, thinking text, full cwd,
or git branch ever appears in the output.

These functions are PURE: no database, network, or filesystem access. The side
effects (reading the local DB, the network POST, the server-side insert) live
at the edges in agent.py / team_server.py. Keeping extraction pure makes the
privacy guard trivially testable in isolation.
"""

CLIENT_VERSION = "team-1"

# The only keys allowed in a per-turn metric dict on the wire. The payload is
# built by whitelisting onto these fields, and assert_no_forbidden_fields()
# rejects anything outside this set.
ALLOWED_TURN_FIELDS = frozenset({
    "session_id",
    "message_id",
    "timestamp",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "tool_name",
    "project_name",
})

# The only keys allowed at the top level of a push payload.
ALLOWED_PAYLOAD_FIELDS = frozenset({
    "install_uuid",
    "email",
    "client_version",
    "turns",
})


class ForbiddenFieldError(Exception):
    """Raised when a payload would carry a field outside the privacy contract."""


def project_basename(cwd):
    """Reduce a cwd path to its last path component only (the basename).

    The full cwd never leaves the machine; only this single trailing directory
    name is reported, so usage can be grouped per project without revealing the
    filesystem layout, parent directories, or usernames. Handles both POSIX and
    Windows separators. Returns "unknown" for empty/None input.
    """
    if not cwd:
        return "unknown"
    parts = [p for p in cwd.replace("\\", "/").rstrip("/").split("/") if p]
    return parts[-1] if parts else "unknown"


def _field(row, key, default=None):
    """Read a key from a dict or sqlite3.Row, returning default if absent/None."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def extract_turn_metrics(row):
    """Project a local turn row onto the metrics-only wire shape (pure).

    `row` may be a dict or a sqlite3.Row from the local `turns` table. `cwd` is
    read solely to derive the basename `project_name` and is then discarded —
    it is never placed in the returned dict. The result's keys are always a
    subset of ALLOWED_TURN_FIELDS.
    """
    return {
        "session_id": _field(row, "session_id", ""),
        "message_id": _field(row, "message_id", ""),
        "timestamp": _field(row, "timestamp", ""),
        "model": _field(row, "model", ""),
        "input_tokens": int(_field(row, "input_tokens", 0) or 0),
        "output_tokens": int(_field(row, "output_tokens", 0) or 0),
        "cache_read_tokens": int(_field(row, "cache_read_tokens", 0) or 0),
        "cache_creation_tokens": int(_field(row, "cache_creation_tokens", 0) or 0),
        "tool_name": _field(row, "tool_name", None),
        "project_name": project_basename(_field(row, "cwd", "")),
    }


def build_push_payload(rows, install_uuid, email, client_version=CLIENT_VERSION):
    """Build the metrics-only ingest payload from local turn rows (pure).

    Identity (`email`) is carried so the server can verify it matches the access
    key's canonical email; the server still treats the key as the source of
    truth. Runs the forbidden-field guard before returning, so a caller can
    never accidentally ship content.
    """
    payload = {
        "install_uuid": install_uuid,
        "email": email,
        "client_version": client_version,
        "turns": [extract_turn_metrics(r) for r in rows],
    }
    assert_no_forbidden_fields(payload)
    return payload


def assert_no_forbidden_fields(payload):
    """Fail loud if `payload` carries anything beyond the agreed metrics.

    Enforces the privacy contract: top-level keys must be within
    ALLOWED_PAYLOAD_FIELDS and every turn's keys within ALLOWED_TURN_FIELDS.
    Raises ForbiddenFieldError on any violation. This is the single guard both
    the client (before POST) and the server (on ingest) rely on.
    """
    if not isinstance(payload, dict):
        raise ForbiddenFieldError("payload must be a dict")

    extra_top = set(payload) - ALLOWED_PAYLOAD_FIELDS
    if extra_top:
        raise ForbiddenFieldError(
            "payload has forbidden top-level fields: " + ", ".join(sorted(extra_top))
        )

    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        raise ForbiddenFieldError("payload 'turns' must be a list")

    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise ForbiddenFieldError("turn %d is not a dict" % index)
        extra = set(turn) - ALLOWED_TURN_FIELDS
        if extra:
            raise ForbiddenFieldError(
                "turn %d has forbidden fields: %s" % (index, ", ".join(sorted(extra)))
            )
