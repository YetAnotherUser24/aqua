#!/usr/bin/env python3
"""
One-command development stack: database, broker, gateway, virtual nodes, dashboard.

Brings up the whole system locally so the dashboard can be exercised against **real firmware**
telemetry — the virtual node runs the production serialiser and the production device models —
without a Pi, without mosquitto, and without touching anything that is deployed.

    python3 tools/dev-stack.py                 # then open http://localhost:8899
    python3 tools/dev-stack.py --seconds 120   # run for two minutes and exit

What it starts, and where:

    pgserver    embedded PostgreSQL, unix socket under --runtime-dir
    dev-broker  amqtt on 127.0.0.1:<mqtt-port>
    gateway     `python -m aqua_gateway.main` on <api-port>
    nodes       tools/virtual-nodes.py, which owns the firmware binary
    web         static `web/out` plus a reverse proxy for /api and /healthz on <web-port>

Everything runs as a child process rather than in-process, so each piece can be restarted,
inspected or killed on its own — and so a panic in one does not take the others with it.

The dashboard is served on the **same origin** as the API, exactly as Caddy does in production,
which is why the frontend's relative `/api/...` paths work unchanged here.
"""

from __future__ import annotations

import argparse
import http.server
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "gateway"
TOOLS = REPO_ROOT / "tools"
MIGRATIONS = GATEWAY_DIR / "migrations"

CHILDREN: list[subprocess.Popen] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def say(message: str) -> None:
    print(message, flush=True)


