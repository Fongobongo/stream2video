"""Tests for the proxy contract — download.validate_proxy_url /
mask_proxy_url / check_proxy_reachable.

The liveness probe is exercised against LOCAL socket stand-ins on
127.0.0.1 (a real 204/407/502-speaking fake proxy, a dead port, a
silent accept-only server) — no test may touch the network.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from stream2video.download import (
    PROXY_SCHEMES,
    check_proxy_reachable,
    mask_proxy_url,
    validate_proxy_url,
)


class TestValidateProxyUrl:
    """One format rule shared by the GUI dialog, load_config and
    validate_pipeline_config: a known yt-dlp scheme plus a host, an
    optional in-range numeric port, credentials allowed."""

    @pytest.mark.parametrize(
        "proxy",
        [
            "http://127.0.0.1:8080",
            "https://proxy.corp.example:3128",
            "socks5://user:pass@host:1080",
            "socks5h://host",  # no port — scheme default assumed
            "socks4://host:1080",
            "socks4a://host",
            "http://[::1]:8080",  # IPv6 literal
            "HTTP://Host.Example:8080",  # urlsplit lowercases the scheme
            "  http://127.0.0.1:8080  ",  # surrounding whitespace stripped
        ],
    )
    def test_valid_addresses(self, proxy: str):
        assert validate_proxy_url(proxy) is None

    @pytest.mark.parametrize("proxy", ["", "   ", "\t"])
    def test_empty_means_no_proxy_and_is_valid(self, proxy: str):
        assert validate_proxy_url(proxy) is None

    @pytest.mark.parametrize(
        "proxy",
        [
            "127.0.0.1:8080",  # the classic scheme-less typo
            "8080",
            "htt://host:8080",  # scheme typo
            "://host",
            "http://",  # no host
            "http://:1080",  # port but no host
            "http://host:abc",  # non-numeric port
            "http://host:99999",  # out-of-range port
        ],
    )
    def test_invalid_addresses(self, proxy: str):
        assert validate_proxy_url(proxy) is not None

    def test_scheme_list_is_the_yt_dlp_set(self):
        assert (
            frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"}) == PROXY_SCHEMES
        )

    def test_schemeless_message_names_examples(self):
        error = validate_proxy_url("127.0.0.1:8080")
        assert error is not None
        assert "proxy scheme" in error
        assert "http://127.0.0.1:8080" in error  # copy-pasteable example

    def test_bad_port_message_names_the_port_problem(self):
        error = validate_proxy_url("http://host:abc")
        assert error is not None
        assert "invalid port" in error


class TestMaskProxyUrl:
    """Credentials must never reach a doctor row or a log line — even
    for a garbage address that just failed validation."""

    @pytest.mark.parametrize(
        ("proxy", "masked"),
        [
            ("socks5://user:pass@host:1080", "socks5://***:***@host:1080"),
            ("http://user@proxy:8080", "http://***@proxy:8080"),  # user, no password
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),  # nothing to mask
            ("127.0.0.1:8080", "127.0.0.1:8080"),  # garbage passthrough
        ],
    )
    def test_masking(self, proxy: str, masked: str):
        assert mask_proxy_url(proxy) == masked

    def test_no_password_leaks_for_invalid_url_with_credentials(self):
        # An address that FAILS validation still gets masked when displayed.
        assert "secret" not in mask_proxy_url("htt://user:secret@host:1")


@contextmanager
def _local_proxy(respond: bytes | None, seen: list[bytes] | None = None) -> Iterator[int]:
    """A local TCP stand-in for a proxy on 127.0.0.1; yields the port.

    ``respond=None`` → accept connections and stay silent (a wedged
    proxy); otherwise read one request and send ``respond`` back. When
    ``seen`` is given, every received request is appended to it.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # listener closed — shutdown
            if respond is None:
                time.sleep(5)
                conn.close()
                continue
            try:
                data = conn.recv(65536)
                if seen is not None:
                    seen.append(data)
                conn.sendall(respond)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    try:
        yield port
    finally:
        srv.close()


