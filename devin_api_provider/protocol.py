"""Devin (Teams) cloud wire protocol — pure stdlib.

Implements DESIGN.md "Verified wire recipe": protobuf codec, Connect-RPC envelopes over
TLS, the GetCliModelConfigs catalog and the GetChatMessage stream. Nothing here prints
or logs. Every error text passes through _redact(), so the session token never leaks.
"""

import json
import os
import re
import socket
import ssl
import struct
import threading
import tomllib
import uuid
from typing import Any, Iterable, Iterator

HOST = "server.codeium.com"  # DESIGN § Transport
PORT = 443
GET_CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
CATALOG_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"
CLIENT_NAME = "chisel"  # DESIGN § ClientMetadata
CLIENT_VERSION = "2026.8.18"
MIN_TEMPERATURE = 0.001  # DESIGN § GetChatMessage: 0.0 makes the server answer `internal`
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 8192  # enforced server-side cap
CONTEXT_WINDOW = 128000
TOP_K = 40
TOP_P = 0.95
DEFAULT_SYSTEM_WITH_TOOLS = "You are a helpful assistant. Use the available tools when appropriate."
CREDENTIALS_PATH = os.path.expanduser("~/.local/share/devin/credentials.toml")  # DESIGN § Credential
CREDENTIAL_KEY = "windsurf_api_key"
PROVIDER_NAMES = {1: "cognition", 2: "openai", 3: "anthropic", 4: "google", 5: "xai",
                  6: "deepseek", 7: "moonshot", 9: "zhipu", 11: "nvidia"}  # DESIGN § Catalog

_MCP_GATE_MARKER = "unable to process request due to an mcp configuration issue"
_POLICY_MARKERS = ("content policy", "usage policy", "safety policy", "content filter")
_AUTH_MARKERS = ("invalid api key", "unauthenticated", "unauthorized", "expired", "invalid token")
_RATE_CODES = frozenset({"rate_limit", "rate_limited", "resource_exhausted"})
_SCHEMA_DATA_KEYS = frozenset({"default", "const", "enum", "examples"})  # literal JSON, not schemas
_SCHEMA_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})
_CHUNK_SIZE_RE = re.compile(rb"([0-9A-Fa-f]+)[^\r\n]*\r?\n")
_MAX_ERROR_TEXT = 500
_MAX_CHUNK_LINE = 1024  # longest chunk-size or chunk-terminator line the decoder accepts
_MAX_HEAD_BYTES = 65536  # cap on the HTTP response head (status line and headers)
_CH_SIZE, _CH_DATA, _CH_EOL, _CH_DONE = range(4)  # _ChunkedDecoder states


