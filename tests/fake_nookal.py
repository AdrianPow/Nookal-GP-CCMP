"""In-process stand-in for the Nookal REST API.

A real HTTP server on localhost, so the tests exercise the actual requests
session, form encoding, GET-vs-POST, retries and the presigned-URL PUT —
not a monkeypatched stub. Nothing here talks to Nookal.

Usage:

    fake = FakeNookal()
    fake.route("verify", {"status": "success"})
    client = NookalClient(NookalConfig(api_key="k", base_url=fake.base_url))
    ...
    fake.stop()

Route values may be:
    dict                -> returned as JSON with HTTP 200
    (status, dict)      -> returned as JSON with that HTTP status
    (status, str)       -> returned verbatim (for non-JSON body tests)
    callable(params)    -> returning any of the above, for per-call behaviour

Unrouted endpoints answer the way Nookal rejects an endpoint name it does
not know, which is what drives the client's candidate-name fallback.
"""

from __future__ import annotations

import json
import threading
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_PREFIX = "/production/v2/"
S3_PREFIX = "/s3/"

# What Nookal returns for a function name it doesn't recognise: HTTP 200 with
# a failure envelope, not a 404.
UNKNOWN_FUNCTION = {
    "status": "failure",
    "details": {"errorMessage": "Unknown function requested"},
}


# The page Nookal actually serves for an endpoint name it doesn't know —
# captured verbatim from the live API on 2026-08-03 (updateMedicareDetails).
NOOKAL_404_PAGE = (
    "<html>\r\n    <head>\r\n        <title>Hold it right there!</title>\r\n"
    "    </head>\r\n    <body>\r\n"
    '        <p><img src="/images/v2.0/404.png" /></p>\r\n'
    "        <h1>Oh, no you don't.</h1>\r\n"
    "        <p>Officer Nigel noticed you were trying to escape to a page "
    "that doesn't exist.</p>\r\n"
    "        <!--404 Page Not Found-->\r\n"
    "    </body>\r\n</html>"
)


def success(results: dict | list) -> dict:
    """Nookal's success envelope: {"status":..,"data":{"results":..}}."""
    return {"status": "success", "data": {"results": results}}


def failure(message: str) -> dict:
    return {"status": "failure", "details": {"errorMessage": message}}


Call = namedtuple("Call", "method endpoint params body")


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 => connection closed per request, so a hung keep-alive can
    # never stall a test run.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # noqa: D102 - silence stderr spam
        pass

    # ------------------------------------------------------------ helpers

    def _body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, payload) -> None:
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload).encode()
            ctype = "application/json"
        else:
            raw = str(payload).encode()
            ctype = "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def _unpack(route, params):
        if callable(route):
            route = route(params)
        if isinstance(route, tuple):
            return route
        return 200, route

    # -------------------------------------------------------------- verbs

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self._api("GET", parsed.path, params)

    def do_POST(self):  # noqa: N802
        raw = self._body_bytes().decode()
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        self._api("POST", urlparse(self.path).path, params)

    def do_PUT(self):  # noqa: N802
        fake = self.server.fake
        body = self._body_bytes()
        path = urlparse(self.path).path
        with fake.lock:
            fake.calls.append(Call("PUT", path, {}, body))
            status = (fake.put_statuses.pop(0) if fake.put_statuses
                      else fake.put_status)
        self._send(status, "" if status == 204 else "ok")

    # ---------------------------------------------------------------- api

    def _api(self, verb: str, path: str, params: dict) -> None:
        fake = self.server.fake
        if not path.startswith(API_PREFIX):
            self._send(404, failure("no such path"))
            return
        endpoint = path[len(API_PREFIX):]
        with fake.lock:
            fake.calls.append(Call(verb, endpoint, params, None))
            route = fake.routes.get(endpoint, fake.default_route)
        status, payload = self._unpack(route, params)
        self._send(status, payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeNookal:
    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.default_route: object = UNKNOWN_FUNCTION
        self.calls: list[Call] = []
        self.put_status = 200
        self.put_statuses: list[int] = []   # consumed one per PUT, then falls
                                            # back to put_status
        self.lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- config

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/production/v2"

    def s3_url(self, name: str = "upload") -> str:
        return f"http://127.0.0.1:{self.port}{S3_PREFIX}{name}"

    def route(self, endpoint: str, response) -> None:
        self.routes[endpoint] = response

    def routes_from(self, mapping: dict) -> None:
        self.routes.update(mapping)

    # ----------------------------------------------------------- querying

    def endpoints(self, verb: str | None = None) -> list[str]:
        """Endpoint names in call order (PUTs appear as their path)."""
        with self.lock:
            return [c.endpoint for c in self.calls
                    if verb is None or c.method == verb]

    def sequence(self) -> list[tuple[str, str]]:
        with self.lock:
            return [(c.method, c.endpoint) for c in self.calls]

    def calls_to(self, endpoint: str) -> list[Call]:
        with self.lock:
            return [c for c in self.calls if c.endpoint == endpoint]

    def count(self, endpoint: str) -> int:
        return len(self.calls_to(endpoint))

    def last_params(self, endpoint: str) -> dict:
        calls = self.calls_to(endpoint)
        if not calls:
            raise AssertionError(f"{endpoint} was never called")
        return calls[-1].params

    def reset(self) -> None:
        with self.lock:
            self.calls.clear()

    # -------------------------------------------------------------- close

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
