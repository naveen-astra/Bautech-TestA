"""Serve email OTP codes to Maestro flows, so login works without Firebase's
help.

The clean fix for unattended login is Firebase test phone numbers with a fixed
code - see README, "What is still needed". That needs one config change from
Navicon and may not happen in time. This is the fallback that needs nothing
from them: register real email addresses as the three test accounts, point
Bautech's "Sign in with Email" at them, and let this service read the code out
of the inbox.

DESIGN: THE SERVER DOES THE WAITING

Maestro's JS sandbox has no sleep or timer, so `fetch_otp.js` cannot poll in a
retry loop - it can only make one HTTP call and wait for the response. So the
waiting has to happen here: `/otp` holds the connection open, polling IMAP
internally, until a matching code arrives or its own timeout elapses. That
keeps the Maestro side to a single request instead of an ad hoc retry policy
guessed at from the flow.

The IMAP polling (`ImapOtpSource`) and the HTTP layer (`OtpRelayServer`) are
separate on purpose: the polling logic is what actually needs testing, and it
should not require a real mailbox to do that. Tests inject a fake source.

Run standalone:

    python -m sentinel.otp_relay --host 0.0.0.0 --port 8765

Then point `SENTINEL_OTP_RELAY_URL` at wherever this is reachable from the
device under test. On BrowserStack that means a public URL or a BrowserStack
Local tunnel - not localhost, which the cloud device cannot reach.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlparse

# A bare 6-digit run, not preceded/followed by another digit - avoids matching
# into a longer number (an amount, an order id) that happens to contain six
# consecutive digits.
_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

_POLL_INTERVAL_SECONDS = 2.0


class OtpRelayError(Exception):
    """The relay could not do what was asked of it."""


# --------------------------------------------------------------------------- #
# Finding the code
# --------------------------------------------------------------------------- #


class OtpSource(Protocol):
    """Where a code comes from. `ImapOtpSource` in production, a stub in tests."""

    def latest_code(self, identifier: str, *, since: float) -> str | None:
        """The newest matching code received after `since` (epoch seconds), if any."""
        ...


@dataclass
class ImapOtpSource:
    """Polls an IMAP mailbox for Bautech's OTP email.

    One source serves every persona sharing a mailbox: `identifier` narrows the
    search to the message addressed to that persona's own test account, so
    three logins in flight at once do not read each other's codes.
    """

    host: str
    username: str
    password: str
    folder: str = "INBOX"
    sender_filter: str = ""  # e.g. "noreply@naviconinfra.com"; empty = any sender
    port: int = 993

    def _connect(self) -> imaplib.IMAP4_SSL:
        connection = imaplib.IMAP4_SSL(self.host, self.port)
        connection.login(self.username, self.password)
        connection.select(self.folder)
        return connection

    def latest_code(self, identifier: str, *, since: float) -> str | None:
        since_date = time.strftime("%d-%b-%Y", time.gmtime(since))
        criteria = ["SINCE", since_date]
        if self.sender_filter:
            criteria += ["FROM", self.sender_filter]
        # IMAP SEARCH lacks free-text address matching reliable enough for a
        # bare mobile number, so identifier narrowing happens after fetch,
        # against the message's own To/subject/body - not in the query itself.

        connection = self._connect()
        try:
            status, raw_ids = connection.search(None, *criteria)
            if status != "OK":
                raise OtpRelayError(f"IMAP search failed: {status}")

            candidates: list[tuple[float, str]] = []
            for message_id in raw_ids[0].split():
                status, data = connection.fetch(message_id, "(RFC822 INTERNALDATE)")
                if status != "OK" or not data or data[0] is None:
                    continue
                raw = data[0][1] if isinstance(data[0], tuple) else b""
                message = email.message_from_bytes(raw)

                if identifier and identifier not in _all_text(message):
                    continue

                received = _received_at(connection, message_id) or since
                if received < since:
                    continue

                code = _extract_code(message)
                if code:
                    candidates.append((received, code))

            if not candidates:
                return None
            candidates.sort(key=lambda pair: pair[0])
            return candidates[-1][1]
        finally:
            try:
                connection.logout()
            except Exception:
                pass  # a failed logout must never mask the code we already have

    def wait_for_code(self, identifier: str, timeout_seconds: float) -> str | None:
        """Poll until a code arrives or the timeout elapses. Blocking."""
        deadline = time.monotonic() + timeout_seconds
        started = time.time()
        while True:
            code = self.latest_code(identifier, since=started)
            if code:
                return code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _all_text(message: email.message.Message) -> str:
    parts = [message.get("To", ""), message.get("Subject", "")]
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain":
                parts.append(_decode_payload(part))
    else:
        parts.append(_decode_payload(message))
    return " ".join(parts)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _extract_code(message: email.message.Message) -> str | None:
    match = _CODE.search(_all_text(message))
    return match.group(1) if match else None


def _received_at(connection: imaplib.IMAP4_SSL, message_id: bytes) -> float | None:
    """When the mail server received this message, as epoch seconds.

    Preferred over the message's own Date header, which is client-set and can
    be wrong or absent; INTERNALDATE is assigned by the server on arrival.
    """
    status, data = connection.fetch(message_id, "(INTERNALDATE)")
    if status != "OK" or not data or not isinstance(data[0], bytes):
        return None
    parsed = imaplib.Internaldate2tuple(data[0])
    return time.mktime(parsed) if parsed else None


# --------------------------------------------------------------------------- #
# The HTTP layer fetch_otp.js talks to
# --------------------------------------------------------------------------- #


def _make_handler(source: OtpSource, default_timeout_ms: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            print(f"[otp_relay] {self.address_string()} - {format % args}")

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            if parsed.path != "/otp":
                self._respond(404, {"error": "not found"})
                return

            params = parse_qs(parsed.query)
            identifier = (params.get("identifier") or [""])[0]
            if not identifier:
                self._respond(400, {"error": "identifier is required"})
                return

            try:
                timeout_ms = int((params.get("timeout_ms") or [str(default_timeout_ms)])[0])
            except ValueError:
                timeout_ms = default_timeout_ms

            try:
                code = source.wait_for_code(identifier, timeout_ms / 1000)
            except OtpRelayError as exc:
                self._respond(502, {"error": str(exc)})
                return

            if code is None:
                self._respond(504, {"error": f"no code arrived for {identifier!r} in time"})
                return
            self._respond(200, {"otp": code})

        def _respond(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class OtpRelayServer:
    """The HTTP surface. Thin on purpose - `wait_for_code` does the real work."""

    def __init__(self, source: OtpSource, host: str = "0.0.0.0", port: int = 8765,
                 default_timeout_ms: int = 30000) -> None:
        self.source = source
        self._httpd = ThreadingHTTPServer(
            (host, port), _make_handler(source, default_timeout_ms)
        )

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address

    def serve_forever(self) -> None:
        print(f"[otp_relay] listening on http://{self.address[0]}:{self.address[1]}/otp")
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def source_from_env() -> ImapOtpSource:
    required = ("SENTINEL_OTP_IMAP_HOST", "SENTINEL_OTP_IMAP_USER", "SENTINEL_OTP_IMAP_PASS")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise OtpRelayError(f"missing environment variable(s): {', '.join(missing)}")
    return ImapOtpSource(
        host=os.environ["SENTINEL_OTP_IMAP_HOST"],
        username=os.environ["SENTINEL_OTP_IMAP_USER"],
        password=os.environ["SENTINEL_OTP_IMAP_PASS"],
        folder=os.environ.get("SENTINEL_OTP_IMAP_FOLDER", "INBOX"),
        sender_filter=os.environ.get("SENTINEL_OTP_SENDER_FILTER", ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    try:
        source = source_from_env()
    except OtpRelayError as exc:
        print(f"error: {exc}")
        return 2

    server = OtpRelayServer(source, host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
