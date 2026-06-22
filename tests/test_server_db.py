"""Tests for server_db.py - server schema, dedup, and manager queries."""

import os
import tempfile
import unittest
from pathlib import Path

from server_db import (
    get_conn,
    get_enrollment,
    get_team_data,
    get_user_detail,
    init_server_db,
    mcp_server_from_tool,
    upsert_turn_metrics,
)


def _turn(message_id, model="claude-opus-4-7", tool_name=None, project="proj",
          session="sess-1", timestamp="2026-04-08T09:30:00Z",
          inp=100, out=50, cr=10, cc=5):
    return {
        "session_id": session,
        "message_id": message_id,
        "timestamp": timestamp,
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cr,
        "cache_creation_tokens": cc,
        "tool_name": tool_name,
        "project_name": project,
    }


class ServerDBTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_conn(self.db_path)
        init_server_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        # WAL sidecar files
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()

    def _add_user(self, email, is_admin=0):
        cur = self.conn.execute(
            "INSERT INTO users (email, is_admin, created_at) VALUES (?, ?, ?)",
            (email, is_admin, "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        return cur.lastrowid

    def _add_key(self, user_id, key_hash, last_seen_at=None, revoked_at=None):
        self.conn.execute(
            "INSERT INTO access_keys (key_hash, user_id, created_at, last_seen_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key_hash, user_id, "2026-01-01T00:00:00Z", last_seen_at, revoked_at),
        )
        self.conn.commit()


class TestSchema(ServerDBTestCase):
    def test_tables_created(self):
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("users", names)
        self.assertIn("access_keys", names)
        self.assertIn("server_turns", names)

    def test_composite_unique_index_exists(self):
        indexes = self.conn.execute("PRAGMA index_list(server_turns)").fetchall()
        names = [r["name"] for r in indexes]
        self.assertIn("idx_server_turns_user_msg", names)
        # Confirm it spans (user_id, message_id), not message_id alone.
        cols = [r["name"] for r in self.conn.execute(
            "PRAGMA index_info(idx_server_turns_user_msg)")]
        self.assertEqual(cols, ["user_id", "message_id"])

    def test_wal_enabled(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_idempotent_init(self):
        # Running init again must not raise or duplicate.
        init_server_db(self.conn)


class TestUpsertDedup(ServerDBTestCase):
    def test_basic_insert(self):
        uid = self._add_user("a@x.com")
        result = upsert_turn_metrics(self.conn, uid, "uuid-1",
                                     [_turn("m1"), _turn("m2")])
        self.assertEqual(result, {"received": 2, "inserted": 2, "deduped": 0})

    def test_repush_same_batch_dedupes(self):
        uid = self._add_user("a@x.com")
        batch = [_turn("m1"), _turn("m2"), _turn("m3")]
        upsert_turn_metrics(self.conn, uid, "uuid-1", batch)
        result = upsert_turn_metrics(self.conn, uid, "uuid-1", batch)
        self.assertEqual(result["received"], 3)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["deduped"], 3)
        # Total row count stayed at 3.
        count = self.conn.execute("SELECT COUNT(*) FROM server_turns").fetchone()[0]
        self.assertEqual(count, 3)

    def test_same_message_id_different_users_both_insert(self):
        """The key correctness property the single-column index would fail."""
        a = self._add_user("a@x.com")
        b = self._add_user("b@x.com")
        upsert_turn_metrics(self.conn, a, "uuid-a", [_turn("shared-msg")])
        result = upsert_turn_metrics(self.conn, b, "uuid-b", [_turn("shared-msg")])
        self.assertEqual(result["inserted"], 1)
        count = self.conn.execute("SELECT COUNT(*) FROM server_turns").fetchone()[0]
        self.assertEqual(count, 2)

    def test_partial_overlap(self):
        uid = self._add_user("a@x.com")
        upsert_turn_metrics(self.conn, uid, "u", [_turn("m1"), _turn("m2")])
        result = upsert_turn_metrics(self.conn, uid, "u",
                                     [_turn("m2"), _turn("m3")])
        self.assertEqual(result["received"], 2)
        self.assertEqual(result["inserted"], 1)  # only m3 is new
        self.assertEqual(result["deduped"], 1)


class TestMcpParsing(unittest.TestCase):
    def test_extracts_server(self):
        self.assertEqual(mcp_server_from_tool("mcp__github__create_issue"), "github")
        self.assertEqual(mcp_server_from_tool("mcp__slack__send_message"), "slack")

    def test_non_mcp_tool(self):
        self.assertIsNone(mcp_server_from_tool("Bash"))
        self.assertIsNone(mcp_server_from_tool(""))
        self.assertIsNone(mcp_server_from_tool(None))

    def test_malformed_mcp_name(self):
        self.assertIsNone(mcp_server_from_tool("mcp__"))


class TestQueries(ServerDBTestCase):
    def test_model_mix_collapses_empty_and_null(self):
        uid = self._add_user("a@x.com")
        upsert_turn_metrics(self.conn, uid, "u", [
            _turn("m1", model=""),
            _turn("m2", model=None),
            _turn("m3", model="claude-opus-4-7"),
        ])
        data = get_team_data(self.conn)
        unknowns = [d for d in data["daily_by_model"] if d["model"] == "unknown"]
        # Both '' and NULL collapse into a single 'unknown' bucket per day.
        self.assertEqual(len(unknowns), 1)
        self.assertEqual(unknowns[0]["turns"], 2)

    def test_tool_freq_and_mcp(self):
        uid = self._add_user("a@x.com")
        upsert_turn_metrics(self.conn, uid, "u", [
            _turn("m1", tool_name="Bash"),
            _turn("m2", tool_name="Bash"),
            _turn("m3", tool_name="mcp__github__create_issue"),
            _turn("m4", tool_name="mcp__github__list_prs"),
        ])
        data = get_team_data(self.conn)
        tools = {t["tool_name"]: t["turns"] for t in data["tool_freq"]}
        self.assertEqual(tools["Bash"], 2)
        mcp = {m["server"]: m for m in data["mcp_servers"]}
        self.assertIn("github", mcp)
        self.assertEqual(mcp["github"]["turns"], 2)
        self.assertEqual(mcp["github"]["users"], 1)

    def test_leaderboard_includes_zero_usage_user(self):
        self._add_user("active@x.com")
        idle = self._add_user("idle@x.com")
        active = self.conn.execute(
            "SELECT user_id FROM users WHERE email='active@x.com'").fetchone()[0]
        upsert_turn_metrics(self.conn, active, "u", [_turn("m1")])
        data = get_team_data(self.conn)
        emails = {u["email"]: u for u in data["users"]}
        self.assertIn("active@x.com", emails)
        self.assertIn("idle@x.com", emails)
        self.assertEqual(emails["idle@x.com"]["turns"], 0)

    def test_projects_breakdown(self):
        uid = self._add_user("a@x.com")
        upsert_turn_metrics(self.conn, uid, "u", [
            _turn("m1", project="alpha"),
            _turn("m2", project="alpha"),
            _turn("m3", project="beta"),
        ])
        data = get_team_data(self.conn)
        projects = {p["project_name"]: p for p in data["projects"]}
        self.assertEqual(projects["alpha"]["turns"], 2)
        self.assertEqual(projects["beta"]["turns"], 1)

    def test_enrollment_and_last_seen(self):
        uid = self._add_user("a@x.com")
        self._add_key(uid, "hash-a", last_seen_at="2026-04-08T10:00:00Z")
        # A revoked key for the same user should not change active enrollment.
        self._add_key(uid, "hash-a-old", revoked_at="2026-04-01T00:00:00Z")
        enrollment = get_enrollment(self.conn)
        self.assertEqual(len(enrollment), 1)
        self.assertEqual(enrollment[0]["email"], "a@x.com")
        self.assertEqual(enrollment[0]["last_seen_at"], "2026-04-08T10:00:00Z")
        self.assertEqual(enrollment[0]["active_keys"], 1)

    def test_enrollment_excludes_users_without_active_key(self):
        uid = self._add_user("revoked-only@x.com")
        self._add_key(uid, "h", revoked_at="2026-04-01T00:00:00Z")
        self.assertEqual(get_enrollment(self.conn), [])


class TestUserDetail(ServerDBTestCase):
    def test_drilldown(self):
        a = self._add_user("a@x.com")
        b = self._add_user("b@x.com")
        upsert_turn_metrics(self.conn, a, "u", [_turn("m1", tool_name="Bash")])
        upsert_turn_metrics(self.conn, b, "u", [_turn("m2", tool_name="Read")])
        detail = get_user_detail(self.conn, a)
        self.assertEqual(detail["email"], "a@x.com")
        self.assertEqual(detail["totals"]["turns"], 1)
        tools = [t["tool_name"] for t in detail["tool_freq"]]
        self.assertEqual(tools, ["Bash"])  # only this user's tools

    def test_unknown_user_returns_none(self):
        self.assertIsNone(get_user_detail(self.conn, 99999))


if __name__ == "__main__":
    unittest.main()
