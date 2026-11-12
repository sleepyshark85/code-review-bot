"""`UrllibTransport` against a local HTTP server on the loopback interface."""

import json
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from review_bot.providers.openai_compat import HttpResponse, UrllibTransport
from review_bot.providers.openai_wire import MAX_RESPONSE_BYTES


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = ()
    hang_up: bool = False


@dataclass
class Server:
    url: str = ""
    received: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)
    respond: Callable[[str], Reply] = lambda _path: Reply()


@pytest.fixture
def server() -> Iterator[Server]:
    state = Server()

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            headers = {name.lower(): value for name, value in self.headers.items()}
            state.received.append((self.path, headers, body))
            answer = state.respond(self.path)
            if answer.hang_up:
                self.close_connection = True
                return
            self.send_response(answer.status)
            for name, value in answer.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        do_GET = do_POST = _handle  # noqa: N815

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


async def post(url: str) -> HttpResponse:
    return await UrllibTransport().post_json(
        url,
        {"model": "m1", "text": "ü"},
        headers={"Authorization": "Bearer k"},
        timeout_s=5.0,
    )


async def test_post_json_sends_the_body_as_json_with_the_headers(server: Server) -> None:
    server.respond = lambda _path: Reply(200, b'{"ok": true}', (("X-Id", "7"),))

    response = await post(f"{server.url}/v1/chat/completions")

    ((path, headers, body),) = server.received
    assert path == "/v1/chat/completions"
    assert json.loads(body) == {"model": "m1", "text": "ü"}
    assert headers["authorization"] == "Bearer k"
    assert headers["content-type"] == "application/json"
    assert response.status == 200
    assert response.body == b'{"ok": true}'
    assert response.headers["X-Id"] == "7"


async def test_post_json_returns_an_error_status_as_a_response(server: Server) -> None:
    server.respond = lambda _path: Reply(503, b"busy", (("Retry-After", "3"),))

    response = await post(server.url)

    assert (response.status, response.body, response.headers["Retry-After"]) == (503, b"busy", "3")


async def test_post_json_never_follows_a_redirect(server: Server) -> None:
    elsewhere = f"{server.url}/elsewhere"
    server.respond = lambda path: (
        Reply() if path == "/elsewhere" else Reply(302, b"", (("Location", elsewhere),))
    )

    response = await post(f"{server.url}/v1")

    assert response.status == 302
    assert [path for path, _headers, _body in server.received] == ["/v1"]


async def test_post_json_reads_at_most_one_byte_past_4_mb(server: Server) -> None:
    server.respond = lambda _path: Reply(200, b"x" * (MAX_RESPONSE_BYTES + 100))

    response = await post(server.url)

    assert len(response.body) == MAX_RESPONSE_BYTES + 1


async def test_post_json_to_a_closed_port_raises_connection_refused() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    with pytest.raises(ConnectionRefusedError):
        await post(f"http://127.0.0.1:{port}/v1")


async def test_post_json_raises_when_the_server_hangs_up(server: Server) -> None:
    server.respond = lambda _path: Reply(hang_up=True)

    with pytest.raises(ConnectionResetError):
        await post(server.url)
