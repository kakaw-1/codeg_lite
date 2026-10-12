#!/usr/bin/env python3
"""One-container supervisor for Codeg + cloudflared + ModelScope health UI."""

import html
import json
import os
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

RESTART_DELAY_SECONDS = 5


def is_codeg_reachable():
    """HTTP 401 is expected if Codeg authenticates the root endpoint."""
    try:
        with urlopen("http://127.0.0.1:3080/", timeout=1.5) as response:
            return response.status < 500
    except HTTPError as exc:
        return exc.code in (400, 401, 403, 404)
    except (URLError, OSError, TimeoutError):
        return False


class ProcessState:
    def __init__(self):
        self.processes = {}
        self.last_started = {}
        self.started_at = time.time()
        self.lock = threading.Lock()

    def running(self, name):
        with self.lock:
            process = self.processes.get(name)
            return process is not None and process.poll() is None

    def snapshot(self):
        return {
            "codeg": "reachable" if is_codeg_reachable() else "unreachable",
            "cloudflared_process": "running" if self.running("cloudflared") else "stopped",
            "uptime_seconds": int(time.time() - self.started_at),
        }


class HealthHandler(BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
    # 允许魔搭附加的路径和查询参数
    # 非 API 路径统一显示健康检查页面
    path = self.path.split("?", 1)[0].rstrip("/") or "/"
    if path not in ("/health", "/status", "/ready"):
        self.path = "/"
    else:
        self.path = path

        status = self.state.snapshot()
        ready = (
            status["codeg"] == "reachable"
            and status["cloudflared_process"] == "running"
        )
        if self.path in ("/health", "/status", "/ready"):
            body = json.dumps({"ready": ready, **status}, separators=(",", ":")).encode()
            # /health is liveness for the Studio/container; /ready tests dependencies.
            code = HTTPStatus.SERVICE_UNAVAILABLE if self.path == "/ready" and not ready else HTTPStatus.OK
            self.send_response(code)
            self.send_common_headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        c = html.escape(status["codeg"])
        t = html.escape(status["cloudflared_process"])
        body = ("<!doctype html><html lang='en'><head>"
                "<meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<meta http-equiv='refresh' content='15'>"
                "<title>Codeg Lite - ModelScope health</title>"
                "<style>body{font:16px system-ui;max-width:640px;margin:10vh auto;"
                "padding:0 20px;line-height:1.7;color:#222}"
                "code{background:#eee;padding:2px 5px;border-radius:4px}"
                ".state{padding:16px;border:1px solid #ddd;border-radius:12px}"
                "</style></head><body><h1>Codeg Lite</h1>"
                "<p>This ModelScope page is for health checks only. "
                "Open Codeg using your protected Cloudflare domain.</p>"
                "<div class='state'><p>Codeg local HTTP: <code>" + c + "</code></p>"
                "<p>Cloudflared process: <code>" + t + "</code></p>"
                "<p>Container uptime: <code>" + str(status["uptime_seconds"]) + "s</code></p></div>"
                "<p><small>Running process does not prove the tunnel is connected. "
                "Check cloudflared logs and Cloudflare Dashboard.</small></p>"
                "</body></html>").encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_common_headers("text/html; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self, ctype, length):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")

    def log_message(self, format_string, *args):
        return  # Do not log user addresses or untrusted paths on this public page.


def configure_environment():
    token = os.environ.get("CODEG_TOKEN", "").strip()
    tunnel_token = os.environ.get("TUNNEL_TOKEN", "").strip() or os.environ.get(
        "CF_TUNNEL_TOKEN", ""
    ).strip()
    if len(token) < 8:
        raise RuntimeError("Set CODEG_TOKEN as a ModelScope Secret (24+ random chars).")
    if not tunnel_token:
        raise RuntimeError("Set TUNNEL_TOKEN as a ModelScope Secret (Cloudflare Tunnel token).")
    if os.environ.get("CODEG_HOST") != "127.0.0.1" or os.environ.get("CODEG_PORT") != "3080":
        raise RuntimeError("CODEG_HOST=127.0.0.1 and CODEG_PORT=3080 are required.")

    paths = [
        os.environ.get("HOME", "/mnt/workspace/codeg/userhome"),
        os.environ.get("CODEG_HOME", "/mnt/workspace/codeg/codeg-home"),
        os.environ.get("CODEG_DATA_DIR", "/mnt/workspace/codeg/data"),
        "/mnt/workspace/codeg/projects",
    ]
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)
    os.chdir("/mnt/workspace/codeg/projects")

    codeg_env = dict(os.environ)
    codeg_env.pop("TUNNEL_TOKEN", None)
    codeg_env.pop("CF_TUNNEL_TOKEN", None)
    cf_env = {
        key: value for key, value in os.environ.items()
        if key in (
            "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "HTTP_PROXY",
            "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy",
            "https_proxy", "no_proxy", "all_proxy", "TUNNEL_TRANSPORT_PROTOCOL"
        )
    }
    cf_env["TUNNEL_TOKEN"] = tunnel_token  # Avoid token in argv / build output.
    return codeg_env, cf_env


def main():
    os.umask(0o077)
    codeg_env, cf_env = configure_environment()
    state = ProcessState()
    HealthHandler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("HEALTH_PORT", "7860"))), HealthHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("[codeg-lite] health page listening on 0.0.0.0:7860", flush=True)

    specs = {
        "codeg": (["codeg-server", "--supervise"], codeg_env),
        "cloudflared": (["cloudflared", "tunnel", "--no-autoupdate", "run"], cf_env),
    }
    stopping = threading.Event()

    def handle_stop(signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    try:
        while not stopping.is_set():
            for name, (command, env) in specs.items():
                with state.lock:
                    process = state.processes.get(name)
                    last_started = state.last_started.get(name, 0)
                if process is not None and process.poll() is None:
                    continue
                if process is not None:
                    print(f"[codeg-lite] {name} stopped (exit={process.returncode}), restarting", flush=True)
                if time.time() - last_started < RESTART_DELAY_SECONDS:
                    continue
                try:
                    started = subprocess.Popen(command, env=env, cwd=os.getcwd())
                except OSError as exc:
                    print(f"[codeg-lite] failed to start {name}: {exc}", flush=True)
                    with state.lock:
                        state.last_started[name] = time.time()
                    continue
                with state.lock:
                    state.processes[name] = started
                    state.last_started[name] = time.time()
                print(f"[codeg-lite] started {name} (pid={started.pid})", flush=True)
            stopping.wait(2.0)
    finally:
        with state.lock:
            processes = list(state.processes.items())
        for name, process in processes:
            if process.poll() is None:
                print(f"[codeg-lite] stopping {name}", flush=True)
                process.terminate()
        for name, process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"[codeg-lite] configuration error: {exc}", flush=True)
        raise SystemExit(2)
