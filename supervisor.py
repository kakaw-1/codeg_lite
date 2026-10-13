#!/usr/bin/env python3
"""One-container supervisor for Codeg + cloudflared + ModelScope health UI.

Storage layout (single container, no external DB):

- ``codeg-server`` reads/writes ``CODEG_DATA_DIR`` on a LOCAL fast disk
  (default ``/eas/workspace/codeg-data``, ext4, ~1ms writes). This fixes
  10+ concurrent agents stalling on ossfs (``/mnt/workspace``, ~60ms
  writes, WAL checkpoint starvation, 5s busy-timeout acquires).
- The OSS path (default ``/mnt/workspace/codeg/data``) is only a snapshot
  mirror: restored into the local dir at startup, re-synced every
  ``CODEG_SNAPSHOT_INTERVAL_S`` seconds (default 1s), and flushed once more
  on SIGTERM/SIGINT. A crash therefore loses at most one interval; a
  graceful restart loses nothing.
- Live SQLite files are NEVER plain-copied (a torn copy beside a WAL
  sidecar would be corrupt). Snapshots use the sqlite3 backup API into a
  local staging file, then an atomic rename into the OSS mirror.
"""

import html
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

RESTART_DELAY_SECONDS = 5

# --- Local-primary + OSS snapshot settings (all env-overridable) ---
LOCAL_DATA_DIR_DEFAULT = "/eas/workspace/codeg-data"
OSS_DATA_DIR_DEFAULT = "/mnt/workspace/codeg/data"
SNAPSHOT_INTERVAL_DEFAULT = 1.0  # seconds; crash RPO is at most one interval
# Candidate live DB layouts: current top-level file, plus the db/ subdir
# layout referenced by newer codeg-server builds. Every entry is mirrored
# to the same relative path on the OSS side.
DB_REL_PATHS = ("codeg.db", os.path.join("db", "codeg.db"))
# Transient sidecars that must never be mirrored in either direction.
TRANSIENT_SUFFIXES = ("-shm", "-wal", "-journal", ".lock", ".tmp")
SNAPSHOT_HEARTBEAT = ".snapshot.json"
SNAPSHOT_STAGE_PREFIX = ".snapshot-stage-"

# Filled by configure_environment(); read by main(). Kept as a module-level
# dict (instead of changing the (codeg_env, cf_env) return signature) so
# existing callers and tests keep working.
SNAPSHOT_CONFIG = {
    "local_dir": LOCAL_DATA_DIR_DEFAULT,
    "oss_dir": OSS_DATA_DIR_DEFAULT,
    "interval": SNAPSHOT_INTERVAL_DEFAULT,
}


def snap_log(message):
    print(f"[codeg-lite][snapshot] {message}", flush=True)


def is_codeg_reachable():
    """HTTP 401 is expected if Codeg authenticates the root endpoint."""
    try:
        with urlopen("http://127.0.0.1:3080/", timeout=1.5) as response:
            return response.status < 500
    except HTTPError as exc:
        return exc.code in (400, 401, 403, 404)
    except (URLError, OSError, TimeoutError):
        return False