def wait_for_port(host: str, port: int, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def wait_for_http(url: str, timeout_s: float = 40.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def spawn(
    name: str,
    args: list[str],
    *,
    env: dict | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen:
    say(f"  starting {name}")
    process = subprocess.Popen(args, env=env, cwd=str(cwd) if cwd else None)
    CHILDREN.append(process)
    return process


def shutdown(*_: object) -> None:
    if not CHILDREN:
        return
    say("\nstopping children")
    for process in CHILDREN:
        if process.poll() is None:
            process.terminate()
    for process in CHILDREN:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    CHILDREN.clear()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def start_database(runtime: Path) -> str:
    """Start embedded PostgreSQL and apply every migration. Returns an asyncpg DSN."""
    try:
        import pgserver
    except ImportError:
        sys.exit("pgserver is not installed; pip install pgserver")

    data_dir = runtime / "pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = pgserver.get_server(str(data_dir))

    for sql in sorted(MIGRATIONS.glob("*.sql")):
        out = db.psql(sql.read_text())
        errors = [
            ln for ln in out.splitlines() if ln.startswith(("ERROR", "FATAL"))
        ]
        if errors:
            sys.exit(
                f"migration {sql.name} failed:\n  " + "\n  ".join(errors[:5])
            )

    say(
        f"  database ready ({len(list(MIGRATIONS.glob('*.sql')))} migrations applied)"
    )
    # pgserver listens on a unix socket inside the data directory.
    return f"postgresql://postgres@/postgres?host={data_dir}"


# ---------------------------------------------------------------------------
# Static dashboard + API proxy
# ---------------------------------------------------------------------------


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """
    Serves the static export and proxies the API onto the same origin.

    Same-origin matters: the frontend uses relative `/api/...` URLs precisely because Caddy
    puts both behind one hostname in production. Reproducing that here means the local stack
    exercises the same code path rather than a CORS-enabled variant that only exists in dev.
    """

    protocol_version = "HTTP/1.1"
    api_port = 8000
    upstream = "127.0.0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        if "/api/stream" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    def _is_api(self) -> bool:
        path = self.path.split("?", 1)[0]
        return path.startswith("/api/") or path in ("/healthz", "/metrics")

    def _proxy(self, method: str) -> None:
        url = f"http://{self.upstream}:{self.api_port}{self.path}"
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        request = urllib.request.Request(url, data=body, method=method)
        for header in ("Accept", "Content-Type"):
            if self.headers.get(header):
                request.add_header(header, self.headers[header])

        try:
            with urllib.request.urlopen(request, timeout=None) as response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in (
                        "content-length",
                        "transfer-encoding",
                        "connection",
                        "content-encoding",
                        "keep-alive",
                    ):
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()

                if "/api/stream" in (self.path or ""):
                    # Server-sent events never end, and `copyfileobj` blocks until it has
                    # read a full 64 KB -- which an event stream, emitting a few hundred
                    # bytes a second, will not do for a very long time. The browser
                    # therefore received nothing at all and sat on "connecting" forever,
                    # while `curl` against the gateway worked fine and made the proxy look
                    # innocent. `read1` returns as soon as any data is available, which is
                    # the only read that makes sense for a stream.
                    while True:
                        chunk = response.read1(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    shutil.copyfileobj(response, self.wfile, length=64 * 1024)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (urllib.error.URLError, OSError) as exc:
            message = f'{{"detail":"gateway unreachable: {exc}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(message)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(message)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self._is_api():
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self._is_api():
            return self._proxy("POST")
        self.send_error(404)

    def do_HEAD(self) -> None:  # noqa: N802
        if self._is_api():
            return self._proxy("HEAD")
        return super().do_HEAD()


def ensure_node_binary() -> None:
    """
    Build the host virtual node if it is missing.

    It is a compiled artefact and deliberately not in git, so without this the "one command"
    claim is false on a fresh clone — you get a clear error, but only after the database, broker
    and gateway have all been started for nothing.
    """
    binary = REPO_ROOT / "firmware" / "virtual_node" / "virtual_node"
    if binary.exists():
        return
    script = binary.parent / "build.sh"
    if not script.exists():
        sys.exit(f"{script} not found")
    say("  building the host virtual node (first run)")
    result = subprocess.run([str(script)], capture_output=True, text=True)
    if result.returncode != 0:
        say(result.stdout[-1500:])
        say(result.stderr[-3000:])
        sys.exit(
            "could not build firmware/virtual_node — set AQUA_WORKSPACE to the dir holding nodo-*"
        )


def serve_dashboard(
    web_root: Path, api_port: int, web_port: int
) -> http.server.ThreadingHTTPServer:
    handler = lambda *a, **kw: DashboardHandler(
        *a, directory=str(web_root), **kw
    )  # noqa: E731
    DashboardHandler.api_port = api_port
    server = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", default="/tmp/aqua-devstack")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=8899)
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument(
        "--hz", type=float, default=5.0, help="virtual node sampling rate"
    )
    parser.add_argument(
        "--seconds", type=float, default=0.0, help="0 = run until Ctrl-C"
    )
    parser.add_argument(
        "--no-nodes", action="store_true", help="stack only, no virtual nodes"
    )
    parser.add_argument("--web-root", default=str(REPO_ROOT / "web" / "out"))
    parser.add_argument(
        "--reset", action="store_true", help="wipe the runtime directory first"
    )
    args = parser.parse_args()

    runtime = Path(args.runtime_dir)
    if args.reset and runtime.exists():
        shutil.rmtree(runtime)
    for sub in ("telemetry", "state", "exports", "telemetry/unknown"):
        (runtime / sub).mkdir(parents=True, exist_ok=True)

    web_root = Path(args.web_root)
    if not (web_root / "index.html").exists():
        say(
            f"warning: {web_root}/index.html not found — run `npm run build` in web/ first"
        )

    signal.signal(signal.SIGINT, lambda *a: shutdown())
    signal.signal(signal.SIGTERM, lambda *a: shutdown())

    say("=== aqua dev stack ===")
    database_url = start_database(runtime)

    env = os.environ.copy()
    env.update(
        {
            "AQUA_ENVIRONMENT": "development",
            "AQUA_LOG_JSON": "false",
            "AQUA_LOG_LEVEL": "INFO",
            "AQUA_DATABASE_URL": database_url,
            "AQUA_MQTT__HOST": "127.0.0.1",
            "AQUA_MQTT__PORT": str(args.mqtt_port),
            "AQUA_API_PORT": str(args.api_port),
            "AQUA_ARCHIVE__ROOT": str(runtime / "telemetry"),
            "AQUA_ARCHIVE__UNKNOWN_FIELDS_PATH": str(
                runtime / "telemetry" / "unknown"
            ),
            "AQUA_MANIFEST_PATH": str(runtime / "state" / "manifest.db"),
            "AQUA_EXPORT_ROOT": str(runtime / "exports"),
            # Uploads disabled: there is no object store here, and a failing flush loop would
            # fill the log with noise that has nothing to do with the dashboard.
            "AQUA_BACKUP__ENABLED": "false",
            # Shorter buckets so the chart shows data within a couple of minutes rather than
            # making a reviewer wait.
            "AQUA_ROLLUP_INTERVAL_S": "15",
            "PYTHONPATH": str(GATEWAY_DIR),
            "PYTHONUNBUFFERED": "1",
        }
    )

    spawn(
        "broker",
        [
            sys.executable,
            str(TOOLS / "dev-broker.py"),
            "--port",
            str(args.mqtt_port),
        ],
    )
    if not wait_for_port("127.0.0.1", args.mqtt_port):
        shutdown()
        return 1
    say(f"  broker up on 127.0.0.1:{args.mqtt_port}")

    spawn(
        "gateway",
        [sys.executable, "-m", "aqua_gateway.main"],
        env=env,
        cwd=GATEWAY_DIR,
    )
    if not wait_for_http(
        f"http://127.0.0.1:{args.api_port}/healthz", timeout_s=45
    ):
        say("gateway did not become healthy")
        shutdown()
        return 1
    say(f"  gateway up on 127.0.0.1:{args.api_port}")

    if not args.no_nodes:
        ensure_node_binary()
        spawn(
            "virtual nodes",
            [
                sys.executable,
                str(TOOLS / "virtual-nodes.py"),
                "--hz",
                str(args.hz),
                "--port",
                str(args.mqtt_port),
            ],
            env=env,
        )

    dashboard = serve_dashboard(web_root, args.api_port, args.web_port)
    say(
        f"  dashboard on http://127.0.0.1:{args.web_port}  (static: {web_root})"
    )
    say("")
    say("  press Ctrl-C to stop")
    say("")

    started = time.time()
    try:
        while True:
            time.sleep(0.5)
            for process in list(CHILDREN):
                if process.poll() is not None:
                    say(
                        f"a child exited with code {process.returncode}; shutting down"
                    )
                    shutdown()
                    return 1
            if args.seconds and time.time() - started >= args.seconds:
                say(f"\n--seconds {args.seconds} elapsed")
                break
    except KeyboardInterrupt:
        pass
    finally:
        # Stop accepting requests before killing the gateway, otherwise the proxy serves a
        # burst of 502s during teardown. Then shut the server down explicitly: leaving it to
        # a daemon thread at interpreter exit logs a fatal "could not acquire lock for
        # <stderr>" error that looks like a crash and is not one.
        dashboard.shutdown()
        dashboard.server_close()
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
