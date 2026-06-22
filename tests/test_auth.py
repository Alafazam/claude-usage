"""Tests for auth.py - access-key lifecycle and request authentication."""

import os
import tempfile
import unittest
from pathlib import Path

import auth
from server_db import get_conn, init_server_db


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db_path = Path(self.tmpfile.name)
        self.conn = get_conn(self.db_path)
        init_server_db(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            if p.exists():
                p.unlink()


class TestKeyPrimitives(AuthTestCase):
    def test_generate_key_has_prefix(self):
        key = auth.generate_key()
        self.assertTrue(key.startswith("clu_"))

    def test_generate_key_is_unique(self):
        self.assertNotEqual(auth.generate_key(), auth.generate_key())

    def test_hash_is_stable_sha256(self):
        import hashlib
        raw = "clu_example"
        self.assertEqual(auth.hash_key(raw), hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(auth.hash_key(raw), auth.hash_key(raw))

    def test_hash_differs_per_key(self):
        self.assertNotEqual(auth.hash_key("clu_a"), auth.hash_key("clu_b"))


class TestCreateAndLookup(AuthTestCase):
    def test_create_then_lookup_roundtrip(self):
        raw = auth.create_key(self.conn, "Alice@Example.com")
        identity = auth.lookup_key(self.conn, raw)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "alice@example.com")  # lowercased
        self.assertFalse(identity["is_admin"])

    def test_admin_flag_carries(self):
        raw = auth.create_key(self.conn, "boss@x.com", is_admin=True)
        identity = auth.lookup_key(self.conn, raw)
        self.assertTrue(identity["is_admin"])

    def test_create_keys_multiple(self):
        result = auth.create_keys(self.conn, ["a@x.com", "b@x.com"])
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertTrue(item["key"].startswith("clu_"))
            self.assertIsNotNone(auth.lookup_key(self.conn, item["key"]))

    def test_lookup_unknown_returns_none(self):
        self.assertIsNone(auth.lookup_key(self.conn, "clu_nonexistent"))
        self.assertIsNone(auth.lookup_key(self.conn, None))
        self.assertIsNone(auth.lookup_key(self.conn, ""))

    def test_raw_key_never_stored(self):
        raw = auth.create_key(self.conn, "a@x.com")
        # The raw key must not appear anywhere in the access_keys table.
        rows = self.conn.execute("SELECT * FROM access_keys").fetchall()
        for r in rows:
            for value in tuple(r):
                self.assertNotEqual(value, raw)


class TestRevoke(AuthTestCase):
    def test_revoke_blocks_lookup(self):
        raw = auth.create_key(self.conn, "a@x.com")
        key_id = auth.lookup_key(self.conn, raw)["key_id"]
        auth.revoke_key(self.conn, key_id)
        self.assertIsNone(auth.lookup_key(self.conn, raw))

    def test_revoke_is_idempotent(self):
        raw = auth.create_key(self.conn, "a@x.com")
        key_id = self.conn.execute("SELECT key_id FROM access_keys").fetchone()[0]
        auth.revoke_key(self.conn, key_id)
        first = self.conn.execute(
            "SELECT revoked_at FROM access_keys WHERE key_id = ?", (key_id,)).fetchone()[0]
        auth.revoke_key(self.conn, key_id)
        second = self.conn.execute(
            "SELECT revoked_at FROM access_keys WHERE key_id = ?", (key_id,)).fetchone()[0]
        self.assertEqual(first, second)  # original revoke time preserved


class TestTouchAndList(AuthTestCase):
    def test_touch_last_seen(self):
        raw = auth.create_key(self.conn, "a@x.com")
        key_id = self.conn.execute("SELECT key_id FROM access_keys").fetchone()[0]
        self.assertIsNone(self.conn.execute(
            "SELECT last_seen_at FROM access_keys WHERE key_id = ?", (key_id,)).fetchone()[0])
        auth.touch_last_seen(self.conn, key_id)
        self.assertIsNotNone(self.conn.execute(
            "SELECT last_seen_at FROM access_keys WHERE key_id = ?", (key_id,)).fetchone()[0])

    def test_list_keys_omits_secret(self):
        auth.create_key(self.conn, "a@x.com", label="laptop")
        listed = auth.list_keys(self.conn)
        self.assertEqual(len(listed), 1)
        entry = listed[0]
        self.assertEqual(entry["email"], "a@x.com")
        self.assertEqual(entry["label"], "laptop")
        self.assertEqual(entry["status"], "active")
        self.assertNotIn("key_hash", entry)
        self.assertNotIn("key", entry)


class TestBootstrapAdmin(AuthTestCase):
    def test_bootstrap_key_idempotent(self):
        auth.bootstrap_admin(self.conn, admin_key="clu_admin_secret")
        auth.bootstrap_admin(self.conn, admin_key="clu_admin_secret")
        count = self.conn.execute("SELECT COUNT(*) FROM access_keys").fetchone()[0]
        self.assertEqual(count, 1)
        identity = auth.lookup_key(self.conn, "clu_admin_secret")
        self.assertTrue(identity["is_admin"])

    def test_bootstrap_admin_emails(self):
        auth.bootstrap_admin(self.conn, admin_emails=["boss@x.com", "lead@x.com"])
        for email in ("boss@x.com", "lead@x.com"):
            row = self.conn.execute(
                "SELECT is_admin FROM users WHERE email = ?", (email,)).fetchone()
            self.assertEqual(row["is_admin"], 1)


class TestResolveUiIdentity(AuthTestCase):
    def test_local_mode_with_admin_key(self):
        raw = auth.create_key(self.conn, "boss@x.com", is_admin=True)
        cfg = {"auth_mode": "local"}
        headers = {"Authorization": "Bearer " + raw}
        identity = auth.resolve_ui_identity(self.conn, headers, cfg)
        self.assertTrue(identity["is_admin"])

    def test_local_mode_without_key(self):
        self.assertIsNone(auth.resolve_ui_identity(self.conn, {}, {"auth_mode": "local"}))

    def test_proxy_mode_admin_email(self):
        auth.bootstrap_admin(self.conn, admin_emails=["boss@x.com"])
        cfg = {"auth_mode": "proxy", "sso_header": "X-Forwarded-Email"}
        identity = auth.resolve_ui_identity(
            self.conn, {"X-Forwarded-Email": "boss@x.com"}, cfg)
        self.assertTrue(identity["is_admin"])

    def test_proxy_mode_unknown_email_not_admin(self):
        cfg = {"auth_mode": "proxy", "sso_header": "X-Forwarded-Email"}
        identity = auth.resolve_ui_identity(
            self.conn, {"X-Forwarded-Email": "stranger@x.com"}, cfg)
        self.assertFalse(identity["is_admin"])
        self.assertEqual(identity["email"], "stranger@x.com")

    def test_proxy_mode_missing_header(self):
        cfg = {"auth_mode": "proxy", "sso_header": "X-Forwarded-Email"}
        self.assertIsNone(auth.resolve_ui_identity(self.conn, {}, cfg))


if __name__ == "__main__":
    unittest.main()
