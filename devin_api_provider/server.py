"""OpenAI-compatible HTTP front-end for the Devin provider. Pure stdlib.

Routes:
  GET  /                      service descriptor
  GET  /healthz               liveness probe
  GET  /v1/models             enabled catalog selectors
  POST /v1/chat/completions   chat.completion JSON, or SSE when stream=true

Bearer auth is enforced only when DEVIN_PROVIDER_KEY (or key=) is set.
Tokens are never logged; each request produces one log line:
method, path, status, model, stream flag.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import sys
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import __version__

try:
    from . import protocol as _protocol
except ImportError as _exc:  # protocol.py missing, or one of its imports is unavailable
    _protocol = None
    _PROTOCOL_ERROR = _exc
else:
    _PROTOCOL_ERROR = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BODY_BYTES = 32 * 1024 * 1024

_END = object()
_ROUTES = {
    "/": frozenset({"GET"}),
    "/healthz": frozenset({"GET"}),
    "/v1/models": frozenset({"GET"}),
    "/v1/chat/completions": frozenset({"POST"}),
}
_SERVICE = {
    "service": "devin-api-provider",
    "endpoints": ["/v1/models", "/v1/chat/completions", "/healthz"],
}


class _Unavailable(Exception):
    """Stand-in so except-clauses stay valid when protocol.py cannot be imported."""


_DevinError = _protocol.DevinError if _protocol is not None else _Unavailable


class _HTTPError(Exception):
    def __init__(self, status, message, err_type="invalid_request_error", code=None, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type
        self.code = code
        self.headers = headers or {}


class _ClientGone(Exception):
    """The client disconnected while we were writing. Not a server fault."""


def _log(line: str) -> None:
    try:
        sys.stderr.write(f"[devin-provider] {line}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


def _safe(text: str, limit: int = 128) -> str:
    """Strip control characters from client-supplied values before logging."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:limit]


def _require_protocol():
    if _protocol is None:
        raise RuntimeError(
            "devin_api_provider/protocol.py could not be imported "
            f"({_PROTOCOL_ERROR}). The HTTP server needs it: restore protocol.py "
            "(and its dependencies) before starting the server."
        )
    return _protocol


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _call_id() -> str:
    return "call_" + uuid.uuid4().hex[:24]


def _error_body(message: str, err_type: str, code: str | None) -> dict:
    return {"error": {"message": message, "type": err_type, "code": code}}


