import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from supervisor import (
    ProcessState,
    SnapshotState,
    configure_environment,
    restore_from_oss,
    snapshot_once,
    HealthHandler,
    SNAPSHOT_CONFIG,
)
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError
import threading
import json


class SupervisorTests(unittest.TestCase):
    @patch("supervisor.is_codeg_reachable", return_value=False)
    def test_status_does_not_claim_connected(self, mock_probe):
        state = ProcessState()
        snapshot = state.snapshot()
        self.assertEqual(snapshot["codeg"], "unreachable")
        self.assertEqual(snapshot["cloudflared_process"], "stopped")

    def test_requires_tokens(self):
        with patch.dict(os.environ, {"CODEG_TOKEN": "", "TUNNEL_TOKEN": ""}, clear=True):
            with self.assertRaises(RuntimeError):
                configure_environment()

    def test_does_not_expose_tunnel_token_to_codeg(self):
        with patch.dict(os.environ, {
            "CODEG_TOKEN": "C" * 40,
            "TUNNEL_TOKEN": "test-cloudflare-secret",
            "CODEG_HOST": "127.0.0.1",
            "CODEG_PORT": "3080",
            "HOME": "/tmp/codeg-lite-unit-test-home",
            "CODEG_HOME": "/tmp/codeg-lite-unit-test-config",
            "CODEG_DATA_DIR": "/tmp/codeg-lite-unit-test-data",
        }, clear=True):
            with patch("supervisor.Path.mkdir"), patch("supervisor.os.chdir"):
                codeg_env, cf_env = configure_environment()
        self.assertNotIn("TUNNEL_TOKEN", codeg_env)
        self.assertNotIn("CODEG_TOKEN", cf_env)
        self.assertEqual(cf_env["TUNNEL_TOKEN"], "test-cloudflare-secret")


    @patch("supervisor.is_codeg_reachable", return_value=False)
    def test_health_status_and_public_page(self, mock_probe):
        state = ProcessState()
        HealthHandler.state = state
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/") as response:
                html_body = response.read().decode()
                self.assertIn("health checks only", html_body)
                self.assertNotIn("test-cloudflare-secret", html_body)
            with urlopen(base + "/health") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode())
                self.assertFalse(payload["ready"])
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/ready")
            self.assertEqual(error.exception.code, 503)
        finally:
            server.shutdown()
            server.server_close()