class SnapshotState:
    """OSS mirror status for /health; also serializes snapshot runs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.op_lock = threading.Lock()
        self.last_ok = None
        self.last_error = None
        self.db_sigs = {}


class ProcessState:
    def __init__(self):
        self.processes = {}
        self.last_started = {}
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.snap = None  # SnapshotState, attached by main(); None in tests.

    def running(self, name):
        with self.lock:
            process = self.processes.get(name)
            return process is not None and process.poll() is None

    def snapshot(self):
        status = {
            "codeg": "reachable" if is_codeg_reachable() else "unreachable",
            "cloudflared_process": "running" if self.running("cloudflared") else "stopped",
            "uptime_seconds": int(time.time() - self.started_at),
        }
        snap = self.snap
        if snap is not None:
            with snap.lock:
                last_ok = snap.last_ok
                last_error = snap.last_error
            status["snapshot_last_ok_age_s"] = (
                int(time.time() - last_ok) if last_ok else None
            )
            status["snapshot_error"] = last_error
        else:
            status["snapshot_last_ok_age_s"] = None
            status["snapshot_error"] = None
        return status


class HealthHandler(BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
        # 兼容魔搭访问路径及查询参数
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
        snap_age = status.get("snapshot_last_ok_age_s")
        snap_err = status.get("snapshot_error")
        snap_text = (
            "never" if snap_age is None else f"{snap_age}s ago"
        ) + (f" (error: {html.escape(str(snap_err))})" if snap_err else "")
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
                "<p>Container uptime: <code>" + str(status["uptime_seconds"]) + "s</code></p>"
                "<p>OSS snapshot: <code>" + snap_text + "</code></p></div>"
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


def is_transient_name(name):
    """True for SQLite sidecars, lock files, temp files, and our own staging."""
    if name == SNAPSHOT_HEARTBEAT or name.startswith(SNAPSHOT_STAGE_PREFIX):
        return True
    return name.endswith(TRANSIENT_SUFFIXES)


def live_db_paths(local_dir):
    """Existing non-empty live DB main files (handles both known layouts)."""
    found = []
    for rel in DB_REL_PATHS:
        path = os.path.join(local_dir, rel)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                found.append((rel, path))
        except OSError:
            continue
    return found


def db_sig(path):
    """Change signature covering both the main file and its WAL sidecar.

    The main file's mtime alone is NOT enough: in WAL mode, fresh writes
    may live only in ``-wal`` while the main file is untouched, so both
    must be compared to decide whether a snapshot round can be skipped.
    """
    try:
        wal = path + "-wal"
        if os.path.exists(wal):
            wal_sig = (os.path.getsize(wal), int(os.stat(wal).st_mtime_ns))
        else:
            wal_sig = (0, 0)
        return (os.path.getsize(path), int(os.stat(path).st_mtime_ns), wal_sig)
    except OSError:
        return None


def backup_sqlite_file(src, stage, timeout=10.0):
    """Consistent copy of a live (WAL-mode, possibly locked) SQLite DB.

    Uses the sqlite3 backup API, so the result is consistent even while
    codeg-server holds the DB open. Never plain-copy a live DB file.
    """
    try:
        os.unlink(stage)
    except OSError:
        pass
    src_con = sqlite3.connect(src, timeout=timeout)
    try:
        try:
            src_con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except sqlite3.OperationalError:
            pass  # busy: the backup below still yields a consistent image
        dst_con = sqlite3.connect(stage, timeout=timeout)
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.unlink(stage + suffix)
        except OSError:
            pass


def atomic_replace(src_file, dst_path):
    """Publish a file atomically: readers only ever see the complete copy."""
    tmp = dst_path + ".tmp"
    shutil.copy2(src_file, tmp)
    try:
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass  # ossfs may ignore fsync; the rename below is still atomic
    os.replace(tmp, dst_path)


def atomic_write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def mirror_is_current(dst, src_stat):
    try:
        dst_stat = os.stat(dst)
    except OSError:
        return False
    if dst_stat.st_size != src_stat.st_size:
        return False
    # ossfs timestamp granularity is coarse; allow a small skew.
    return abs(dst_stat.st_mtime - src_stat.st_mtime) <= 2


def sync_tree_local_to_oss(local_dir, oss_dir):
    """One-way mirror of non-DB, non-transient files. Local is the master.

    Files deleted locally are intentionally NOT deleted on the OSS side:
    with a single writer, stray OSS files are cheap storage, while an
    accidental delete would be data loss.
    """
    copied = 0
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            if is_transient_name(name):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, local_dir)
            if rel in DB_REL_PATHS:
                continue  # live DB main files travel via the backup API only
            dst = os.path.join(oss_dir, rel)
            try:
                src_stat = os.stat(src)
            except OSError:
                continue
            if mirror_is_current(dst, src_stat):
                continue
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                snap_log(f"sync skipped {rel}: {exc}")
    return copied


def cleanup_transients(root):
    """Best-effort removal of stale SQLite sidecars / lock files."""
    if not os.path.isdir(root):
        return
    for cur, _dirs, files in os.walk(root):
        for name in files:
            if name == SNAPSHOT_HEARTBEAT or not name.endswith(TRANSIENT_SUFFIXES):
                continue
            try:
                os.unlink(os.path.join(cur, name))
            except OSError:
                pass


def restore_from_oss(oss_dir, local_dir):
    """Fill-missing restore before the server starts (no live writers yet).

    Only files missing locally are copied, so a surviving local master is
    never overwritten by an older snapshot. Plain copy is safe here because
    the OSS mirror itself was written via the backup API (single-file,
    checkpointed) and no writer is running during restore.
    Returns the number of files restored.
    """
    if not os.path.isdir(oss_dir):
        return 0
    restored = 0
    for root, _dirs, files in os.walk(oss_dir):
        for name in files:
            if is_transient_name(name):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, oss_dir)
            dst = os.path.join(local_dir, rel)
            if os.path.exists(dst):
                continue
            try:
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
            except OSError as exc:
                snap_log(f"restore skipped {rel}: {exc}")
    cleanup_transients(local_dir)
    return restored


def snapshot_once(local_dir, oss_dir, snap_state, final=False):
    """Mirror local master -> OSS. Best-effort; returns True on success."""
    with snap_state.op_lock:
        try:
            Path(oss_dir).mkdir(parents=True, exist_ok=True)
            timeout = 30.0 if final else 10.0
            with snap_state.lock:
                known_sigs = dict(snap_state.db_sigs)
            backed = 0
            for rel, src in live_db_paths(local_dir):
                sig = db_sig(src)
                if sig is None:
                    continue
                if not final and known_sigs.get(rel) == sig:
                    continue  # neither main file nor WAL changed
                stage = os.path.join(
                    local_dir, SNAPSHOT_STAGE_PREFIX + rel.replace(os.sep, "_")
                )
                try:
                    backup_sqlite_file(src, stage, timeout=timeout)
                    dst = os.path.join(oss_dir, rel)
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace(stage, dst)
                    known_sigs[rel] = sig
                    backed += 1
                except sqlite3.OperationalError as exc:
                    snap_log(f"db busy, retry next round: {rel}: {exc}")
                finally:
                    try:
                        os.unlink(stage)
                    except OSError:
                        pass
            copied = sync_tree_local_to_oss(local_dir, oss_dir)
            # Drop pre-migration leftovers (e.g. old -shm/-wal/-lock files);
            # the mirror itself never contains sidecars.
            cleanup_transients(oss_dir)
            atomic_write_json(
                os.path.join(oss_dir, SNAPSHOT_HEARTBEAT),
                {
                    "last_ok": int(time.time()),
                    "final": bool(final),
                    "db_backed_up": backed,
                    "files_synced": copied,
                },
            )
            with snap_state.lock:
                snap_state.last_ok = time.time()
                snap_state.last_error = None
                snap_state.db_sigs = known_sigs
            snap_log(f"snapshot ok: db={backed} files={copied} final={bool(final)}")
            return True
        except Exception as exc:  # never break supervision over a snapshot
            with snap_state.lock:
                snap_state.last_error = f"{type(exc).__name__}: {exc}"
            snap_log(f"snapshot failed: {exc}")
            return False


def snapshot_loop(stopping, local_dir, oss_dir, interval, snap_state):
    while not stopping.wait(interval):
        snapshot_once(local_dir, oss_dir, snap_state, final=False)


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

    # CODEG_DATA_DIR (OSS path) is kept as the snapshot mirror; the server
    # itself runs against the local fast disk below.
    oss_dir = (
        os.environ.get("CODEG_OSS_DATA_DIR", "").strip()
        or os.environ.get("CODEG_DATA_DIR", OSS_DATA_DIR_DEFAULT)
    )
    local_dir = (
        os.environ.get("CODEG_LOCAL_DATA_DIR", "").strip()
        or LOCAL_DATA_DIR_DEFAULT
    )
    try:
        interval = float(
            os.environ.get("CODEG_SNAPSHOT_INTERVAL_S", str(SNAPSHOT_INTERVAL_DEFAULT))
        )
    except ValueError:
        raise RuntimeError("CODEG_SNAPSHOT_INTERVAL_S must be a number of seconds.")
    if interval <= 0:
        raise RuntimeError("CODEG_SNAPSHOT_INTERVAL_S must be > 0.")

    paths = [
        os.environ.get("HOME", "/mnt/workspace/codeg/userhome"),
        os.environ.get("CODEG_HOME", "/mnt/workspace/codeg/codeg-home"),
        oss_dir,
        local_dir,
        "/mnt/workspace/codeg/projects",
    ]
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)
    os.chdir("/mnt/workspace/codeg/projects")

    codeg_env = dict(os.environ)
    codeg_env.pop("TUNNEL_TOKEN", None)
    codeg_env.pop("CF_TUNNEL_TOKEN", None)
    codeg_env["CODEG_DATA_DIR"] = local_dir
    codeg_env["CODEG_OSS_DATA_DIR"] = oss_dir
    codeg_env["CODEG_SNAPSHOT_INTERVAL_S"] = str(interval)
    cf_env = {
        key: value for key, value in os.environ.items()
        if key in (
            "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "HTTP_PROXY",
            "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy",
            "https_proxy", "no_proxy", "all_proxy", "TUNNEL_TRANSPORT_PROTOCOL"
        )
    }
    cf_env["TUNNEL_TOKEN"] = tunnel_token  # Avoid token in argv / build output.
    SNAPSHOT_CONFIG.update(
        {"local_dir": local_dir, "oss_dir": oss_dir, "interval": interval}
    )
    return codeg_env, cf_env


def main():
    os.umask(0o077)
    codeg_env, cf_env = configure_environment()
    local_dir = SNAPSHOT_CONFIG["local_dir"]
    oss_dir = SNAPSHOT_CONFIG["oss_dir"]
    interval = SNAPSHOT_CONFIG["interval"]
    snap_state = SnapshotState()

    restored = restore_from_oss(oss_dir, local_dir)
    snap_log(f"startup restore: {restored} file(s) {oss_dir} -> {local_dir}")
    snap_log(f"live dir={local_dir} mirror={oss_dir} interval={interval:g}s")

    state = ProcessState()
    state.snap = snap_state
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
    snap_thread = threading.Thread(
        target=snapshot_loop,
        args=(stopping, local_dir, oss_dir, interval, snap_state),
        daemon=True,
    )
    snap_thread.start()

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
        # Graceful flush #1 while writers are still up (backup API is
        # consistent against live writers), so almost nothing is left for
        # the post-shutdown pass.
        snap_log("graceful flush before stopping children")
        snapshot_once(local_dir, oss_dir, snap_state, final=True)
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
        # Graceful flush #2 after writers are gone: catches anything
        # written during the shutdown window. Zero loss on graceful stop.
        snap_log("final flush after children stopped")
        snapshot_once(local_dir, oss_dir, snap_state, final=True)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"[codeg-lite] configuration error: {exc}", flush=True)
        raise SystemExit(2)
