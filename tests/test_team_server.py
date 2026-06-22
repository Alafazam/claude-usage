"""Integration tests for team_server.py - a real server on an ephemeral port."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import auth
import server_db
import team_server
from team_server import TeamServerHandler


def _payload(email, message_ids, **turn_overrides):
    turns = []
    for mid in message_ids:
        turn = {
            "session_id": "sess-1",
            "message_id": mid,
            "timestamp": "2026-04-08T09:30:00Z",
            "model": "claude-opus-4-7",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 5,
            "tool_name": "Bash",
            "project_name": "proj",
        }
        turn.update(turn_overrides)
        turns.append(turn)
    return {"install_uuid": "uuid-1", "email": email,
            "client_version": "team-1", "turns": turns}


class TeamServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.tmpfile.close()
        cls.db_path = Path(cls.tmpfile.name)

        cls._orig_db = team_server.DB_PATH
        cls._orig_mode = team_server.AUTH_MODE
        team_server.DB_PATH = cls.db_path
        team_server.AUTH_MODE = "local"

        conn = server_db.get_conn(cls.db_path)
        server_db.init_server_db(conn)
        cls.admin_key = auth.create_key(conn, "admin@x.com", is_admin=True)
        cls.dev_key = auth.create_key(conn, "dev@x.com")
        cls.revoked_key = auth.create_key(conn, "old@x.com")
        revoked_id = auth.lookup_key(conn, cls.revoked_key)["key_id"]
        auth.revoke_key(conn, revoked_id)
        conn.close()

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TeamServerHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        team_server.DB_PATH = cls._orig_db
        team_server.AUTH_MODE = cls._orig_mode
        os.unlink(cls.db_path)
        for suffix in ("-wal", "-shm"):
            p = Path(str(cls.db_path) + suffix)
            if p.exists():
                p.unlink()

    def _request(self, method, path, body=None, headers=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req)
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def _bearer(self, key):
        return {"Authorization": "Bearer " + key}


class TestIngestAuth(TeamServerTestCase):
    def test_no_bearer_rejected(self):
        status, _ = self._request("POST", "/api/ingest", _payload("dev@x.com", ["m1"]))
        self.assertEqual(status, 401)

    def test_revoked_key_rejected(self):
        status, _ = self._request("POST", "/api/ingest",
                                  _payload("old@x.com", ["m1"]),
                                  self._bearer(self.revoked_key))
        self.assertEqual(status, 401)

    def test_email_mismatch_rejected(self):
        # dev's key, but claiming to be someone else.
        status, _ = self._request("POST", "/api/ingest",
                                  _payload("someone-else@x.com", ["m1"]),
                                  self._bearer(self.dev_key))
        self.assertEqual(status, 401)

    def test_missing_email_rejected(self):
        body = _payload("dev@x.com", ["m1"])
        del body["email"]
        status, _ = self._request("POST", "/api/ingest", body, self._bearer(self.dev_key))
        self.assertEqual(status, 400)


class TestIngest(TeamServerTestCase):
    def test_valid_ingest(self):
        status, resp = self._request("POST", "/api/ingest",
                                     _payload("dev@x.com", ["a1", "a2"]),
                                     self._bearer(self.dev_key))
        self.assertEqual(status, 200)
        self.assertEqual(resp, {"received": 2, "inserted": 2, "deduped": 0})

    def test_idempotent_repush(self):
        batch = _payload("dev@x.com", ["b1", "b2", "b3"])
        self._request("POST", "/api/ingest", batch, self._bearer(self.dev_key))
        status, resp = self._request("POST", "/api/ingest", batch, self._bearer(self.dev_key))
        self.assertEqual(status, 200)
        self.assertEqual(resp["received"], 3)
        self.assertEqual(resp["inserted"], 0)
        self.assertEqual(resp["deduped"], 3)

    def test_forbidden_field_rejected(self):
        body = _payload("dev@x.com", ["c1"], cwd="/Users/dev/secret/proj")
        status, _ = self._request("POST", "/api/ingest", body, self._bearer(self.dev_key))
        self.assertEqual(status, 400)

    def test_last_seen_updated(self):
        self._request("POST", "/api/ingest",
                      _payload("dev@x.com", ["seen1"]), self._bearer(self.dev_key))
        conn = server_db.get_conn(self.db_path)
        seen = conn.execute(
            "SELECT last_seen_at FROM access_keys k JOIN users u ON u.user_id=k.user_id "
            "WHERE u.email='dev@x.com'").fetchone()[0]
        conn.close()
        self.assertIsNotNone(seen)

    def test_data_stored_under_key_user(self):
        # Identity is the key's user; the row must belong to dev@x.com.
        self._request("POST", "/api/ingest",
                      _payload("dev@x.com", ["owned1"]), self._bearer(self.dev_key))
        conn = server_db.get_conn(self.db_path)
        row = conn.execute(
            "SELECT u.email FROM server_turns t JOIN users u ON u.user_id=t.user_id "
            "WHERE t.message_id='owned1'").fetchone()
        conn.close()
        self.assertEqual(row["email"], "dev@x.com")


class TestAdminEndpoints(TeamServerTestCase):
    def test_team_data_requires_auth(self):
        status, _ = self._request("GET", "/api/team/data")
        self.assertEqual(status, 401)

    def test_team_data_rejects_non_admin(self):
        status, _ = self._request("GET", "/api/team/data", headers=self._bearer(self.dev_key))
        self.assertEqual(status, 403)

    def test_team_data_admin_ok(self):
        status, resp = self._request("GET", "/api/team/data",
                                     headers=self._bearer(self.admin_key))
        self.assertEqual(status, 200)
        self.assertIn("totals", resp)
        self.assertIn("users", resp)
        self.assertIn("by_model", resp)

    def test_create_and_list_keys(self):
        status, resp = self._request("POST", "/api/admin/keys",
                                     {"emails": ["new@x.com"], "label": "laptop"},
                                     self._bearer(self.admin_key))
        self.assertEqual(status, 200)
        self.assertEqual(len(resp["created"]), 1)
        self.assertTrue(resp["created"][0]["key"].startswith("clu_"))

        status, listed = self._request("GET", "/api/admin/keys",
                                       headers=self._bearer(self.admin_key))
        self.assertEqual(status, 200)
        emails = {k["email"] for k in listed["keys"]}
        self.assertIn("new@x.com", emails)
        # No secret material in the listing.
        for k in listed["keys"]:
            self.assertNotIn("key_hash", k)
            self.assertNotIn("key", k)

    def test_revoke_key(self):
        _, resp = self._request("POST", "/api/admin/keys",
                                {"emails": ["revoke-me@x.com"]},
                                self._bearer(self.admin_key))
        raw = resp["created"][0]["key"]
        conn = server_db.get_conn(self.db_path)
        key_id = auth.lookup_key(conn, raw)["key_id"]
        conn.close()
        status, _ = self._request("POST", "/api/admin/keys/%d/revoke" % key_id,
                                  {}, self._bearer(self.admin_key))
        self.assertEqual(status, 200)
        conn = server_db.get_conn(self.db_path)
        self.assertIsNone(auth.lookup_key(conn, raw))
        conn.close()

    def test_user_detail(self):
        # dev has ingested in other tests; resolve their id and drill down.
        conn = server_db.get_conn(self.db_path)
        uid = conn.execute("SELECT user_id FROM users WHERE email='dev@x.com'").fetchone()[0]
        conn.close()
        status, resp = self._request("GET", "/api/team/user/%d" % uid,
                                     headers=self._bearer(self.admin_key))
        self.assertEqual(status, 200)
        self.assertEqual(resp["email"], "dev@x.com")
        self.assertIn("by_model", resp)

    def test_unknown_user_404(self):
        status, _ = self._request("GET", "/api/team/user/99999",
                                  headers=self._bearer(self.admin_key))
        self.assertEqual(status, 404)


class TestPages(TeamServerTestCase):
    def test_dashboard_page_served(self):
        status, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Team usage", body)

    def test_keys_page_served(self):
        status, body = self._request("GET", "/admin/keys")
        self.assertEqual(status, 200)
        self.assertIn("Access keys", body)

    def test_unknown_route_404(self):
        status, _ = self._request("GET", "/nope")
        self.assertEqual(status, 404)


class TestProxyMode(TeamServerTestCase):
    def test_proxy_header_admin(self):
        # Seed an admin email and flip the server into proxy mode.
        conn = server_db.get_conn(self.db_path)
        auth.bootstrap_admin(conn, admin_emails=["proxyboss@x.com"])
        conn.close()
        team_server.AUTH_MODE = "proxy"
        try:
            status, resp = self._request(
                "GET", "/api/team/data",
                headers={"X-Forwarded-Email": "proxyboss@x.com"})
            self.assertEqual(status, 200)
            self.assertIn("totals", resp)
            # A non-admin SSO email is forbidden.
            status2, _ = self._request(
                "GET", "/api/team/data",
                headers={"X-Forwarded-Email": "stranger@x.com"})
            self.assertEqual(status2, 403)
        finally:
            team_server.AUTH_MODE = "local"


if __name__ == "__main__":
    unittest.main()
