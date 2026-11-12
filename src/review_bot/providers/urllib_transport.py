"""The real ``HttpTransport``: one JSON POST over ``urllib``, bounded by a deadline (ADR 0020).

A socket timeout bounds each read, not the request, so a server sending a byte at a time could
hold a request open for ever. Here a timer shuts the request's socket once its time is up, which
ends any read in progress, and the request fails with ``TimeoutError``. The timer covers sending,
the headers and the body, once ``connect()`` has returned. The earlier phases have other bounds:
the operating system bounds resolving the host name, each resolved address gets one socket
timeout to connect (so N unreachable addresses can take N timeouts), and CPython's ``ssl``
module bounds the whole TLS handshake by one socket timeout.
"""

import asyncio
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
from types import TracebackType
from typing import Final, Self

from review_bot.audit import JsonValue
from review_bot.no_redirect import NoRedirect
from review_bot.providers.openai_wire import MAX_RESPONSE_BYTES

__all__ = ["HttpResponse", "UrllibTransport"]

# The most read of a body: one byte past the limit, so an oversized body can be told apart.
_READ_BYTES: Final = MAX_RESPONSE_BYTES + 1


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """An HTTP response: its status, headers and body, of which at most 4 MB and a byte are read.

    The byte past 4 MB lets the provider tell an oversized body from one exactly at the limit.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes


class UrllibTransport:
    """An ``HttpTransport`` over ``urllib``, run in a thread so the event loop never blocks.

    It follows no redirect, and ``timeout_s`` bounds the whole request: connecting, sending, and
    reading the headers and the body.
    """

    async def post_json(
        self,
        url: str,
        body: Mapping[str, JsonValue],
        *,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> HttpResponse:
        """Send ``body`` as JSON and return the response, whatever its status.

        Raises:
            TimeoutError: If the request took longer than ``timeout_s``.
            OSError: If no response came back, such as ``ConnectionRefusedError``.
            HTTPException: If the response was malformed or cut off.
        """
        data = json.dumps(body).encode("utf-8")
        return await asyncio.to_thread(_post, url, data, headers, timeout_s)


def _post(url: str, data: bytes, headers: Mapping[str, str], timeout_s: float) -> HttpResponse:
    request = urllib.request.Request(  # noqa: S310 - the base URL's scheme is checked
        url,
        data=data,
        method="POST",
        headers={**headers, "Content-Type": "application/json", "User-Agent": "review-bot"},
    )
    with _Deadline(timeout_s) as deadline:
        opener = urllib.request.build_opener(
            NoRedirect, _HTTPHandler(deadline), _HTTPSHandler(deadline)
        )
        try:
            response = _open(opener, request, timeout_s)
        except (OSError, HTTPException) as exc:
            if deadline.has_passed:
                raise TimeoutError(f"no complete response within {timeout_s:g} s") from exc
            raise
        # A shut socket can also end a body with no declared length early, as if it were whole.
        if deadline.has_passed:
            raise TimeoutError(f"no complete response within {timeout_s:g} s")
    return response


def _open(
    opener: urllib.request.OpenerDirector, request: urllib.request.Request, timeout_s: float
) -> HttpResponse:
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return HttpResponse(response.status, dict(response.headers), response.read(_READ_BYTES))
    except urllib.error.HTTPError as exc:
        with exc:
            return HttpResponse(exc.code, dict(exc.headers), exc.read(_READ_BYTES))
    except urllib.error.URLError as exc:
        # The OS error inside says what failed, such as a refused connection.
        if isinstance(exc.reason, OSError):
            raise exc.reason from exc
        raise


class _Deadline:
    """Shuts every socket a request connects once ``timeout_s`` has passed.

    A socket connected after that is shut at once, so nothing outlives the deadline.
    """

    def __init__(self, timeout_s: float) -> None:
        self.has_passed = False
        self._sockets: list[socket.socket] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(timeout_s, self._pass)
        self._timer.daemon = True

    def __enter__(self) -> Self:
        self._timer.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._timer.cancel()

    def watch(self, sock: socket.socket) -> None:
        """Shut ``sock`` at the deadline, or now if it has passed."""
        with self._lock:
            self._sockets.append(sock)
            if self.has_passed:
                _shut(sock)

    def _pass(self) -> None:
        with self._lock:
            self.has_passed = True
            for sock in self._sockets:
                _shut(sock)


def _shut(sock: socket.socket) -> None:
    # Already closed or reset: the read it would end has ended anyway.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        return


class _WatchedHTTPConnection(HTTPConnection):
    """An HTTP connection whose socket its ``deadline`` shuts."""

    deadline: _Deadline

    def connect(self) -> None:
        """Connect, then hand the socket to the deadline."""
        super().connect()
        self.deadline.watch(self.sock)


class _WatchedHTTPSConnection(HTTPSConnection, _WatchedHTTPConnection):
    """An HTTPS connection whose sockets its ``deadline`` shuts.

    The plain socket is detached when TLS wraps it, so the timer can't end the handshake; the
    handshake has CPython's own bound of one socket timeout. The TLS socket is watched here, once
    the handshake is done.
    """

    def connect(self) -> None:
        """Connect and shake hands, then hand the TLS socket to the deadline."""
        super().connect()
        self.deadline.watch(self.sock)


class _WatchedConnections:
    """Makes the connections a handler opens, each watched by ``deadline``.

    Its signature is the one ``AbstractHTTPHandler.do_open`` calls a connection class with.
    """

    def __init__(self, kind: type[_WatchedHTTPConnection], deadline: _Deadline) -> None:
        self._kind = kind
        self._deadline = deadline

    def __call__(
        self,
        host: str,
        /,
        *,
        port: int | None = None,
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
        blocksize: int = 8192,
    ) -> HTTPConnection:
        """A connection to ``host``, not yet open."""
        connection = self._kind(
            host, port, timeout=timeout, source_address=source_address, blocksize=blocksize
        )
        connection.deadline = self._deadline
        return connection


class _HTTPHandler(urllib.request.HTTPHandler):
    """Opens ``http`` URLs over connections a deadline watches."""

    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._connections = _WatchedConnections(_WatchedHTTPConnection, deadline)

    def http_open(self, req: urllib.request.Request) -> HTTPResponse:
        return self.do_open(self._connections, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    """Opens ``https`` URLs over connections a deadline watches."""

    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._connections = _WatchedConnections(_WatchedHTTPSConnection, deadline)

    def https_open(self, req: urllib.request.Request) -> HTTPResponse:
        return self.do_open(self._connections, req)