class DevinError(RuntimeError):
    """Failure carrying a Connect-style `code` and a suggested HTTP `status`."""

    def __init__(self, message: str, code: str = "upstream_error", status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---- credentials (DESIGN § Credential) --------------------------------------

def find_credential(env: dict | None = None) -> tuple[str, str]:
    """Return (token, source). Source is an env var name or 'credentials.toml'. The token is never logged."""
    source_env = os.environ if env is None else env
    for name in ("DEVIN_CONNECT_TOKEN", "WINDSURF_API_KEY"):
        value = (source_env.get(name) or "").strip()
        if value:
            return value, name
    file_token = _read_credentials_file(CREDENTIALS_PATH)
    if file_token:
        return file_token, "credentials.toml"
    raise DevinError("no credential found — run `devin auth login` or set DEVIN_CONNECT_TOKEN",
                     "unauthenticated", 401)


def get_token(env: dict | None = None) -> str:
    """DEVIN_CONNECT_TOKEN > WINDSURF_API_KEY > credentials.toml."""
    return find_credential(env)[0]


def _read_credentials_file(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError):
        raise DevinError(f"credentials file {path} is unreadable — run `devin auth login`",
                         "unauthenticated", 401) from None
    value = data.get(CREDENTIAL_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---- protobuf codec (DESIGN § Verified wire recipe) -------------------------

def _enc_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError("truncated or overlong varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _tag(field: int, wire: int) -> bytes:
    return _enc_varint((field << 3) | wire)


def wvar(field: int, value: int) -> bytes:
    return _tag(field, 0) + _enc_varint(int(value))


def wfix64(field: int, value: float) -> bytes:
    return _tag(field, 1) + struct.pack("<d", float(value))


def wmsg(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _enc_varint(len(payload)) + payload


def wstr(field: int, text: str) -> bytes:
    return wmsg(field, text.encode("utf-8"))


def _slice(buf: bytes, pos: int, length: int) -> tuple[bytes, int]:
    end = pos + length
    if end > len(buf):
        raise ValueError("truncated field")
    return buf[pos:end], end


def parse_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """Decode to (field, wire_type, value): varint→int, length-delimited/fixed→bytes. Raises ValueError."""
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 0x7
        if number == 0:
            raise ValueError("field number 0")
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 1:
            value, pos = _slice(buf, pos, 8)
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            value, pos = _slice(buf, pos, length)
        elif wire == 5:
            value, pos = _slice(buf, pos, 4)
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _sub(value: bytes) -> list[tuple[int, int, Any]]:
    try:
        return parse_fields(value)
    except ValueError:
        raise DevinError("malformed upstream protobuf", "upstream_error", 502) from None


def _first_str(fields: list[tuple[int, int, Any]], number: int) -> str | None:
    for num, wire, value in fields:
        if num == number and wire == 2:
            return value.decode("utf-8", "replace")
    return None


def _first_int(fields: list[tuple[int, int, Any]], number: int) -> int | None:
    for num, wire, value in fields:
        if num == number and wire == 0:
            return value
    return None


def _client_metadata(token: str) -> bytes:
    """ClientMetadata (DESIGN § ClientMetadata). The token appears exactly once (#3)."""
    return (wstr(1, CLIENT_NAME) + wstr(2, CLIENT_VERSION) + wstr(3, token) + wstr(4, "en")
            + wstr(5, "macos") + wstr(7, CLIENT_VERSION) + wstr(12, CLIENT_NAME)
            + wstr(31, os.urandom(366).hex()))  # fingerprint: exactly 732 hex chars


# ---- Connect envelopes and HTTP (DESIGN § Transport) ------------------------

def _envelope(proto: bytes) -> bytes:
    """Request envelope: flag 0x00 + BE32 length. UNCOMPRESSED ONLY (gzip → server `internal`)."""
    return b"\x00" + struct.pack(">I", len(proto)) + proto


class _FrameReader:
    """Incremental decoder for Connect response envelopes (DESIGN § envelope framing).

    feed() takes byte pieces of any size and yields (flag, payload) for each frame as soon as the
    frame is complete. partial_error() names a truncated tail once the input is exhausted.
    """

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, data: bytes) -> Iterator[tuple[int, bytes]]:
        self._pending += data
        while len(self._pending) >= 5:
            (length,) = struct.unpack(">I", self._pending[1:5])
            if len(self._pending) < 5 + length:
                return
            flag = self._pending[0]
            payload = bytes(self._pending[5:5 + length])
            del self._pending[:5 + length]
            if flag & 0x01:
                raise DevinError("compressed upstream frames are not supported", "upstream_error", 502)
            yield flag, payload

    def partial_error(self) -> str | None:
        """Name the truncated tail left at end of input, or None when input ended on a frame boundary."""
        if not self._pending:
            return None
        return "truncated envelope header" if len(self._pending) < 5 else "truncated envelope payload"


def _iter_frames(body: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (flag, payload) for each frame of a complete response body (catalog path)."""
    reader = _FrameReader()
    yield from reader.feed(body)
    error = reader.partial_error()
    if error is not None:
        raise DevinError(error, "upstream_error", 502)


def _check_token(token: str) -> None:
    if not token or any(ch.isspace() or not 33 <= ord(ch) <= 126 for ch in token):
        raise DevinError("credential has invalid characters — re-run `devin auth login`",
                         "unauthenticated", 401)


def _build_http_request(path: str, token: str, body: bytes, content_type: str) -> bytes:
    """Authorization is `Basic {token}-{token}`: the doubling is load-bearing (DESIGN § Transport)."""
    _check_token(token)
    head = (f"POST {path} HTTP/1.1\r\n"
            f"Host: {HOST}\r\n"
            f"Content-Type: {content_type}\r\n"
            "Connect-Protocol-Version: 1\r\n"
            f"Content-Length: {len(body)}\r\n"
            "User-Agent: connect-es/2.0.0\r\n"
            f"Authorization: Basic {token}-{token}\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n")
    return head.encode("ascii") + body


class _TransportError(DevinError):
    """Socket or framing failure while reading the response: timeout, reset, or bad chunk framing.

    _chat_events swallows it only after the stream has completed; before completion it propagates.
    """


def _connect(timeout: float) -> Any:
    """Open a TLS connection to the Devin upstream host."""
    try:
        raw_sock = socket.create_connection((HOST, PORT), timeout=timeout)
    except TimeoutError:
        raise DevinError("timeout connecting to the Devin upstream", "timeout", 504) from None
    except OSError:
        raise DevinError("cannot reach the Devin upstream host", "unavailable", 503) from None
    try:
        return ssl.create_default_context().wrap_socket(raw_sock, server_hostname=HOST)
    except OSError:
        raw_sock.close()
        raise DevinError("TLS handshake with the Devin upstream failed", "unavailable", 503) from None


def _send(sock: Any, request: bytes) -> None:
    try:
        sock.sendall(request)
    except TimeoutError:
        raise _TransportError("timeout sending to the Devin upstream", "timeout", 504) from None
    except OSError:
        raise _TransportError("connection to the Devin upstream dropped while sending",
                              "unavailable", 503) from None


def _recv(sock: Any) -> bytes:
    """One socket read. b'' means EOF; requests send Connection: close, so EOF ends the response."""
    try:
        return sock.recv(65536)
    except TimeoutError:
        raise _TransportError("timeout waiting for the Devin upstream response", "timeout", 504) from None
    except OSError:
        raise _TransportError("connection to the Devin upstream dropped while reading",
                              "unavailable", 503) from None


def _iter_recv(sock: Any) -> Iterator[bytes]:
    """Yield raw response pieces as they arrive, until EOF."""
    while True:
        data = _recv(sock)
        if not data:
            return
        yield data


def _exchange(request: bytes, timeout: float) -> bytes:
    """Send one request and return the whole raw response (the whole-body catalog path)."""
    sock = _connect(timeout)
    try:
        _send(sock, request)
        return b"".join(_iter_recv(sock))
    finally:
        sock.close()


class _ChunkedDecoder:
    """Incremental HTTP/1.1 chunked transfer-coding decoder (LF or CRLF line endings).

    feed() never raises: bytes decoded before a framing error are returned, the error is recorded,
    and check() raises it. finish() records a body that ended before its terminal chunk.
    """

    def __init__(self) -> None:
        self._state = _CH_SIZE
        self._line = bytearray()
        self._remaining = 0
        self._error: str | None = None

    @property
    def done(self) -> bool:
        """True once the terminal zero-size chunk is read; any later bytes are ignored."""
        return self._state == _CH_DONE

    @property
    def failed(self) -> bool:
        return self._error is not None

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        pos = 0
        while pos < len(data) and self._state != _CH_DONE and self._error is None:
            if self._state == _CH_DATA:
                take = min(self._remaining, len(data) - pos)
                out += data[pos:pos + take]
                pos += take
                self._remaining -= take
                if self._remaining == 0:
                    self._state = _CH_EOL
                continue
            end = data.find(b"\n", pos)
            stop = len(data) if end < 0 else end + 1
            if len(self._line) + stop - pos > _MAX_CHUNK_LINE:
                self._error = "malformed chunked upstream response"
                break
            self._line += data[pos:stop]
            pos = stop
            if end < 0:
                break
            line = bytes(self._line)
            self._line.clear()
            self._on_line(line)
        return bytes(out)

    def _on_line(self, line: bytes) -> None:
        if self._state == _CH_EOL:
            if line in (b"\r\n", b"\n"):
                self._state = _CH_SIZE
            else:
                self._error = "malformed chunked upstream response"
            return
        match = _CHUNK_SIZE_RE.match(line)
        if match is None:
            self._error = "malformed chunked upstream response"
            return
        size = int(match.group(1), 16)
        if size == 0:
            self._state = _CH_DONE
        else:
            self._remaining = size
            self._state = _CH_DATA

    def finish(self) -> None:
        """Call at EOF. A body that never reached its terminal chunk is an error."""
        if self._state != _CH_DONE and self._error is None:
            self._error = ("truncated chunked upstream response" if self._state == _CH_DATA
                           else "malformed chunked upstream response")

    def check(self) -> None:
        if self._error is not None:
            raise _TransportError(self._error, "upstream_error", 502)


def _dechunk(data: bytes) -> bytes:
    """Decode a complete chunked body in one shot (whole-body path)."""
    decoder = _ChunkedDecoder()
    out = decoder.feed(data)
    decoder.finish()
    decoder.check()
    return out


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def _classify_error(code: str, message: str, http_status: int = 0, token: str = "") -> DevinError:
    """Relay-binding order: MCP-gate → content policy → auth → rate limit → internal → other codes."""
    c = (code or "").strip().lower()
    m = (message or "").lower()
    if _MCP_GATE_MARKER in m:
        kind, status = "upstream_error", 502
    elif any(x in m for x in _POLICY_MARKERS) or ("policy" in m and "refus" in m):
        kind, status = "invalid_request", 400
    elif (c == "unauthenticated" or http_status == 401
          or any(x in m for x in _AUTH_MARKERS)):
        kind, status = "unauthenticated", 401
    elif c in _RATE_CODES or http_status == 429 or "rate limit" in m or "rate_limit" in m:
        kind, status = "rate_limited", 429
    elif c in ("internal", "upstream_internal") or "an internal error occurred" in m:
        kind, status = "upstream_internal", 502
    elif c == "invalid_argument":
        kind, status = "invalid_request", 400
    elif c == "unavailable":
        kind, status = "unavailable", 503
    elif c == "deadline_exceeded":
        kind, status = "timeout", 504
    else:
        kind, status = "upstream_error", 502
    text = _redact((message or c or "upstream error").strip(), token)[:_MAX_ERROR_TEXT]
    return DevinError(text, kind, status)


def _error_from_body(body: bytes) -> tuple[str, str]:
    """Extract (code, message) from `{"code","message"}` or `{"error":{...}}`; else raw text."""
    text = body.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except ValueError:
        return "", text[:300].strip()
    if isinstance(obj, dict):
        err = obj.get("error", obj)
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
    return "", text[:300].strip()


def _parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    """Return the status code and lower-cased headers of an HTTP/1.1 response head."""
    lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        raise DevinError("malformed upstream HTTP status line", "upstream_error", 502) from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


def _is_chunked(headers: dict[str, str]) -> bool:
    return "chunked" in headers.get("transfer-encoding", "").lower()


def _http_error(status: int, body: bytes, token: str) -> DevinError:
    code, message = _error_from_body(body)
    return _classify_error(code, message or f"upstream returned HTTP {status}", status, token)


def _parse_http_response(raw: bytes, token: str) -> bytes:
    """Parse a fully buffered HTTP response and return the de-chunked body (200 only)."""
    split = raw.find(b"\r\n\r\n")
    if split < 0:
        raise DevinError("malformed upstream HTTP response", "upstream_error", 502)
    status, headers = _parse_head(raw[:split])
    body = raw[split + 4:]
    if _is_chunked(headers):
        body = _dechunk(body)
    if status != 200:
        raise _http_error(status, body, token)
    return body


def _post_connect(path: str, token: str, proto: bytes, *, timeout: float,
                  content_type: str = "application/connect+proto", connect_rpc: bool = True) -> bytes:
    """POST one request and return the de-chunked response body. Enveloped unless connect_rpc=False."""
    payload = _envelope(proto) if connect_rpc else proto
    return _parse_http_response(_exchange(_build_http_request(path, token, payload, content_type), timeout), token)


def _read_head(sock: Any) -> tuple[bytes, bytes]:
    """Read the response head. Returns (head without its blank line, body bytes already received)."""
    buf = bytearray()
    while True:
        split = buf.find(b"\r\n\r\n")
        if split >= 0:
            if split > _MAX_HEAD_BYTES:
                raise DevinError("malformed upstream HTTP response", "upstream_error", 502)
            return bytes(buf[:split]), bytes(buf[split + 4:])
        if len(buf) > _MAX_HEAD_BYTES:
            raise DevinError("malformed upstream HTTP response", "upstream_error", 502)
        data = _recv(sock)
        if not data:
            raise DevinError("malformed upstream HTTP response", "upstream_error", 502)
        buf += data


def _post_connect_stream(path: str, token: str, proto: bytes, *, timeout: float,
                         content_type: str = "application/connect+proto",
                         connect_rpc: bool = True) -> Iterator[bytes]:
    """Yield the de-chunked response body as it arrives (same wire as _post_connect).

    A non-200 response is read whole and raised before any piece is yielded. A truncated or malformed
    chunked body raises _TransportError when reached. The socket is closed when the generator finishes,
    raises, or is closed by the consumer.
    """
    payload = _envelope(proto) if connect_rpc else proto
    request = _build_http_request(path, token, payload, content_type)
    sock = _connect(timeout)
    try:
        _send(sock, request)
        head, rest = _read_head(sock)
        status, headers = _parse_head(head)
        chunked = _is_chunked(headers)
        if status != 200:
            body = rest + b"".join(_iter_recv(sock))
            if chunked:
                body = _dechunk(body)
            raise _http_error(status, body, token)
        if not chunked:
            if rest:
                yield rest
            yield from _iter_recv(sock)
            return
        decoder = _ChunkedDecoder()
        piece = rest
        while True:
            out = decoder.feed(piece)
            if out:
                yield out
            if decoder.done or decoder.failed:
                break
            piece = _recv(sock)
            if not piece:
                break
        decoder.finish()
        decoder.check()
    finally:
        sock.close()


def _json_or_none(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        return None


def _control_frame(flag: int, payload: bytes, token: str) -> bool:
    """True for a JSON control frame (end-of-stream `{}`). Raises the mapped DevinError for in-body errors."""
    if flag & 0x02:
        obj = _json_or_none(payload)
        if obj is None:
            raise DevinError("malformed end-of-stream frame", "upstream_error", 502)
    elif payload[:1] == b"{":
        obj = _json_or_none(payload)
        if not isinstance(obj, dict) or not ("error" in obj or obj == {}):
            return False
    else:
        return False
    if isinstance(obj, dict) and obj.get("error") is not None:
        err = obj["error"]
        if isinstance(err, dict):
            raise _classify_error(str(err.get("code") or ""), str(err.get("message") or ""), 0, token)
        raise _classify_error("", str(err), 0, token)
    return True


# ---- catalog (DESIGN § GetCliModelConfigs) ----------------------------------

_MODELS_CACHE: list[dict] | None = None
_MODELS_LOCK = threading.Lock()


def _parse_model_entry(value: bytes) -> dict | None:
    fields = parse_fields(value)
    selector = _first_str(fields, 22)
    if not selector:
        return None
    provider_no = _first_int(fields, 10)
    provider = "unknown" if provider_no is None else PROVIDER_NAMES.get(provider_no, f"provider-{provider_no}")
    return {"id": selector, "label": _first_str(fields, 1) or selector, "provider": provider,
            "enabled": not _first_int(fields, 4)}


def _is_envelope_framed(body: bytes) -> bool:
    """True when body is Connect envelope frames (flag 0x00/0x02 + BE32 length) that consume it exactly."""
    if not body or body[0] not in (0x00, 0x02):
        return False
    pos = 0
    while pos < len(body):
        if pos + 5 > len(body):
            return False
        (length,) = struct.unpack(">I", body[pos + 1:pos + 5])
        pos += 5 + length
    return pos == len(body)


def _catalog_frame_fields(body: bytes, token: str) -> list[tuple[int, int, object]]:
    """Fields from envelope-framed catalog data frames; control frames are error-checked and skipped."""
    try:
        frames = list(_iter_frames(body))
    except DevinError:
        raise DevinError("catalog parse failed", "upstream_error", 502) from None
    fields: list[tuple[int, int, object]] = []
    for flag, payload in frames:
        if _control_frame(flag, payload, token):
            continue
        try:
            fields.extend(parse_fields(payload))
        except ValueError:
            continue
    return fields


def _catalog_entries(fields: list[tuple[int, int, object]]) -> list[dict]:
    """ClientModelConfig submessages (repeated #1 or #2), deduped by selector; bad entries are skipped."""
    models: list[dict] = []
    seen: set[str] = set()
    for number, wire, value in fields:
        if number not in (1, 2) or wire != 2:
            continue
        try:
            entry = _parse_model_entry(value)
        except ValueError:
            continue  # one bad entry must not sink the catalog
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            models.append(entry)
    return models


def _parse_catalog(body: bytes, token: str) -> list[dict]:
    """GetCliModelConfigs body. Live shape is raw protobuf: repeated top-level #1 ClientModelConfig
    (top-level #3/#4 ignored). Connect envelope frames are accepted as a fallback; a body starting
    with '{' is a Connect JSON error and is mapped to DevinError."""
    if body[:1] == b"{":
        try:
            _control_frame(0x02, body, token)  # raises the mapped error when the JSON carries one
        except ValueError:
            pass
        raise DevinError("catalog parse failed", "upstream_error", 502)
    if _is_envelope_framed(body):
        fields = _catalog_frame_fields(body, token)
    else:
        try:
            fields = parse_fields(body)
        except ValueError:
            raise DevinError("catalog parse failed", "upstream_error", 502) from None
    models = _catalog_entries(fields)
    if not models:
        raise DevinError("catalog parse failed", "upstream_error", 502)
    return models


def list_models(token: str | None = None, timeout: float = 30.0, refresh: bool = False) -> list[dict]:
    """Live catalog as [{id, label, provider, enabled}]. Memoized in-process unless refresh=True."""
    global _MODELS_CACHE
    with _MODELS_LOCK:
        if _MODELS_CACHE is not None and not refresh:
            return [dict(item) for item in _MODELS_CACHE]
    tok = token or get_token()
    raw = _post_connect(CATALOG_PATH, tok, wmsg(1, _client_metadata(tok)), timeout=timeout,
                        content_type="application/proto", connect_rpc=False)
    models = _parse_catalog(raw, tok)
    with _MODELS_LOCK:
        _MODELS_CACHE = models
    return [dict(item) for item in models]


# ---- chat request (DESIGN § GetChatMessage request) -------------------------

def _resolve_max_tokens(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_TOKENS
    try:
        cap = int(value)
    except (TypeError, ValueError):
        raise DevinError("max_tokens must be an integer", "invalid_request", 400) from None
    if cap < 1:
        raise DevinError("max_tokens must be >= 1", "invalid_request", 400)
    return cap


def _resolve_temperature(value: Any) -> float:
    """Clamp to MIN_TEMPERATURE: 0.0 makes the server answer `internal` (DESIGN)."""
    if value is None:
        return DEFAULT_TEMPERATURE
    try:
        temp = float(value)
    except (TypeError, ValueError):
        raise DevinError("temperature must be a number", "invalid_request", 400) from None
    if temp != temp:  # NaN
        raise DevinError("temperature must be a number", "invalid_request", 400)
    return max(temp, MIN_TEMPERATURE)


def _strip_schema_descriptions(value: Any, in_properties: bool = False) -> Any:
    """Recursively drop `description` annotations (MCP-gate). A property NAMED `description` is kept."""
    if isinstance(value, list):
        return [_strip_schema_descriptions(item) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, child in value.items():
        if in_properties:  # keys are property names; values are schemas
            out[key] = _strip_schema_descriptions(child)
        elif key == "description":  # annotation
            continue
        elif key in _SCHEMA_DATA_KEYS:  # literal data, copied verbatim
            out[key] = child
        else:
            out[key] = _strip_schema_descriptions(child, in_properties=key in _SCHEMA_NAME_MAPS)
    return out


def _normalize_schema(params: Any) -> dict:
    schema = {k: v for k, v in params.items() if k != "$schema"} if isinstance(params, dict) else {}
    schema["type"] = "object"
    props = schema.get("properties")
    schema["properties"] = props if isinstance(props, dict) else {}
    required = schema.get("required")
    schema["required"] = [r for r in required if isinstance(r, str)] if isinstance(required, list) else []
    return _strip_schema_descriptions(schema)


def _normalize_tools(tools: Any) -> list[tuple[str, dict]]:
    specs: list[tuple[str, dict]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            raise DevinError("each tool must be an object", "invalid_request", 400)
        fn = tool["function"] if isinstance(tool.get("function"), dict) else tool
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            raise DevinError("tool definition is missing a name", "invalid_request", 400)
        specs.append((name.strip(), _normalize_schema(fn.get("parameters"))))
    return specs


def _tool_def(name: str, schema: dict) -> bytes:
    """ToolDef (#10). MCP-gate: #2 description := tool name (DESIGN § MCP-gate)."""
    return wstr(1, name) + wstr(2, name) + wstr(3, json.dumps(schema, separators=(",", ":"), ensure_ascii=False))


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif (isinstance(part, dict) and part.get("type", "text") in ("text", "input_text")
                  and isinstance(part.get("text"), str)):
                pieces.append(part["text"])
        return "\n".join(piece for piece in pieces if piece)
    return json.dumps(content, ensure_ascii=False)


def _content_images(content: Any) -> list[tuple[str, str]]:
    """image_url data: URLs → (base64, mime) for ImageData (DESIGN § ChatMessage #10)."""
    images: list[tuple[str, str]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else ref
        if not isinstance(url, str) or not url.startswith("data:"):
            raise DevinError("only base64 data: image URLs are supported", "invalid_request", 400)
        header, sep, data = url[5:].partition(",")
        if not sep or ";base64" not in header:
            raise DevinError("image data URLs must be base64-encoded", "invalid_request", 400)
        images.append((data, header.split(";", 1)[0] or "image/png"))
    return images


def _normalize_call(call: Any) -> tuple[str, str, str]:
    """Assistant tool_call → (id, name, arguments JSON string)."""
    if not isinstance(call, dict):
        raise DevinError("each tool call must be an object", "invalid_request", 400)
    fn = call["function"] if isinstance(call.get("function"), dict) else call
    name = str(fn.get("name") or "")
    if not name:
        raise DevinError("assistant tool call is missing a function name", "invalid_request", 400)
    raw_args = fn.get("arguments")
    if raw_args is None or raw_args == "":
        args = "{}"
    elif isinstance(raw_args, str):
        args = raw_args
    else:
        args = json.dumps(raw_args, ensure_ascii=False)
    return str(call.get("id") or f"call_{uuid.uuid4().hex[:24]}"), name, args


def _item(source: int, text: str = "", *, images: list | None = None,
          tool_call_id: str | None = None, call: tuple | None = None) -> dict:
    return {"source": source, "text": text, "images": images or [],
            "tool_call_id": tool_call_id, "call": call}


def _mergeable(item: dict) -> bool:
    return not item["images"] and item["call"] is None and item["tool_call_id"] is None


def _encode_item(item: dict) -> bytes:
    """ChatMessage (DESIGN § GetChatMessage request, #3 rep). One ChatMessage per tool call."""
    head = wstr(1, str(uuid.uuid4()))
    if item["call"] is not None:
        call_id, name, args = item["call"]
        return head + wvar(2, 2) + wmsg(6, wstr(1, call_id) + wstr(2, name) + wstr(3, args))
    out = head + wvar(2, item["source"])
    if item["text"]:
        out += wstr(3, item["text"])
    if item["tool_call_id"]:
        out += wstr(7, item["tool_call_id"])
    for data, mime in item["images"]:
        out += wmsg(10, wstr(1, data) + wstr(2, mime))
    return out


def _encode_messages(messages: Any) -> tuple[str, list[bytes]]:
    """OpenAI messages → (hoisted system text, ChatMessage bytes). Coalesces same-source text runs."""
    if not isinstance(messages, list) or not messages:
        raise DevinError("messages must be a non-empty list", "invalid_request", 400)
    system_texts: list[str] = []
    items: list[dict] = []
    call_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            raise DevinError("each message must be an object", "invalid_request", 400)
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _content_text(content)
            if text:
                system_texts.append(text)
        elif role == "user":
            text, images = _content_text(content), _content_images(content)
            if text or images:
                items.append(_item(1, text, images=images))
        elif role == "assistant":
            text = _content_text(content)
            calls = [_normalize_call(c) for c in (msg.get("tool_calls") or [])]
            call_ids.update(call[0] for call in calls)
            if text:
                items.append(_item(2, text))
            items.extend(_item(2, call=call) for call in calls)  # empty turns without calls: skipped
        elif role == "tool":
            tool_id = str(msg.get("tool_call_id") or "")
            body = _content_text(content)
            label = f"[tool result for {tool_id}]: {body}" if tool_id else f"[tool result]: {body}"
            if tool_id and tool_id in call_ids:  # native round-trip
                items.append(_item(4, label, tool_call_id=tool_id))
            else:  # folds into user text when not a native round-trip
                items.append(_item(1, label))
        else:
            raise DevinError(f"unsupported message role {role!r}", "invalid_request", 400)
    merged: list[dict] = []
    for item in items:
        prev = merged[-1] if merged else None
        if prev and _mergeable(prev) and _mergeable(item) and prev["source"] == item["source"]:
            prev["text"] = f"{prev['text']}\n\n{item['text']}"
        else:
            merged.append(item)
    return "\n".join(system_texts), [_encode_item(item) for item in merged]


def _build_chat_request(token: str, *, messages: Any, model: str, max_tokens: Any,
                        temperature: Any, tool_specs: list[tuple[str, dict]]) -> bytes:
    """GetChatMessage request proto (DESIGN § GetChatMessage request)."""
    if not isinstance(model, str) or not model.strip():
        raise DevinError("model selector is required", "invalid_request", 400)
    system_text, chat_messages = _encode_messages(messages)
    if not system_text and tool_specs:
        system_text = DEFAULT_SYSTEM_WITH_TOOLS  # Claude-family rejects empty system + tools
    completion = (wvar(1, 1) + wvar(2, _resolve_max_tokens(max_tokens)) + wvar(3, CONTEXT_WINDOW)
                  + wfix64(5, _resolve_temperature(temperature)) + wvar(7, TOP_K) + wfix64(8, TOP_P))
    parts = [wmsg(1, _client_metadata(token))]
    if system_text:
        parts.append(wstr(2, system_text))
    parts.extend(wmsg(3, msg) for msg in chat_messages)
    parts.append(wvar(7, 5))
    parts.append(wmsg(8, completion))
    parts.extend(wmsg(10, _tool_def(name, schema)) for name, schema in tool_specs)
    parts.append(wmsg(15, wstr(1, str(uuid.uuid4())) + wvar(2, 1) + wvar(3, 4)))  # ModelConfig
    parts.append(wstr(16, str(uuid.uuid4())))
    parts.append(wvar(20, 1))
    parts.append(wstr(21, model))
    return b"".join(parts)


# ---- chat response (DESIGN § GetChatMessage response) -----------------------

def _accumulate_call(value: bytes, calls: list[dict], by_id: dict[str, dict]) -> None:
    """ChatToolCall (#6). No name on the wire; id-less fragments extend the most recent call."""
    fields = _sub(value)
    call_id = _first_str(fields, 1) or ""
    call = by_id.get(call_id) if call_id else (calls[-1] if calls else None)
    if call is None:
        call_id = call_id or f"call_{uuid.uuid4().hex[:24]}"
        call = {"id": call_id, "args": "", "invalid": ""}
        calls.append(call)
        by_id[call_id] = call
    call["args"] += _first_str(fields, 3) or ""
    call["invalid"] += _first_str(fields, 4) or ""


def _accumulate_usage(value: bytes, usage: dict[str, Any]) -> None:
    """metadata (#7): 2 prompt tokens, 3 completion tokens, 9 model."""
    fields = _sub(value)
    usage["seen"] = True
    prompt = _first_int(fields, 2)
    completion = _first_int(fields, 3)
    if prompt is not None:
        usage["prompt_tokens"] = prompt
    if completion is not None:
        usage["completion_tokens"] = completion
    usage["model"] = _first_str(fields, 9) or usage["model"]


def _args_fit(args: dict, schema: dict) -> bool:
    props = schema.get("properties") or {}
    if any(key not in args for key in schema.get("required") or []):
        return False
    return not props or set(args) <= set(props)


def _resolve_tool_name(args: str, tool_specs: list[tuple[str, dict]]) -> str:
    """Reverse-lookup only (DESIGN:111): lone tool, else unique schema fit, else 'unknown'."""
    if len(tool_specs) == 1:
        return tool_specs[0][0]
    try:
        parsed = json.loads(args)
    except ValueError:
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    fits = [tool_name for tool_name, schema in tool_specs if _args_fit(parsed, schema)]
    return fits[0] if len(fits) == 1 else "unknown"


def _final_arguments(call: dict) -> str:
    args = call["args"]
    if not args.strip():
        return "{}"
    try:
        json.loads(args)
        return args
    except ValueError:
        return "{}" if call["invalid"] else args


def _chat_events(pieces: Iterable[bytes], *, token: str, model: str,
                 tool_specs: list[tuple[str, dict]]) -> Iterator[dict]:
    """Decode GetChatMessage byte pieces into events as they arrive (DESIGN § GetChatMessage response).

    Deltas and reasoning stream live. Completion is the end-of-stream frame or a finish signal (#5).
    An end frame stops reading at once. After a finish signal the stream is drained to EOF so usage
    and tool calls that follow are kept; a transport error during that drain, or a truncated trailing
    frame, is a clean end. Before completion a transport error propagates and a truncated tail is an
    error.
    """
    reader = _FrameReader()
    finish: int | None = None
    completed = False
    ended = False
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    usage: dict[str, Any] = {"seen": False, "prompt_tokens": 0, "completion_tokens": 0, "model": ""}
    try:
        for piece in pieces:
            for flag, payload in reader.feed(piece):
                if _control_frame(flag, payload, token):
                    completed = ended = True
                    break
                for number, wire, value in _sub(payload):
                    if wire == 2 and number == 3:  # content delta
                        text = value.decode("utf-8", "replace")
                        if text:
                            yield {"type": "delta", "content": text}
                    elif wire == 2 and number == 9:  # reasoning delta
                        text = value.decode("utf-8", "replace")
                        if text:
                            yield {"type": "reasoning", "content": text}
                    elif wire == 0 and number == 5:  # finish signal
                        finish = value
                        completed = True
                    elif wire == 2 and number == 6:  # ChatToolCall
                        _accumulate_call(value, calls, by_id)
                    elif wire == 2 and number == 7:  # metadata
                        _accumulate_usage(value, usage)
            if ended:
                break
    except _TransportError:
        if not completed:
            raise
    finally:
        close = getattr(pieces, "close", None)
        if close is not None:
            close()
    if not completed:
        raise DevinError(reader.partial_error() or "upstream stream ended before completion",
                         "upstream_error", 502)
    for index, call in enumerate(calls):
        yield {"type": "tool_call", "index": index, "id": call["id"],
               "name": _resolve_tool_name(call["args"], tool_specs),
               "arguments": _final_arguments(call)}
    # DESIGN § GetChatMessage response: 2 -> stop, 3 -> length
    yield {"type": "finish", "reason": "length" if finish == 3 else "stop"}
    if usage["seen"]:
        yield {"type": "usage", "prompt_tokens": int(usage["prompt_tokens"]),
               "completion_tokens": int(usage["completion_tokens"]), "model": usage["model"] or model}


def stream_chat(token: str, *, messages: list[dict], model: str, max_tokens: int | None = None,
                temperature: float | None = None, tools: list[dict] | None = None,
                timeout: float = 600.0) -> Iterator[dict]:
    """Yield events: delta | reasoning | tool_call | finish | usage (DESIGN § protocol.py contract).

    The response is consumed incrementally; completion and error rules live in _chat_events.
    """
    cap = _resolve_max_tokens(max_tokens)
    tool_specs = _normalize_tools(tools)
    request = _build_chat_request(token, messages=messages, model=model, max_tokens=cap,
                                  temperature=temperature, tool_specs=tool_specs)
    pieces = _post_connect_stream(GET_CHAT_PATH, token, request, timeout=timeout,
                                  content_type="application/connect+proto", connect_rpc=True)
    yield from _chat_events(pieces, token=token, model=model, tool_specs=tool_specs)
