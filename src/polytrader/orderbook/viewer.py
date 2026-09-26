"""Local dashboard and shared HTTP/SSE access to one maintained event feed."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import math
import threading
from urllib.parse import urlparse

from polytrader.orderbook.api import resolve_books
from polytrader.orderbook.service import OrderBookService


class FeedHub:
    """Thread-safe latest state; independent HTTP clients never consume each other's updates."""

    def __init__(self):
        self.condition = threading.Condition()
        self.version = 0
        self.closed = False
        self.payload = {"state": "idle", "event": {}, "books": [], "excluded": [],
                        "error": None, "published_at": None, "version": 0}

    def publish(self, payload):
        with self.condition:
            self.version += 1
            self.payload = {**payload, "version": self.version,
                            "published_at": datetime.now(timezone.utc).isoformat()}
            self.condition.notify_all()

    def read(self):
        with self.condition:
            return self.payload

    def wait(self, version, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.version != version or self.closed, timeout)
            return self.payload, self.closed

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class FeedController:
    def __init__(self, hub, *, client=None):
        self.hub = hub
        self.client = client
        self.task = None

    def select(self, event=None, **selection):
        # Called on the asyncio loop. Cancelling an older discovery prevents it
        # from publishing over a newer selection even if its HTTP thread finishes later.
        if self.task is not None:
            self.task.cancel()
        self.task = asyncio.create_task(self._follow(event, **selection))

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def _follow(self, event, **selection):
        base = {"event": {"slug": event}, "books": [], "excluded": [], "error": None}
        self.hub.publish({**base, "state": "connecting"})
        collection = None
        try:
            collection = await asyncio.to_thread(resolve_books, event, client=self.client, **selection)
            if not collection.books:
                raise ValueError("No eligible outcome books in this selection")
            async with OrderBookService(collection, client=self.client, max_retries=None) as service:
                # Sampling notifications keeps memory and browser traffic bounded.
                # The service still processes every incoming depth update.
                async def check_updates():
                    async for _ in service.updates():
                        pass
                consumer = asyncio.create_task(check_updates())
                try:
                    while True:
                        self.hub.publish({**collection.to_dict(), "state": "running", "error": None})
                        if consumer.done():
                            await consumer  # Propagate feed errors to every attached client.
                            break
                        await asyncio.sleep(.25)
                finally:
                    consumer.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer
            self.hub.publish({**collection.to_dict(), "state": "stopped", "error": None})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.hub.publish({**(collection.to_dict() if collection else base),
                              "state": "error", "error": str(exc)})


def make_server(hub, select, *, port=8765, replay=None):
    """Bind only to loopback; POST changes the shared event for every consumer."""
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            port = self.server.server_port
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def send_payload(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                self.send_payload(403, {"error": "Use the localhost address"})
                return
            path = urlparse(self.path).path
            if path == "/":
                self.send_payload(200, files("polytrader.orderbook").joinpath("viewer.html").read_bytes(),
                                  "text/html; charset=utf-8")
            elif path == "/api/books":
                self.send_payload(200, hub.read())
            elif path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                version = -1
                self.connection.settimeout(15)
                try:
                    while True:
                        payload, closed = hub.wait(version, timeout=2)
                        if closed:
                            break
                        if payload["version"] == version:
                            frame = "event: heartbeat\ndata: {}\n\n"
                        else:
                            version = payload["version"]
                            frame = f"id: {version}\ndata: {json.dumps(payload, allow_nan=False)}\n\n"
                        self.wfile.write(frame.encode())
                        self.wfile.flush()
                except (OSError, TimeoutError):
                    pass
            else:
                self.send_payload(404, {"error": "Not found"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.valid_host() or (origin and origin != f"http://{self.headers.get('Host')}"):
                self.send_payload(403, {"error": "Only same-origin requests are accepted"})
                return
            if self.path not in {"/api/event", "/api/replay"}:
                self.send_payload(404, {"error": "Not found"})
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected application/json")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size")
                self.connection.settimeout(5)
                payload = json.loads(self.rfile.read(length))
                if self.path == "/api/replay":
                    if replay is None:
                        raise ValueError("This server is a live feed, not a replay")
                    speed = payload.get("speed", 1) if isinstance(payload, dict) else None
                    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
                        raise ValueError("speed must be finite and positive")
                    replay(speed)
                    self.send_payload(202, {"speed": speed})
                    return
                if replay is not None:
                    raise ValueError("Cannot switch a replay server to a live event")
                if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
                    raise ValueError("Provide an event URL or slug")
                from polytrader.data.discovery import event_slug
                event = event_slug(payload["event"])
                select(event)
            except (ValueError, OSError) as exc:
                self.send_payload(400, {"error": str(exc)})
                return
            self.send_payload(202, {"event": event})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


async def serve_orderbooks(event=None, *, port=8765, client=None, **selection):
    hub = FeedHub()
    controller = FeedController(hub, client=client)
    loop = asyncio.get_running_loop()
    server = make_server(hub, lambda value: loop.call_soon_threadsafe(controller.select, value), port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Live order books: http://127.0.0.1:{server.server_port}", flush=True)
    if event or selection.get("token_ids"):
        controller.select(event, **selection)
    try:
        await asyncio.Event().wait()
    finally:
        await controller.close()
        hub.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


class ReplayController:
    """Feed the same viewer from disk; never creates a market client."""

    def __init__(self, hub, recording):
        self.hub, self.recording = hub, recording
        self.task = None

    def play(self, speed=1.0):
        if self.task is not None:
            self.task.cancel()
        self.task = asyncio.create_task(self._play(speed))

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def _play(self, speed):
        from polytrader.orderbook.recording import replay_orderbooks
        payload = {"mode": "replay", "state": "replaying", "event": self.recording.metadata["event"],
                   "books": [], "excluded": self.recording.metadata.get("excluded", []),
                   "error": None, "speed": speed, "recorded_at": self.recording.started_at.isoformat()}
        self.hub.publish(payload)
        try:
            async for frame in replay_orderbooks(self.recording, speed=speed):
                books = []
                for token, quote in frame.quotes.items():
                    books.append({**quote.to_dict(), "market": self.recording.markets[token],
                                  "bids": [quote.best_bid.to_dict()] if quote.best_bid else [],
                                  "asks": [quote.best_ask.to_dict()] if quote.best_ask else []})
                payload = {**payload, "books": books, "recorded_at": frame.recorded_at.isoformat(),
                           "elapsed_seconds": frame.elapsed_seconds, "end_reason": frame.end_reason,
                           "state": "replay_finished" if frame.end_reason else "replaying"}
                self.hub.publish(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.hub.publish({**payload, "state": "error", "error": str(exc)})


async def serve_replay(recording, *, speed=1.0, port=8765):
    from polytrader.orderbook.recording import _positive
    _positive(speed, "speed")
    hub = FeedHub()
    controller = ReplayController(hub, recording)
    loop = asyncio.get_running_loop()
    server = make_server(hub, None, port=port,
                         replay=lambda value: loop.call_soon_threadsafe(controller.play, value))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Recorded quotes: http://127.0.0.1:{server.server_port}", flush=True)
    controller.play(speed)
    try:
        await asyncio.Event().wait()
    finally:
        await controller.close()
        hub.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