def _chunk(cid, created, model, delta, finish_reason) -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage(event) -> dict:
    event = event or {}
    prompt = int(event.get("prompt_tokens") or 0)
    completion = int(event.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _finish_reason(reason, has_tool_calls: bool) -> str:
    if has_tool_calls and reason in (None, "", "stop"):
        return "tool_calls"
    return reason or "stop"


def _status_for(exc) -> int:
    status = getattr(exc, "status", 502)
    return status if isinstance(status, int) and 400 <= status <= 599 else 502


def _optional_number(body: dict, key: str):
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _HTTPError(400, f"'{key}' must be a finite number", code=f"invalid_{key}")
    return value


def _close(iterator) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        try:
            close()  # release the upstream connection when the client has gone
        except Exception:
            pass


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


class _Accumulator:
    """Folds protocol events into a non-streaming chat.completion body."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict[str, str]] = {}
        self.finish: str | None = None
        self.usage: dict | None = None

    def add(self, event: dict) -> str | None:
        kind = event.get("type")
        if kind == "delta":
            self.text.append(str(event.get("content") or ""))
        elif kind == "reasoning":
            self.reasoning.append(str(event.get("content") or ""))
        elif kind == "tool_call":
            slot = self.calls.setdefault(
                int(event.get("index") or 0), {"id": "", "name": "", "arguments": ""}
            )
            if event.get("id"):
                slot["id"] = str(event["id"])
            if event.get("name"):
                slot["name"] = str(event["name"])
            slot["arguments"] += str(event.get("arguments") or "")
        elif kind == "finish":
            self.finish = event.get("reason") or self.finish
        elif kind == "usage":
            self.usage = event
        return kind

    def completion(self, model: str) -> dict:
        content = "".join(self.text)
        reasoning = "".join(self.reasoning)
        tool_calls = [
            {
                "id": slot["id"] or _call_id(),
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
            for _, slot in sorted(self.calls.items())
        ]
        message: dict[str, Any] = {
            "role": "assistant",
            "content": content or (None if tool_calls else ""),
        }
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": _completion_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": _finish_reason(self.finish, bool(tool_calls)),
                }
            ],
            "usage": _usage(self.usage),
        }


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128  # listen backlog; socketserver default of 5 resets bursts of >5 clients
    api_key: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server_version = f"devin-api-provider/{__version__}"
    protocol_version = "HTTP/1.0"  # one request per connection; an SSE stream ends at close

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, fmt: str, *args: Any) -> None:  # protocol-level noise only
        _log(f"{self.address_string()} {fmt % args}")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        pass  # replaced by the per-request line emitted in _dispatch

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    # -- request lifecycle ---------------------------------------------------

    def _dispatch(self, method: str) -> None:
        self._responded = False
        self._status: int | None = None
        self._extra = ""
        route = urlsplit(self.path).path or "/"
        self._route = route.rstrip("/") or "/" if route != "/" else "/"
        note = ""
        try:
            self._handle(method)
        except _ClientGone:
            note = " (client disconnected)"
        finally:
            _log(f"{method} {self._route} {self._status or '-'}{self._extra}{note}")

    def _handle(self, method: str) -> None:
        try:
            self._authorize()
            allowed = _ROUTES.get(self._route)
            if allowed is None:
                raise _HTTPError(404, f"no route for {self._route}", code="not_found")
            if method not in allowed:
                raise _HTTPError(
                    405,
                    f"{method} is not allowed on {self._route}",
                    code="method_not_allowed",
                    headers={"Allow": ", ".join(sorted(allowed))},
                )
            if self._route == "/":
                self._send_json(200, _SERVICE)
            elif self._route == "/healthz":
                self._send_json(200, {"ok": True, "version": __version__})
            elif self._route == "/v1/models":
                self._models()
            else:
                self._chat()
        except _HTTPError as exc:
            self._send_error(exc.status, exc.message, exc.err_type, exc.code, exc.headers)
        except _DevinError as exc:
            self._send_error(
                _status_for(exc), str(exc), "devin_error", getattr(exc, "code", "upstream_error")
            )
        except _ClientGone:
            raise
        except Exception:
            _log("internal error:\n" + traceback.format_exc())
            self._send_error(500, "internal server error", "server_error", "internal_error")

    def _authorize(self) -> None:
        expected = self.server.api_key
        if not expected:
            return
        scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            presented.strip().encode("utf-8"), expected.encode("utf-8")
        )
        if not ok:
            raise _HTTPError(
                401,
                "missing or invalid bearer token",
                "authentication_error",
                "invalid_api_key",
                {"WWW-Authenticate": 'Bearer realm="devin-api-provider"'},
            )

    # -- routes ----------------------------------------------------------------

    def _models(self) -> None:
        proto = _require_protocol()
        data = [
            {
                "id": item["id"],
                "object": "model",
                "created": 0,
                "owned_by": item.get("provider") or "devin",
            }
            for item in proto.list_models()
            if item.get("enabled")
        ]
        self._send_json(200, {"object": "list", "data": data})

    def _chat(self) -> None:
        body = self._read_json_object()
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise _HTTPError(400, "'model' is required and must be a string", code="invalid_model")
        messages = body.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(isinstance(m, dict) for m in messages)
        ):
            raise _HTTPError(
                400, "'messages' must be a non-empty array of message objects", code="invalid_messages"
            )
        tools = body.get("tools")
        if tools is not None and not isinstance(tools, list):
            raise _HTTPError(400, "'tools' must be an array when provided", code="invalid_tools")
        stream = body.get("stream")
        if stream is None:
            stream = False
        elif not isinstance(stream, bool):
            raise _HTTPError(400, "'stream' must be a boolean when provided", code="invalid_stream")
        options = body.get("stream_options")
        include_usage = isinstance(options, dict) and bool(options.get("include_usage"))
        max_tokens = _optional_number(body, "max_tokens")
        if max_tokens is None:
            max_tokens = _optional_number(body, "max_completion_tokens")
        temperature = _optional_number(body, "temperature")
        self._extra = f" model={_safe(model)} stream={str(stream).lower()}"

        proto = _require_protocol()
        token = proto.get_token()
        events = proto.stream_chat(
            token,
            messages=messages,
            model=model,
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            temperature=float(temperature) if temperature is not None else None,
            tools=tools or None,
        )
        if stream:
            self._stream(events, model, include_usage)
        else:
            acc = _Accumulator()
            for event in events:
                acc.add(event)
            self._send_json(200, acc.completion(model))

    def _stream(self, events: Iterable[dict], model: str, include_usage: bool) -> None:
        iterator = iter(events)
        # Fetch the first event before committing headers, so upstream failures
        # before any output still surface as a real HTTP status.
        first = next(iterator, _END)
        cid = _completion_id()
        created = int(time.time())
        acc = _Accumulator()
        seen: set[int] = set()
        try:
            self._start_sse()
            self._sse(_chunk(cid, created, model, {"role": "assistant"}, None))
            event = first
            while event is not _END:
                self._stream_event(event, acc, seen, cid, created, model)
                event = next(iterator, _END)
            self._sse(_chunk(cid, created, model, {}, _finish_reason(acc.finish, bool(acc.calls))))
            if include_usage:
                self._sse(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": _usage(acc.usage),
                    }
                )
            self._sse_done()
        except _ClientGone:
            _close(iterator)
            raise
        except _DevinError as exc:
            _log(f"upstream error mid-stream: {exc}")
            self._sse_error(str(exc), "devin_error", getattr(exc, "code", "upstream_error"))
        except Exception:
            _log("stream failed:\n" + traceback.format_exc())
            self._sse_error("internal server error", "server_error", "internal_error")

    def _stream_event(self, event, acc, seen, cid, created, model) -> None:
        kind = acc.add(event)
        if kind == "delta":
            text = str(event.get("content") or "")
            if text:
                self._sse(_chunk(cid, created, model, {"content": text}, None))
        elif kind == "reasoning":
            text = str(event.get("content") or "")
            if text:
                self._sse(_chunk(cid, created, model, {"reasoning_content": text}, None))
        elif kind == "tool_call":
            index = int(event.get("index") or 0)
            call: dict[str, Any] = {"index": index}
            if index not in seen:
                seen.add(index)
                call["id"] = str(event.get("id") or "") or _call_id()
                call["type"] = "function"
            function: dict[str, str] = {"arguments": str(event.get("arguments") or "")}
            if event.get("name"):
                function["name"] = str(event["name"])
            call["function"] = function
            self._sse(_chunk(cid, created, model, {"tool_calls": [call]}, None))

    # -- low-level writers -----------------------------------------------------

    def _read_json_object(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise _HTTPError(
                411,
                "send Content-Length; chunked request bodies are not supported",
                code="length_required",
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise _HTTPError(400, "invalid Content-Length header", code="invalid_content_length") from None
        if length < 0:
            raise _HTTPError(400, "invalid Content-Length header", code="invalid_content_length")
        if length > MAX_BODY_BYTES:
            raise _HTTPError(
                413, f"request body exceeds {MAX_BODY_BYTES} bytes", code="payload_too_large"
            )
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _HTTPError(400, f"request body must be JSON: {exc}", code="invalid_json") from None
        if not isinstance(body, dict):
            raise _HTTPError(400, "request body must be a JSON object", code="invalid_json")
        return body

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError as exc:  # BrokenPipeError, ConnectionResetError, ...
            raise _ClientGone(str(exc)) from exc

    def _send_bytes(self, status: int, body: bytes, content_type: str, headers=None) -> None:
        self._responded = True
        self._status = status
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as exc:
            raise _ClientGone(str(exc)) from exc

    def _send_json(self, status: int, obj: Any, headers=None) -> None:
        self._send_bytes(
            status,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            headers,
        )

    def _send_error(self, status: int, message: str, err_type: str, code, headers=None) -> None:
        if self._responded:
            _log(f"cannot send {status} {code}: response already started")
            return
        self._send_json(status, _error_body(message, err_type, code), headers)

    def _start_sse(self) -> None:
        self._responded = True
        self._status = 200
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except OSError as exc:
            raise _ClientGone(str(exc)) from exc

    def _sse(self, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self._write(b"data: " + payload.encode("utf-8") + b"\n\n")

    def _sse_done(self) -> None:
        self._write(b"data: [DONE]\n\n")

    def _sse_error(self, message: str, err_type: str, code: str) -> None:
        self._sse(_error_body(message, err_type, code))
        self._sse_done()


def _parse_port(value, source: str = "port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {source} {value!r}: expected an integer from 0 to 65535") from None
    if not 0 <= port <= 65535:
        raise ValueError(f"invalid {source} {port}: expected an integer from 0 to 65535")
    return port


def _resolve(host, port, key):
    """Fill None arguments from DEVIN_PROVIDER_HOST / DEVIN_PROVIDER_PORT / DEVIN_PROVIDER_KEY,
    then from the built-in defaults (127.0.0.1, 8787, no auth)."""
    if host is None:
        host = os.environ.get("DEVIN_PROVIDER_HOST") or DEFAULT_HOST
    if port is None:
        raw = os.environ.get("DEVIN_PROVIDER_PORT")
        port = _parse_port(raw, "DEVIN_PROVIDER_PORT") if raw else DEFAULT_PORT
    if key is None:
        key = os.environ.get("DEVIN_PROVIDER_KEY") or None
    return host, _parse_port(port), key or None


def make_server(host: str | None = None, port: int | None = None, key: str | None = None) -> _Server:
    """Bind without serving. None arguments resolve from the environment (see _resolve).
    Pass port=0 for an ephemeral port (see server.server_address)."""
    _require_protocol()
    host, port, key = _resolve(host, port, key)
    server = _Server((host, port), _Handler)
    server.api_key = key
    return server


def run_server(host: str | None = None, port: int | None = None, key: str | None = None) -> None:
    """Serve until interrupted. None arguments resolve from DEVIN_PROVIDER_HOST / DEVIN_PROVIDER_PORT /
    DEVIN_PROVIDER_KEY, then from the defaults 127.0.0.1 / 8787 / no auth."""
    host, port, key = _resolve(host, port, key)
    try:
        server = make_server(host, port, key)
    except OSError as exc:
        raise SystemExit(f"devin-api-provider: cannot bind {host}:{port}: {exc}") from None
    bound_port = server.server_address[1]
    if not _is_loopback(host) and not server.api_key:
        _log(
            "WARNING: serving on a non-loopback address without DEVIN_PROVIDER_KEY; "
            "anyone who can reach this port can use your Devin session"
        )
    sys.stderr.write(f"devin-api-provider listening on http://{host}:{bound_port}/v1\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
