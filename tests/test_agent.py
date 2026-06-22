"""Tests for agent.py - client config, payload build, and push behaviour."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import agent
from scanner import get_db, init_db


class _Resp:
    """Minimal stand-in for the urlopen context-manager response."""
    def __init__(self, body):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _FakeUrlopen:
    """Records each Request and returns/raises a scripted response."""
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body or {"received": 0, "inserted": 0, "deduped": 0}
        self.requests = []

    def __call__(self, req, *args, **kwargs):
        self.requests.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "error", {}, io.BytesIO(b"{}"))
        return _Resp(json.dumps(self.body).encode("utf-8"))


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        # Local scanner DB with a couple of turns.
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_file.close()
        self.db_path = Path(self.db_file.name)
        conn = get_db(self.db_path)
        init_db(conn)
        conn.executemany("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens,
                output_tokens, cache_read_tokens, cache_creation_tokens,
                tool_name, cwd, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ("s1", "2026-04-08T09:00:00Z", "claude-opus-4-7", 100, 50, 10, 5,
             "Bash", "/Users/dev/secret/proj", "m1"),
            ("s1", "2026-04-08T09:05:00Z", "claude-opus-4-7", 200, 80, 0, 0,
             "Read", "/Users/dev/secret/proj", "m2"),
        ])
        conn.commit()
        conn.close()

        # Isolated config file.
        self.cfg_file = tempfile.NamedTemporaryFile(suffix=".conf", delete=False)
        self.cfg_file.close()
        self.cfg_path = Path(self.cfg_file.name)
        self._write_cfg(server_url="http://server.example", key="clu_devkey",
                        email="dev@x.com")

    def tearDown(self):
        for p in (self.db_path, self.cfg_path):
            if p.exists():
                os.unlink(p)

    def _write_cfg(self, **kv):
        import configparser
        parser = configparser.ConfigParser()
        parser.add_section("team")
        for k, v in kv.items():
            parser.set("team", k, v)
        with open(self.cfg_path, "w") as f:
            parser.write(f)

    def _read_cfg(self):
        import configparser
        parser = configparser.ConfigParser()
        parser.read(self.cfg_path)
        return parser


class TestPushOnce(AgentTestCase):
    def test_posts_metrics_only_payload(self):
        fake = _FakeUrlopen(body={"received": 2, "inserted": 2, "deduped": 0})
        with mock.patch("urllib.request.urlopen", fake), \
             mock.patch.dict(os.environ, {}, clear=True):
            result = agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        self.assertEqual(result, {"received": 2, "inserted": 2, "deduped": 0})
        # Inspect the posted body: no cwd, basename project, declared email.
        self.assertEqual(len(fake.requests), 1)
        sent = json.loads(fake.requests[0].data.decode("utf-8"))
        self.assertEqual(sent["email"], "dev@x.com")
        self.assertEqual(len(sent["turns"]), 2)
        for turn in sent["turns"]:
            self.assertNotIn("cwd", turn)
            self.assertNotIn("git_branch", turn)
            self.assertEqual(turn["project_name"], "proj")
        # Authorization header carries the access key.
        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer clu_devkey")

    def test_watermark_advances_on_success(self):
        fake = _FakeUrlopen(body={"received": 2, "inserted": 2, "deduped": 0})
        with mock.patch("urllib.request.urlopen", fake), \
             mock.patch.dict(os.environ, {}, clear=True):
            agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        self.assertEqual(agent.get_watermark(self._read_cfg()), 2)

    def test_second_push_finds_nothing_new(self):
        fake = _FakeUrlopen(body={"received": 2, "inserted": 2, "deduped": 0})
        with mock.patch("urllib.request.urlopen", fake), \
             mock.patch.dict(os.environ, {}, clear=True):
            agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
            result = agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        self.assertEqual(result, {"received": 0, "inserted": 0, "deduped": 0})
        # No second HTTP call once the watermark caught up.
        self.assertEqual(len(fake.requests), 1)

    def test_watermark_holds_on_auth_failure(self):
        fake = _FakeUrlopen(status=401)
        with mock.patch("urllib.request.urlopen", fake), \
             mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(agent.PushError):
                agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        # Watermark must NOT advance on failure, so the next run retries.
        self.assertEqual(agent.get_watermark(self._read_cfg()), 0)

    def test_missing_config_raises(self):
        self._write_cfg(email="dev@x.com")  # no server_url/key
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(agent.PushError):
                agent.push_once(db_path=self.db_path, config_path=self.cfg_path)


class TestInstallUuid(AgentTestCase):
    def test_generated_once_and_reused(self):
        parser = agent._load_parser(self.cfg_path)
        first = agent.ensure_install_uuid(parser, self.cfg_path)
        # Re-read from disk: it was persisted.
        reread = agent._load_parser(self.cfg_path)
        second = agent.ensure_install_uuid(reread, self.cfg_path)
        self.assertEqual(first, second)
        self.assertTrue(first)


class TestEnvOverrides(AgentTestCase):
    def test_env_overrides_file(self):
        self._write_cfg(server_url="http://file", key="clu_file", email="file@x.com")
        env = {"CLAUDE_USAGE_SERVER_URL": "http://env",
               "CLU_KEY": "clu_env", "CLU_EMAIL": "env@x.com"}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = agent.resolve_settings(agent._load_parser(self.cfg_path))
        self.assertEqual(settings["server_url"], "http://env")
        self.assertEqual(settings["key"], "clu_env")
        self.assertEqual(settings["email"], "env@x.com")

    def test_file_used_when_env_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = agent.resolve_settings(agent._load_parser(self.cfg_path))
        self.assertEqual(settings["server_url"], "http://server.example")
        self.assertEqual(settings["key"], "clu_devkey")
        self.assertEqual(settings["email"], "dev@x.com")


class TestReadNewTurns(AgentTestCase):
    def test_only_id_bearing_turns(self):
        # Insert a turn with empty message_id; it must be skipped.
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO turns (session_id, model, input_tokens, message_id, cwd) "
                     "VALUES ('s2','claude-opus-4-7',1,'','/a/b/p')")
        conn.commit()
        conn.close()
        rows = agent._read_new_turns(self.db_path, 0)
        self.assertTrue(all(r["message_id"] for r in rows))
        self.assertEqual(len(rows), 2)


class TestAgentServerRoundtrip(AgentTestCase):
    """End-to-end: a real agent push against a real team server, no mocks."""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        import auth
        import server_db
        import team_server
        from team_server import TeamServerHandler

        self.srv_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.srv_db.close()
        self.srv_db_path = Path(self.srv_db.name)
        self._orig_db = team_server.DB_PATH
        team_server.DB_PATH = self.srv_db_path
        team_server.AUTH_MODE = "local"

        conn = server_db.get_conn(self.srv_db_path)
        server_db.init_server_db(conn)
        self.key = auth.create_key(conn, "dev@x.com")
        conn.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TeamServerHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        self._write_cfg(server_url="http://127.0.0.1:%d" % self.port,
                        key=self.key, email="dev@x.com")

    def tearDown(self):
        import team_server
        self.server.shutdown()
        self.server.server_close()
        team_server.DB_PATH = self._orig_db
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.srv_db_path) + suffix)
            if p.exists():
                p.unlink()
        super().tearDown()

    def test_roundtrip_stores_metrics_without_content(self):
        import server_db
        with mock.patch.dict(os.environ, {}, clear=True):
            result = agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        self.assertEqual(result["received"], 2)
        self.assertEqual(result["inserted"], 2)

        conn = server_db.get_conn(self.srv_db_path)
        rows = conn.execute("SELECT project_name, tool_name FROM server_turns "
                            "ORDER BY message_id").fetchall()
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(server_turns)")]
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["project_name"], "proj")  # basename, not full path
        self.assertNotIn("cwd", cols)       # server schema has no content columns
        self.assertNotIn("git_branch", cols)

    def test_roundtrip_idempotent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
            # Reset the watermark to force a re-push of the same turns.
            parser = agent._load_parser(self.cfg_path)
            agent.set_watermark(parser, self.cfg_path, 0)
            result = agent.push_once(db_path=self.db_path, config_path=self.cfg_path)
        self.assertEqual(result["received"], 2)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["deduped"], 2)


if __name__ == "__main__":
    unittest.main()
