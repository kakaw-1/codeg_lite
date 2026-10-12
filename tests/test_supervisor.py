import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from supervisor import ProcessState, configure_environment, HealthHandler
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


if __name__ == "__main__":
    unittest.main()