class SnapshotTests(unittest.TestCase):
    def _make_db(self, path, rows=(1, 2, 3)):
        con = sqlite3.connect(path)
        try:
            con.execute("PRAGMA journal_mode=WAL;")
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            con.executemany("INSERT INTO t (id) VALUES (?)", [(r,) for r in rows])
            con.commit()
        finally:
            con.close()

    def _read_ids(self, path):
        con = sqlite3.connect(path)
        try:
            return sorted(r[0] for r in con.execute("SELECT id FROM t"))
        finally:
            con.close()

    def test_restore_fills_missing_and_skips_transients(self):
        with tempfile.TemporaryDirectory() as oss, tempfile.TemporaryDirectory() as local:
            self._make_db(os.path.join(oss, "codeg.db"))
            chat_dir = os.path.join(oss, "chat-sessions", "s1")
            os.makedirs(chat_dir)
            with open(os.path.join(chat_dir, "note.txt"), "w") as handle:
                handle.write("hello")
            # Stale sidecars must never be restored.
            with open(os.path.join(oss, "codeg.db-wal"), "w") as handle:
                handle.write("junk")
            with open(os.path.join(oss, "codeg.db.lock"), "w") as handle:
                handle.write("junk")
            restored = restore_from_oss(oss, local)
            self.assertEqual(restored, 2)
            self.assertEqual(self._read_ids(os.path.join(local, "codeg.db")), [1, 2, 3])
            with open(os.path.join(local, "chat-sessions", "s1", "note.txt")) as handle:
                self.assertEqual(handle.read(), "hello")
            self.assertFalse(os.path.exists(os.path.join(local, "codeg.db-wal")))
            self.assertFalse(os.path.exists(os.path.join(local, "codeg.db.lock")))
            # A surviving local master is never overwritten by restore.
            con = sqlite3.connect(os.path.join(local, "codeg.db"))
            con.execute("INSERT INTO t (id) VALUES (99)")
            con.commit()
            con.close()
            self.assertEqual(restore_from_oss(oss, local), 0)
            self.assertIn(99, self._read_ids(os.path.join(local, "codeg.db")))

    def test_snapshot_roundtrip_with_live_writer(self):
        with tempfile.TemporaryDirectory() as oss, tempfile.TemporaryDirectory() as local:
            live = os.path.join(local, "codeg.db")
            writer = sqlite3.connect(live, timeout=10.0)
            try:
                writer.execute("PRAGMA journal_mode=WAL;")
                writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
                writer.executemany("INSERT INTO t (id) VALUES (?)", [(1,), (2,)])
                writer.commit()  # rows may still sit in -wal; writer stays open
                chat_dir = os.path.join(local, "chat-sessions", "s9")
                os.makedirs(chat_dir)
                with open(os.path.join(chat_dir, "a.txt"), "w") as handle:
                    handle.write("data")
                snap = SnapshotState()
                self.assertTrue(snapshot_once(local, oss, snap))
                self.assertIsNotNone(snap.last_ok)
                self.assertIsNone(snap.last_error)
            finally:
                writer.close()
            mirror = os.path.join(oss, "codeg.db")
            self.assertEqual(self._read_ids(mirror), [1, 2])
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(os.path.exists(mirror + suffix))
            with open(os.path.join(oss, "chat-sessions", "s9", "a.txt")) as handle:
                self.assertEqual(handle.read(), "data")
            with open(os.path.join(oss, ".snapshot.json")) as handle:
                heartbeat = json.load(handle)
            self.assertIn("last_ok", heartbeat)
            # Unchanged state is skipped once sigs converge (the backup's own
            # checkpoint touches the source mtime once, so allow one extra
            # round; the loop reuses a single SnapshotState, same as prod).
            self.assertTrue(snapshot_once(local, oss, snap))
            before = os.stat(mirror).st_mtime_ns
            self.assertTrue(snapshot_once(local, oss, snap))
            self.assertEqual(os.stat(mirror).st_mtime_ns, before)

    def test_snapshot_never_mirrors_transients(self):
        with tempfile.TemporaryDirectory() as oss, tempfile.TemporaryDirectory() as local:
            self._make_db(os.path.join(local, "codeg.db"))
            for name in ("codeg.db-wal", "codeg.db-shm", "codeg.db.lock"):
                with open(os.path.join(local, name), "w") as handle:
                    handle.write("junk")
            self.assertTrue(snapshot_once(local, oss, SnapshotState()))
            for name in ("codeg.db-wal", "codeg.db-shm", "codeg.db.lock"):
                self.assertFalse(os.path.exists(os.path.join(oss, name)))

    def test_configure_defaults_to_local_fast_disk_and_1s(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            with patch.dict(os.environ, {
                "CODEG_TOKEN": "C" * 40,
                "TUNNEL_TOKEN": "test-cloudflare-secret",
                "CODEG_HOST": "127.0.0.1",
                "CODEG_PORT": "3080",
                "HOME": home,
                "CODEG_HOME": os.path.join(tmp, "codeg-home"),
                "CODEG_DATA_DIR": os.path.join(tmp, "oss-data"),
            }, clear=True):
                with patch("supervisor.Path.mkdir"), patch("supervisor.os.chdir"):
                    codeg_env, _cf_env = configure_environment()
            self.assertEqual(codeg_env["CODEG_DATA_DIR"], "/eas/workspace/codeg-data")
            self.assertEqual(
                codeg_env["CODEG_OSS_DATA_DIR"], os.path.join(tmp, "oss-data")
            )
            self.assertEqual(codeg_env["CODEG_SNAPSHOT_INTERVAL_S"], "1.0")
            self.assertEqual(SNAPSHOT_CONFIG["interval"], 1.0)


if __name__ == "__main__":
    unittest.main()
