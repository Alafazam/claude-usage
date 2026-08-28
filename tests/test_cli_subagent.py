"""Tests for the CLI subagent summary lines in `today` and `stats`."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import cli
from scanner import get_db, init_db, insert_turns, upsert_sessions


def _turn(message_id, inp, out, is_subagent, agent_id, ts):
    return {
        "session_id": "sess-1", "timestamp": ts, "model": "claude-opus-4-8",
        "input_tokens": inp, "output_tokens": out,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "tool_name": None, "cwd": "/home/user/proj",
        "message_id": message_id, "is_subagent": is_subagent, "agent_id": agent_id,
    }


class TestCliSubagentLines(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        today_ts = date.today().isoformat() + "T10:00:00Z"
        conn = get_db(self.db_path)
        init_db(conn)
        upsert_sessions(conn, [{
            "session_id": "sess-1", "project_name": "user/proj",
            "first_timestamp": today_ts, "last_timestamp": today_ts,
            "git_branch": "main", "model": "claude-opus-4-8",
            "total_input_tokens": 400, "total_output_tokens": 130,
            "total_cache_read": 0, "total_cache_creation": 0, "turn_count": 2,
        }])
        insert_turns(conn, [
            _turn("m-main", 100, 50, 0, None, today_ts),
            _turn("m-sub", 300, 80, 1, "agent-1", today_ts),
        ])
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def test_today_shows_subagent_tokens(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_today()
        out = buf.getvalue()
        self.assertIn("Subagent tokens:", out)
        # 300 + 80 = 380 subagent tokens, 1 turn
        self.assertIn("(1 turns)", out)

    def test_stats_shows_subagent_turns_and_tokens(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_stats()
        out = buf.getvalue()
        self.assertIn("Subagent turns:", out)
        self.assertIn("Subagent tokens:", out)


class TestCliReadCommandsMigrateOldSchema(unittest.TestCase):
    """A DB created before the subagent columns existed must not crash the read
    commands: require_db runs the idempotent init_db migration on open."""

    def setUp(self):
        import sqlite3
        self.db_path = Path(tempfile.mkdtemp()) / "usage.db"
        today_ts = date.today().isoformat() + "T10:00:00Z"
        # Pre-migration schema: turns has no is_subagent/agent_id, and there is
        # no `agents` table.
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                timestamp TEXT, model TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER, tool_name TEXT, cwd TEXT,
                message_id TEXT);
        """)
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, message_id) "
            "VALUES ('s1', ?, 'claude-opus-4-8', 100, 200, 0, 0, 'm1')",
            (today_ts,))
        conn.commit()
        conn.close()
        self._orig_db = cli.DB_PATH
        cli.DB_PATH = self.db_path

    def tearDown(self):
        cli.DB_PATH = self._orig_db

    def test_today_does_not_crash_on_old_schema(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_today()
        self.assertIn("Subagent tokens:", buf.getvalue())

    def test_week_does_not_crash_on_old_schema(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_week()
        self.assertTrue(buf.getvalue())

    def test_stats_does_not_crash_on_old_schema(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_stats()
        self.assertIn("Subagent turns:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
