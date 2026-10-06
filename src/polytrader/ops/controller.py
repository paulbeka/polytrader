"""Private Unix-socket controller; the browser never receives host credentials."""

import argparse
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import socketserver
import signal
import threading

from .files import encode
from .locking import lock
from .management import Manager, preview, save_bot, validate_config, deployment, initialize


class UnixConnection(HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(path, endpoint, payload=None):
    con = UnixConnection(path)
    try:
        con.request("POST" if payload is not None else "GET", endpoint,
                    body=encode(payload) if payload is not None else None,
                    headers={"Content-Type": "application/json", "X-Polytrader-Control": "1"})
        response = con.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise ValueError(result.get("error", "Controller request failed"))
        return result
    finally:
        con.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        data = encode(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/snapshot":
            return self.reply(404, {"error": "Unknown endpoint"})
        self.reply(200, self.server.manager.snapshot(containers=True))

    def do_POST(self):
        # Socket filesystem permissions authorize the dashboard process. Reject
        # browser-style requests as an additional boundary; no TCP listener exists.
        if self.headers.get("X-Polytrader-Control") != "1" or self.headers.get("Origin"):
            return self.reply(403, {"error": "Authorized local client required"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("Request size must be between 1 byte and 1 MiB")
            body = json.loads(self.rfile.read(size))
            manager = self.server.manager
            if self.path == "/preview":
                result = preview(manager.root, body["action"], body["instances"], body.get("release"))
            elif self.path == "/jobs":
                result = {"id": manager.submit(body["plan"], body["request_id"], "private-dashboard")}
            elif self.path == "/config/validate":
                result = {"config_hash": validate_config(body["strategy"], body["text"], manager.root),
                          "note": "Syntax and settings valid; deployment also validates current public markets."}
            elif self.path == "/bots":
                result = save_bot(manager.root, body["name"], body["strategy"], body["text"],
                                  body.get("display_name", ""), update=body.get("update", False))
            elif self.path == "/releases":
                result = initialize(manager.root, body["manifest"])
            elif self.path == "/config/read":
                _, path = deployment.definition(manager.root, body["name"])
                result = {"text": path.read_text(encoding="utf-8")}
            else:
                return self.reply(404, {"error": "Unknown endpoint"})
            self.reply(200, result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.reply(400, {"error": str(exc)})


def serve(root, socket_path=None):
    root = Path(root).resolve()
    socket_path = Path(socket_path or root / "run/control.sock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    # One controller process. The separate management lock is shared with CLI jobs.
    with lock(root / "locks/controller.lock"):
        manager = Manager(root)
        manager.reconcile()
        socket_path.unlink(missing_ok=True)
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        with Server(str(socket_path), Handler) as server:
            server.manager = manager
            socket_path.chmod(0o660)
            if os.name != "nt" and os.geteuid() == 0:
                os.chown(socket_path, 0, 10001)
            stopped = threading.Event()
            def worker():
                while not stopped.wait(1):
                    identity = manager.next_queued()
                    if identity:
                        try:
                            manager.execute(identity)
                        except OSError:
                            # CLI currently owns the management lock; retry later.
                            continue
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            def shutdown(*_):
                stopped.set()
                threading.Thread(target=server.shutdown, daemon=True).start()
            previous = signal.signal(signal.SIGTERM, shutdown)
            try:
                server.serve_forever(poll_interval=.5)
            finally:
                stopped.set()
                thread.join(timeout=1100)
                signal.signal(signal.SIGTERM, previous)
                socket_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/srv/polytrader"))
    parser.add_argument("--socket", type=Path)
    args = parser.parse_args()
    serve(args.root, args.socket)


if __name__ == "__main__":
    main()
