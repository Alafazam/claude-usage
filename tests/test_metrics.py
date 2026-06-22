"""Tests for metrics.py - pure metric extraction and the privacy guard."""

import sqlite3
import unittest

from metrics import (
    ALLOWED_TURN_FIELDS,
    CLIENT_VERSION,
    ForbiddenFieldError,
    assert_no_forbidden_fields,
    build_push_payload,
    extract_turn_metrics,
    project_basename,
)


def _local_turn_row(**overrides):
    """A representative row as read from the local `turns` table (includes cwd)."""
    row = {
        "id": 7,
        "session_id": "sess-1",
        "message_id": "msg-1",
        "timestamp": "2026-04-08T09:30:00.000Z",
        "model": "claude-opus-4-7",
        "input_tokens": 500,
        "output_tokens": 200,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 20,
        "tool_name": "Bash",
        "cwd": "/Users/alice/repos/claude-usage",
    }
    row.update(overrides)
    return row


class TestProjectBasename(unittest.TestCase):
    def test_takes_last_component_only(self):
        self.assertEqual(project_basename("/Users/alice/repos/claude-usage"), "claude-usage")

    def test_windows_path(self):
        self.assertEqual(project_basename(r"C:\Users\alice\repos\myproj"), "myproj")

    def test_trailing_slash(self):
        self.assertEqual(project_basename("/home/bob/work/"), "work")

    def test_empty_and_none(self):
        self.assertEqual(project_basename(""), "unknown")
        self.assertEqual(project_basename(None), "unknown")

    def test_does_not_leak_parent_dirs(self):
        # The defining privacy property: only the final segment survives.
        result = project_basename("/Users/alice/secret-client-name/proj")
        self.assertEqual(result, "proj")
        self.assertNotIn("secret-client-name", result)
        self.assertNotIn("alice", result)


class TestExtractTurnMetrics(unittest.TestCase):
    def test_whitelists_keys(self):
        out = extract_turn_metrics(_local_turn_row())
        self.assertTrue(set(out).issubset(ALLOWED_TURN_FIELDS))

    def test_drops_cwd_keeps_basename(self):
        out = extract_turn_metrics(_local_turn_row())
        self.assertNotIn("cwd", out)
        self.assertEqual(out["project_name"], "claude-usage")

    def test_carries_metric_values(self):
        out = extract_turn_metrics(_local_turn_row())
        self.assertEqual(out["model"], "claude-opus-4-7")
        self.assertEqual(out["input_tokens"], 500)
        self.assertEqual(out["output_tokens"], 200)
        self.assertEqual(out["cache_read_tokens"], 50)
        self.assertEqual(out["cache_creation_tokens"], 20)
        self.assertEqual(out["tool_name"], "Bash")
        self.assertEqual(out["message_id"], "msg-1")

    def test_handles_sqlite_row(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (session_id, message_id, timestamp, model, "
                     "input_tokens, output_tokens, cache_read_tokens, "
                     "cache_creation_tokens, tool_name, cwd)")
        conn.execute("INSERT INTO t VALUES ('s','m','2026-01-01T00:00:00Z','claude-haiku-4-5',"
                     "1,2,3,4,'Read','/a/b/proj')")
        row = conn.execute("SELECT * FROM t").fetchone()
        out = extract_turn_metrics(row)
        self.assertEqual(out["project_name"], "proj")
        self.assertEqual(out["tool_name"], "Read")
        self.assertNotIn("cwd", out)
        conn.close()

    def test_null_tokens_default_to_zero(self):
        out = extract_turn_metrics(_local_turn_row(input_tokens=None, output_tokens=None))
        self.assertEqual(out["input_tokens"], 0)
        self.assertEqual(out["output_tokens"], 0)


class TestBuildPushPayload(unittest.TestCase):
    def test_payload_shape(self):
        payload = build_push_payload([_local_turn_row()], "uuid-123", "alice@x.com")
        self.assertEqual(payload["install_uuid"], "uuid-123")
        self.assertEqual(payload["email"], "alice@x.com")
        self.assertEqual(payload["client_version"], CLIENT_VERSION)
        self.assertEqual(len(payload["turns"]), 1)

    def test_payload_carries_no_cwd_or_branch(self):
        payload = build_push_payload(
            [_local_turn_row(git_branch="main", cwd="/Users/alice/secret/proj")],
            "uuid-123", "alice@x.com",
        )
        # The defining privacy assertion for the whole feature.
        for turn in payload["turns"]:
            self.assertNotIn("cwd", turn)
            self.assertNotIn("git_branch", turn)
            self.assertNotIn("gitBranch", turn)
            self.assertEqual(turn["project_name"], "proj")

    def test_empty_rows(self):
        payload = build_push_payload([], "uuid-123", "alice@x.com")
        self.assertEqual(payload["turns"], [])


class TestAssertNoForbiddenFields(unittest.TestCase):
    def test_clean_payload_passes(self):
        payload = build_push_payload([_local_turn_row()], "u", "a@x.com")
        # Should not raise.
        assert_no_forbidden_fields(payload)

    def test_rejects_cwd_in_turn(self):
        payload = {"install_uuid": "u", "email": "a@x.com", "client_version": "team-1",
                   "turns": [{"session_id": "s", "cwd": "/Users/alice/proj"}]}
        with self.assertRaises(ForbiddenFieldError):
            assert_no_forbidden_fields(payload)

    def test_rejects_git_branch_in_turn(self):
        payload = {"install_uuid": "u", "email": "a@x.com", "client_version": "team-1",
                   "turns": [{"session_id": "s", "git_branch": "main"}]}
        with self.assertRaises(ForbiddenFieldError):
            assert_no_forbidden_fields(payload)

    def test_rejects_content_fields(self):
        for bad in ("prompt", "code", "thinking", "content", "text"):
            payload = {"install_uuid": "u", "email": "a@x.com", "client_version": "team-1",
                       "turns": [{"session_id": "s", bad: "leaked content"}]}
            with self.assertRaises(ForbiddenFieldError):
                assert_no_forbidden_fields(payload)

    def test_rejects_unexpected_top_level_field(self):
        payload = {"install_uuid": "u", "email": "a@x.com", "client_version": "team-1",
                   "turns": [], "raw_transcript": "leak"}
        with self.assertRaises(ForbiddenFieldError):
            assert_no_forbidden_fields(payload)

    def test_rejects_non_dict_payload(self):
        with self.assertRaises(ForbiddenFieldError):
            assert_no_forbidden_fields(["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
