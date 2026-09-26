import asyncio
import http.client
import json
import threading
import unittest

from polytrader.data import DataError
from polytrader.orderbook.viewer import FeedController, FeedHub, make_server


def state(name="one"):
    return {"state": "running", "event": {"slug": name}, "books": [], "excluded": [], "error": None}


class ViewerHttpTests(unittest.TestCase):
    def setUp(self):
        self.hub = FeedHub()
        self.selections = []
        self.server = make_server(self.hub, self.selections.append, port=0)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.connections = []

    def tearDown(self):
        self.hub.close()
        for connection in self.connections:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        self.connections.append(connection)
        connection.request(method, path, body=body, headers=headers or {})
        return connection.getresponse()

    def data_frame(self, response):
        while True:
            line = response.readline()
            if not line:
                self.fail("Stream closed before snapshot")
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    def test_two_stream_clients_receive_same_state_independently(self):
        self.hub.publish(state())
        first = self.request("GET", "/api/stream")
        second = self.request("GET", "/api/stream")
        self.assertEqual(first.getheader("Content-Type"), "text/event-stream")
        a, b = self.data_frame(first), self.data_frame(second)
        self.assertEqual(a["version"], b["version"])
        self.hub.publish(state("two"))
        a, b = self.data_frame(first), self.data_frame(second)
        self.assertEqual(a["event"]["slug"], "two")
        self.assertEqual(a["version"], b["version"])
        first.close()
        self.hub.publish(state("three"))
        self.assertEqual(self.data_frame(second)["event"]["slug"], "three")
        second.close()

    def test_snapshot_and_packaged_viewer(self):
        self.hub.publish(state())
        response = self.request("GET", "/api/books")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["event"]["slug"], "one")
        response = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Live order books", response.read())

    def test_event_selection_validation_and_origin(self):
        response = self.request("POST", "/api/event", json.dumps({"event": "https://polymarket.com/event/example"}),
                                {"Content-Type": "application/json"})
        self.assertEqual(response.status, 202)
        response.read()
        self.assertEqual(self.selections, ["example"])
        for body, headers, status in [
            ('{"event":"../bad"}', {"Content-Type": "application/json"}, 400),
            ('{"event":"example"}', {"Content-Type": "text/plain"}, 400),
            ('{"event":"example"}', {"Content-Type": "application/json", "Origin": "https://other.example"}, 403),
        ]:
            with self.subTest(body=body, headers=headers):
                response = self.request("POST", "/api/event", body, headers)
                self.assertEqual(response.status, status)
                response.read()
        self.assertEqual(self.selections, ["example"])
        response = self.request("GET", "/api/books", headers={"Host": "untrusted.example"})
        self.assertEqual(response.status, 403)
        response.read()

    def test_replay_control_is_separate_from_live_event_switching(self):
        # A live server must reject replay requests rather than changing its feed.
        response = self.request("POST", "/api/replay", '{"speed":10}', {"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response.read()


class ViewerClient:
    def __init__(self):
        self.closed = 0

    def get_event(self, slug):
        if slug == "bad":
            raise DataError("Discovery failed")
        return {"slug": slug, "markets": [{"id": slug, "slug": slug, "outcomes": ["Yes"],
                                           "clobTokenIds": [slug]}]}

    async def stream(self, tokens):
        try:
            yield {"event_type": "book", "asset_id": tokens[0], "timestamp": "1788220800000",
                   "bids": [{"price": ".4", "size": "10"}],
                   "asks": [{"price": ".5", "size": "20"}]}
            await asyncio.Event().wait()
        finally:
            self.closed += 1


class ViewerControllerTests(unittest.IsolatedAsyncioTestCase):
    async def wait_state(self, hub, predicate):
        async with asyncio.timeout(3):
            while not predicate(hub.read()):
                await asyncio.sleep(.01)
        return hub.read()

    async def test_switching_event_closes_old_feed_and_publishes_new_books(self):
        hub, client = FeedHub(), ViewerClient()
        controller = FeedController(hub, client=client)
        try:
            controller.select("one")
            initial = await self.wait_state(hub, lambda p: p["books"] and p["books"][0]["status"] == "live")
            self.assertEqual(initial["books"][0]["best_ask"]["size"], "20")
            controller.select("two")
            await self.wait_state(hub, lambda p: p["books"] and p["books"][0]["token_id"] == "two"
                                  and p["books"][0]["status"] == "live")
            self.assertEqual(client.closed, 1)
            controller.select("bad")
            error = await self.wait_state(hub, lambda p: p["state"] == "error")
            self.assertEqual(error["books"], [])
            self.assertIn("Discovery failed", error["error"])
        finally:
            await controller.close()
        self.assertEqual(client.closed, 2)


if __name__ == "__main__":
    unittest.main()