def _dead_port() -> int:
    """Reserve a port and free it — connecting to it must be refused."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestCheckProxyReachable:
    """Liveness probe semantics, pinned against local socket stand-ins."""

    def test_invalid_address_short_circuits_without_network(self, monkeypatch):
        def _no_network(*args, **kwargs):  # pragma: no cover - fail loudly
            raise AssertionError("an invalid address must not be probed")

        monkeypatch.setattr(socket, "create_connection", _no_network)
        ok, detail = check_proxy_reachable("127.0.0.1:8080")
        assert ok is False
        assert "invalid address" in detail

    def test_tcp_refused_is_reported_with_host_and_port(self):
        port = _dead_port()
        ok, detail = check_proxy_reachable(f"socks5://127.0.0.1:{port}", timeout=2.0)
        assert ok is False
        assert "cannot connect" in detail
        assert f"127.0.0.1:{port}" in detail

    def test_http_204_through_proxy_is_reachable(self):
        with _local_proxy(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n") as port:
            ok, detail = check_proxy_reachable(f"http://127.0.0.1:{port}", timeout=5.0)
        assert ok is True
        assert "HTTP 204" in detail

    def test_http_200_through_proxy_is_reachable(self):
        with _local_proxy(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") as port:
            ok, detail = check_proxy_reachable(f"http://user:pw@127.0.0.1:{port}", timeout=5.0)
        assert ok is True
        assert "HTTP 200" in detail

    def test_http_credentials_are_sent_as_proxy_authorization(self):
        # A credentialed http proxy answers an unauthenticated request
        # with 407 — the probe must send Basic Proxy-Authorization from
        # the URL's userinfo so "credentials work" is distinguishable
        # from "credentials missing".
        import base64

        seen: list[bytes] = []
        token = base64.b64encode(b"proxyuser:proxypass").decode("ascii")
        with _local_proxy(
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n", seen=seen
        ) as port:
            ok, _detail = check_proxy_reachable(
                f"http://proxyuser:proxypass@127.0.0.1:{port}", timeout=5.0
            )
        assert ok is True
        assert f"Proxy-Authorization: Basic {token}".encode("ascii") in seen[0]

    def test_http_407_with_credentials_sent_reports_rejection(self):
        response = (
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Content-Length: 0\r\nProxy-Authenticate: Basic\r\n\r\n"
        )
        with _local_proxy(response) as port:
            ok, detail = check_proxy_reachable(f"http://user:pw@127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "407" in detail
        assert "rejected the credentials" in detail

    def test_http_407_reports_authentication_problem(self):
        response = (
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Content-Length: 0\r\nProxy-Authenticate: Basic\r\n\r\n"
        )
        with _local_proxy(response) as port:
            ok, detail = check_proxy_reachable(f"http://127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "407" in detail
        assert "authentication" in detail

    def test_http_502_reports_status_verbatim(self):
        with _local_proxy(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n") as port:
            ok, detail = check_proxy_reachable(f"http://127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "502" in detail

    def test_socks4_scheme_gets_tcp_only_probe(self):
        # A silent accept-only server: TCP connect succeeds, no handshake
        # is attempted — the detail must say exactly that.
        with _local_proxy(None) as port:
            ok, detail = check_proxy_reachable(f"socks4://127.0.0.1:{port}", timeout=5.0)
        assert ok is True
        assert "handshake not probed" in detail

    def test_silent_http_proxy_times_out(self):
        with _local_proxy(None) as port:
            ok, detail = check_proxy_reachable(f"http://127.0.0.1:{port}", timeout=0.5)
        assert ok is False
        assert "no HTTP response" in detail


@contextmanager
def _socks5_proxy(
    auth: tuple[str, str] | None,
    connect_reply: int = 0,
    http_response: bytes = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n",
) -> Iterator[tuple[int, dict]]:
    """A fake SOCKS5 server on 127.0.0.1; yields ``(port, seen)``.

    Speaks RFC 1928 + RFC 1929 exactly as the probe expects: greeting
    (replying ``no-auth`` when ``auth`` is None, ``user/pass`` otherwise
    and validating the credentials against ``auth``), a CONNECT whose
    reply code is ``connect_reply``, and one HTTP response through the
    tunnel. ``seen`` records what the client actually sent.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    seen: dict = {}

    def _read_exact(conn: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = conn.recv(count - len(buf))
            if not chunk:
                raise ConnectionError("client closed early")
            buf += chunk
        return buf

    def _handle(conn: socket.socket) -> None:
        try:
            head = _read_exact(conn, 2)  # VER NMETHODS
            seen["greeting"] = head + _read_exact(conn, head[1])
            if auth is None:
                conn.sendall(b"\x05\x00")
            else:
                conn.sendall(b"\x05\x02")
                head = _read_exact(conn, 2)
                user = _read_exact(conn, head[1])
                password = _read_exact(conn, _read_exact(conn, 1)[0])
                seen["user"] = user.decode("utf-8")
                seen["password"] = password.decode("utf-8")
                ok = user.decode("utf-8") == auth[0] and password.decode("utf-8") == auth[1]
                conn.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
                if not ok:
                    conn.close()
                    return
            connect = _read_exact(conn, 5)  # VER CMD RSV ATYP LEN
            seen["connect"] = connect
            if connect[3] == 3:
                _read_exact(conn, connect[4] + 2)
            conn.sendall(bytes([5, connect_reply, 0, 1]) + b"\x00" * 6)
            if connect_reply != 0:
                conn.close()
                return
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            seen["request"] = request
            conn.sendall(http_response)
        except OSError:
            pass
        finally:
            conn.close()

    def _serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()

    threading.Thread(target=_serve, daemon=True).start()
    try:
        yield port, seen
    finally:
        srv.close()


class TestCheckProxyReachableSocks5:
    """The full SOCKS5 probe: greeting + RFC 1929 auth + CONNECT + one
    GET through the tunnel, pinned against a local fake SOCKS5 server."""

    def test_no_auth_socks5_end_to_end_ok(self):
        with _socks5_proxy(auth=None) as (port, seen):
            ok, detail = check_proxy_reachable(f"socks5://127.0.0.1:{port}", timeout=5.0)
        assert ok is True
        assert "HTTP 204" in detail
        assert seen["greeting"] == b"\x05\x02\x00\x02"  # offered no-auth + user/pass

    def test_correct_credentials_accepted_end_to_end(self):
        with _socks5_proxy(auth=("proxyuser", "proxypass")) as (port, seen):
            ok, detail = check_proxy_reachable(
                f"socks5://proxyuser:proxypass@127.0.0.1:{port}", timeout=5.0
            )
        assert ok is True
        assert "HTTP 204" in detail
        # The tunnel request the server saw is the generate_204 GET.
        assert b"GET /generate_204 HTTP/1.1" in seen["request"]
        assert seen["user"] == "proxyuser" and seen["password"] == "proxypass"

    def test_wrong_credentials_rejected(self):
        with _socks5_proxy(auth=("rightuser", "rightpass")) as (port, _):
            ok, detail = check_proxy_reachable(
                f"socks5://wronguser:wrongpass@127.0.0.1:{port}", timeout=5.0
            )
        assert ok is False
        assert "SOCKS5 authentication failed" in detail

    def test_auth_required_but_url_has_no_credentials(self):
        with _socks5_proxy(auth=("user", "pass")) as (port, _):
            ok, detail = check_proxy_reachable(f"socks5://127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "requires username/password" in detail

    def test_connect_refusal_reported_with_rfc1928_reason(self):
        with _socks5_proxy(auth=None, connect_reply=2) as (port, _):
            ok, detail = check_proxy_reachable(f"socks5://127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "connection not allowed by ruleset" in detail

    def test_http_server_is_recognized_as_not_socks5(self):
        # Point the socks5 probe at a plain HTTP fake: the greeting reply
        # is "HT…" — not a SOCKS5 version byte — and the verdict must say
        # so instead of hanging or raising.
        with _local_proxy(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n") as port:
            ok, detail = check_proxy_reachable(f"socks5://127.0.0.1:{port}", timeout=5.0)
        assert ok is False
        assert "not a SOCKS5 proxy" in detail
