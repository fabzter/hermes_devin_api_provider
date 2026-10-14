"""Offline tests for devin_api_provider.protocol (DESIGN.md "Verified wire recipe").

Run from the repo root:  python3 -m tests.test_protocol   (pytest also collects these)
Nothing touches the network: protocol._post_connect is swapped for a fake that returns canned bytes.
"""

from __future__ import annotations

import contextlib
import json
import struct
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from devin_api_provider import protocol as p
from devin_api_provider.protocol import DevinError, parse_fields, wfix64, wmsg, wstr, wvar

TOKEN = "TESTTOKEN-abc123"
MODEL = "glm-5-2"
CHAT_PATH = "/exa.api_server_pb.ApiServerService/GetChatMessage"
CATALOG_PATH = "/exa.api_server_pb.ApiServerService/GetCliModelConfigs"
SYSTEM_DEFAULT = "You are a helpful assistant. Use the available tools when appropriate."

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Gets the weather for a city in great detail",
        "parameters": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "description": "root annotation",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "description": {"type": "string", "description": "a property literally named description"},
            },
            "required": ["city"],
        },
    },
}
PING_TOOL = {"type": "function", "function": {"name": "ping", "description": "Ping the service"}}
TOOL_CALL_TURN = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"MX"}'}}
    ],
}
TOOL_RESULT_TURN = {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}


# ---------------------------------------------------------------- builders and helpers

def _frame(payload: bytes, flag: int = 0x00) -> bytes:
    """Connect envelope: flag byte, big-endian u32 length, payload."""
    return bytes([flag]) + len(payload).to_bytes(4, "big") + payload


END = _frame(b"{}", flag=0x02)  # end-of-stream JSON frame (DESIGN: last frame is JSON {})


def _error_frame(code: str, message: str) -> bytes:
    return _frame(json.dumps({"error": {"code": code, "message": message}}).encode(), flag=0x02)


def _chat_body(*, content=(), reasoning=(), tool_calls=(), finish: int | None = 2, usage=None,
               end: bool = True) -> bytes:
    parts = [_frame(wstr(3, piece)) for piece in content]
    parts += [_frame(wstr(9, piece)) for piece in reasoning]
    parts += [_frame(wmsg(6, wstr(1, cid) + wstr(3, args))) for cid, args in tool_calls]
    if finish is not None:
        parts.append(_frame(wvar(5, finish)))
    if usage is not None:
        prompt, completion, model = usage
        parts.append(_frame(wmsg(7, wvar(2, prompt) + wvar(3, completion) + wstr(9, model))))
    if end:
        parts.append(END)
    return b"".join(parts)


def _catalog_body(entries: list[bytes], field: int = 1) -> bytes:
    """Live GetCliModelConfigs shape: raw protobuf, repeated top-level ClientModelConfig (#1)."""
    return b"".join(wmsg(field, e) for e in entries)


def _catalog_envelope(entries: list[bytes], field: int = 1) -> bytes:
    """Connect envelope-framed catalog body; exercises the envelope fallback path."""
    return _frame(_catalog_body(entries, field)) + END


def _model_entry(*, label=None, disabled=None, provider=None, selector=None, alias=None) -> bytes:
    out = b""
    if label is not None:
        out += wstr(1, label)
    if disabled is not None:
        out += wvar(4, int(disabled))
    if provider is not None:
        out += wvar(10, provider)
    if selector is not None:
        out += wstr(22, selector)
    if alias is not None:
        out += wmsg(23, wstr(23, alias))
    return out


def _s(v: Any) -> Any:
    return v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else v


def _f64(v: Any) -> float:
    if isinstance(v, float):
        return v
    if isinstance(v, int):
        return struct.unpack("<d", v.to_bytes(8, "little"))[0]
    return struct.unpack("<d", bytes(v))[0]


def _all(fields: list, n: int) -> list:
    return [v for f, _wt, v in fields if f == n]


def _one(fields: list, n: int, default: Any = None) -> Any:
    vals = _all(fields, n)
    return vals[0] if vals else default


def _tuples(fields: list) -> list[tuple]:
    return [tuple(f) for f in fields]


def _unwrap(payload: bytes) -> bytes:
    """Drop a Connect envelope if the layer added one (a protobuf body never starts with 0x00)."""
    if len(payload) >= 5 and payload[0] == 0 and int.from_bytes(payload[1:5], "big") == len(payload) - 5:
        return payload[5:]
    return payload


def _raises(fn: Callable[[], object]) -> DevinError:
    try:
        fn()
    except DevinError as exc:
        return exc
    raise AssertionError("expected DevinError, nothing raised")


class FakePost:
    """Stands in for the HTTPS entry points: `_post_connect` (whole-body catalog) and
    `_post_connect_stream` (incremental chat). Every call is recorded in `calls`.

    `__call__` returns the canned `body`. `stream()` yields `pieces` (default: `body` as one piece)
    and sets `closed` once the generator finishes or the consumer closes it.
    """

    def __init__(self, body: bytes, pieces: list[bytes] | None = None) -> None:
        self.body = body
        self.pieces = [body] if pieces is None else list(pieces)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def __call__(self, path: str, token: str, proto: bytes, *args: Any, **kwargs: Any) -> bytes:
        self.calls.append({"path": path, "token": token, "proto": proto, "args": args, "kwargs": kwargs})
        return self.body

    def stream(self, path: str, token: str, proto: bytes, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        self.calls.append({"path": path, "token": token, "proto": proto, "args": args, "kwargs": kwargs})
        try:
            yield from self.pieces
        finally:
            self.closed = True

    def values(self, idx: int = 0) -> list[Any]:
        call = self.calls[idx]
        return list(call["args"]) + list(call["kwargs"].values())


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("test attempted a real network connection")


@contextlib.contextmanager
def _patched_post(fake: FakePost) -> Iterator[FakePost]:
    """Route both HTTPS entry points through `fake`; any real connect fails the test."""
    names = ("_post_connect", "_post_connect_stream", "_connect")
    saved = {name: getattr(p, name) for name in names}
    p._post_connect = fake  # type: ignore[assignment]
    p._post_connect_stream = fake.stream  # type: ignore[assignment]
    p._connect = _no_network  # type: ignore[assignment]
    try:
        yield fake
    finally:
        for name in names:
            setattr(p, name, saved[name])


def _run_chat(messages: list[dict], *, body: bytes | None = None, pieces: list[bytes] | None = None,
              **kwargs: Any) -> tuple[list[dict], FakePost]:
    fake = FakePost(body if body is not None else _chat_body(content=["ok"]), pieces)
    with _patched_post(fake):
        events = list(p.stream_chat(TOKEN, messages=messages, model=MODEL, **kwargs))
    return events, fake


def _chat_request(messages: list[dict], **kwargs: Any) -> list:
    _events, fake = _run_chat(messages, **kwargs)
    return parse_fields(_unwrap(fake.calls[0]["proto"]))


def _chat_messages(req: list) -> list[list]:
    return [parse_fields(m) for m in _all(req, 3)]


def _source(msg: list) -> Any:
    return _one(msg, 2)


def _text(msg: list) -> str:
    return _s(_one(msg, 3, b""))


def _tool_defs(req: list) -> list[list]:
    return [parse_fields(t) for t in _all(req, 10)]


def _of_type(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("type") == kind]


def _stream_error(body: bytes) -> DevinError:
    return _raises(lambda: _run_chat([{"role": "user", "content": "hi"}], body=body))


def _list_models_with(body: bytes) -> list[dict]:
    with _patched_post(FakePost(body)):
        return p.list_models(TOKEN, refresh=True)


# ---------------------------------------------------------------- wire codec

def test_golden_wire_bytes() -> None:
    assert wvar(1, 150).hex() == "089601"
    assert wstr(2, "testing").hex() == "120774657374696e67"
    assert wfix64(5, 1.0).hex() == "29000000000000f03f"
    assert wmsg(3, b"\x08\x01").hex() == "1a020801"


def test_varint_and_parse_fields_roundtrip() -> None:
    for n in (0, 1, 127, 128, 300, 2**32 + 5, 2**53):
        assert _tuples(parse_fields(wvar(1, n))) == [(1, 0, n)], n
    fields = parse_fields(wstr(2, "testing") + wfix64(5, 2.5) + wmsg(3, wvar(1, 7)))
    assert [f for f, _wt, _v in fields] == [2, 5, 3]
    assert _s(fields[0][2]) == "testing"
    assert _f64(fields[1][2]) == 2.5
    assert _tuples(parse_fields(fields[2][2])) == [(1, 0, 7)]


# ---------------------------------------------------------------- errors, credentials, transport

def test_devin_error_defaults_and_attributes() -> None:
    err = DevinError("boom", "rate_limited", 429)
    assert isinstance(err, RuntimeError) and str(err) == "boom"
    assert (err.code, err.status) == ("rate_limited", 429)
    default = DevinError("x")
    assert (default.code, default.status) == ("upstream_error", 502)


def test_get_token_priority_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cred = Path(tmp) / "credentials.toml"
        cred.write_text('windsurf_api_key = "devin-session-token$FILE"\n', encoding="utf-8")
        original = p.CREDENTIALS_PATH
        p.CREDENTIALS_PATH = cred
        try:
            base = {"UNRELATED": "1"}  # non-empty so no `env or os.environ` fallback can leak the host env
            assert p.get_token({**base, "DEVIN_CONNECT_TOKEN": "A", "WINDSURF_API_KEY": "B"}) == "A"
            assert p.get_token({**base, "WINDSURF_API_KEY": "B"}) == "B"
            assert p.get_token(base) == "devin-session-token$FILE"
        finally:
            p.CREDENTIALS_PATH = original


def test_get_token_without_credentials_raises_unauthenticated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        original = p.CREDENTIALS_PATH
        p.CREDENTIALS_PATH = Path(tmp) / "absent.toml"
        try:
            err = _raises(lambda: p.get_token({"UNRELATED": "1"}))
        finally:
            p.CREDENTIALS_PATH = original
    assert (err.code, err.status) == ("unauthenticated", 401)
    assert "no credential found" in str(err)


def test_build_http_request_doubles_basic_auth_and_skips_gzip() -> None:
    raw = p._build_http_request(CHAT_PATH, TOKEN, b"\x0a\x00", "application/connect+proto")
    if isinstance(raw, str):
        raw = raw.encode()
    head = raw.split(b"\r\n\r\n", 1)[0]
    assert head.startswith(b"POST " + CHAT_PATH.encode() + b" HTTP/1.1")
    assert f"Authorization: Basic {TOKEN}-{TOKEN}".encode() in head
    assert b"Content-Type: application/connect+proto" in head
    assert b"Connect-Protocol-Version: 1" in head
    assert b"User-Agent: connect-es/2.0.0" in head
    assert b"Host: server.codeium.com" in head
    assert b"Content-Encoding" not in head


def test_parse_http_response_maps_status_codes() -> None:
    cases = [(401, "unauthenticated", 401), (429, "rate_limited", 429), (500, "upstream_error", 502)]
    for status, code, http_status in cases:
        raw = f"HTTP/1.1 {status} Status\r\nContent-Length: 0\r\n\r\n".encode()
        err = _raises(lambda: p._parse_http_response(raw, TOKEN))
        assert (err.code, err.status) == (code, http_status), status


def test_parse_http_response_returns_body() -> None:
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    plain = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc"
    assert p._parse_http_response(chunked, TOKEN) == b"hello"
    assert p._parse_http_response(plain, TOKEN) == b"abc"


def test_dechunk_accepts_crlf_and_lf_terminators() -> None:
    assert p._dechunk(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n") == b"hello world"
    assert p._dechunk(b"5\nhello\n0\n\n") == b"hello"


def test_client_metadata_matches_design_fields() -> None:
    md = parse_fields(p._client_metadata(TOKEN))
    assert _s(_one(md, 1)) == "chisel"
    assert _s(_one(md, 2)) == "2026.8.18"
    assert [_s(v) for v in _all(md, 3)] == [TOKEN]  # token appears exactly once
    assert _s(_one(md, 4)) == "en"
    assert _s(_one(md, 5)) == "macos"
    assert _s(_one(md, 7)) == "2026.8.18"
    assert _s(_one(md, 12)) == "chisel"
    fingerprint = _s(_one(md, 31))
    assert len(fingerprint) == 732 and all(c in "0123456789abcdef" for c in fingerprint)
    assert _s(_one(parse_fields(p._client_metadata(TOKEN)), 31)) != fingerprint  # fresh random per call


# ---------------------------------------------------------------- GetChatMessage request

def test_stream_request_envelope_fields() -> None:
    events, fake = _run_chat([{"role": "user", "content": "hi"}])
    call = fake.calls[0]
    assert call["path"] == CHAT_PATH
    assert "application/connect+proto" in fake.values()
    req = parse_fields(_unwrap(call["proto"]))
    meta = parse_fields(_one(req, 1))
    assert _s(_one(meta, 1)) == "chisel" and _s(_one(meta, 3)) == TOKEN
    uuid.UUID(_s(_one(req, 16)))  # session id is a UUID
    model_cfg = parse_fields(_one(req, 15))
    uuid.UUID(_s(_one(model_cfg, 1)))
    assert _one(model_cfg, 2) == 1 and _one(model_cfg, 3) == 4
    assert _one(req, 7) == 5 and _one(req, 20) == 1
    assert _s(_one(req, 21)) == MODEL
    assert events


def test_completion_config_defaults_and_max_tokens_override() -> None:
    req = _chat_request([{"role": "user", "content": "hi"}])
    cfg = parse_fields(_one(req, 8))
    assert _one(cfg, 1) == 1
    assert _one(cfg, 2) == 8192
    assert _one(cfg, 3) == 128000
    assert _one(cfg, 7) == 40
    assert _f64(_one(cfg, 5)) == 1.0
    assert abs(_f64(_one(cfg, 8)) - 0.95) < 1e-12
    capped = parse_fields(_one(_chat_request([{"role": "user", "content": "hi"}], max_tokens=256), 8))
    assert _one(capped, 2) == 256


def test_temperature_clamps_to_minimum() -> None:
    for given, expected in ((0, 0.001), (-1.0, 0.001), (0.5, 0.5)):
        req = _chat_request([{"role": "user", "content": "hi"}], temperature=given)
        cfg = parse_fields(_one(req, 8))
        assert abs(_f64(_one(cfg, 5)) - expected) < 1e-12, (given, _f64(_one(cfg, 5)))


def test_default_system_prompt_only_when_tools_present() -> None:
    plain = _chat_request([{"role": "user", "content": "hi"}])
    assert _s(_one(plain, 2)) in (None, "")
    with_tools = _chat_request([{"role": "user", "content": "hi"}], tools=[WEATHER_TOOL])
    assert _s(_one(with_tools, 2)) == SYSTEM_DEFAULT


def test_consecutive_same_source_messages_merge() -> None:
    req = _chat_request([{"role": "user", "content": c} for c in ("a", "b", "c")])
    msgs = _chat_messages(req)
    assert len(msgs) == 1, msgs
    assert _source(msgs[0]) == 1 and _text(msgs[0]) == "a\n\nb\n\nc"


def test_system_messages_are_hoisted_without_breaking_adjacency() -> None:
    req = _chat_request([
        {"role": "system", "content": "S1"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "S2"},
        {"role": "user", "content": "v"},
    ])
    assert _s(_one(req, 2)) == "S1\nS2"
    msgs = _chat_messages(req)
    assert len(msgs) == 1 and _text(msgs[0]) == "u\n\nv", msgs


def test_roles_map_to_sources() -> None:
    req = _chat_request([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "bye"},
    ])
    assert [_source(m) for m in _chat_messages(req)] == [1, 2, 1]


def test_empty_assistant_turn_without_tool_calls_is_skipped() -> None:
    req = _chat_request([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "again"},
    ])
    assert all(_source(m) != 2 for m in _chat_messages(req))


def test_native_tool_round_trip_uses_tool_result_source() -> None:
    req = _chat_request([{"role": "user", "content": "weather?"}, TOOL_CALL_TURN, TOOL_RESULT_TURN])
    results = [m for m in _chat_messages(req) if _source(m) == 4]
    assert len(results) == 1, _chat_messages(req)
    assert _s(_one(results[0], 7)) == "call_1"
    assert "sunny" in _text(results[0])


def test_assistant_tool_calls_encode_toolcall_submessages() -> None:
    req = _chat_request([{"role": "user", "content": "weather?"}, TOOL_CALL_TURN, TOOL_RESULT_TURN])
    calls = [parse_fields(c) for m in _chat_messages(req) for c in _all(m, 6)]
    assert len(calls) == 1, calls
    assert _s(_one(calls[0], 1)) == "call_1"
    assert _s(_one(calls[0], 2)) == "get_weather"
    assert json.loads(_s(_one(calls[0], 3))) == {"city": "MX"}


def test_orphan_tool_result_folds_into_user_text() -> None:
    req = _chat_request([
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "zzz", "content": "result"},
    ])
    msgs = _chat_messages(req)
    assert all(_source(m) != 4 for m in msgs), msgs
    user_text = "\n".join(_text(m) for m in msgs if _source(m) == 1)
    assert "[tool result for zzz]: result" in user_text


def test_image_data_url_becomes_image_data() -> None:
    req = _chat_request([{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUFBQQ=="}},
    ]}])
    images = [parse_fields(i) for m in _chat_messages(req) for i in _all(m, 10)]
    assert len(images) == 1, images
    assert _s(_one(images[0], 1)) == "QUFBQQ=="
    assert _s(_one(images[0], 2)) == "image/png"


def test_non_data_image_url_is_rejected_before_any_request() -> None:
    fake = FakePost(_chat_body(content=["ok"]))
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]
    with _patched_post(fake):
        err = _raises(lambda: list(p.stream_chat(TOKEN, messages=messages, model=MODEL)))
    assert (err.code, err.status) == ("invalid_request", 400)
    assert not fake.calls


def test_tool_def_neutralizes_mcp_gate_and_normalizes_schema() -> None:
    _events, fake = _run_chat([{"role": "user", "content": "hi"}], tools=[WEATHER_TOOL])
    raw = _unwrap(fake.calls[0]["proto"])
    assert b"Gets the weather" not in raw and b"root annotation" not in raw
    tdefs = _tool_defs(parse_fields(raw))
    assert len(tdefs) == 1
    assert _s(_one(tdefs[0], 1)) == "get_weather"
    assert _s(_one(tdefs[0], 2)) == "get_weather"  # MCP-gate: top-level description replaced by the name
    schema = json.loads(_s(_one(tdefs[0], 3)))
    assert "$schema" not in schema and "description" not in schema
    assert schema["type"] == "object"
    assert schema["required"] == ["city"]
    assert schema["properties"]["city"] == {"type": "string"}
    assert schema["properties"]["description"] == {"type": "string"}  # property NAMED description survives


def test_tool_schema_gets_object_defaults_when_parameters_missing() -> None:
    _events, fake = _run_chat([{"role": "user", "content": "hi"}], tools=[PING_TOOL])
    tdefs = _tool_defs(parse_fields(_unwrap(fake.calls[0]["proto"])))
    schema = json.loads(_s(_one(tdefs[0], 3)))
    assert schema["type"] == "object" and schema["properties"] == {}


def test_strip_schema_descriptions_keeps_property_named_description() -> None:
    schema = {
        "type": "object",
        "description": "root",
        "properties": {
            "description": {"type": "string", "description": "inner"},
            "n": {"type": "integer", "description": "count"},
        },
        "required": ["n"],
    }
    assert p._strip_schema_descriptions(schema) == {
        "type": "object",
        "properties": {"description": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["n"],
    }


# ---------------------------------------------------------------- GetChatMessage response

def test_tool_call_name_resolves_against_request_tools() -> None:
    body = _chat_body(tool_calls=[("call_9", '{"city":"MX"}')])
    events, _fake = _run_chat([{"role": "user", "content": "weather?"}], body=body, tools=[WEATHER_TOOL])
    calls = _of_type(events, "tool_call")
    assert len(calls) == 1, events
    assert (calls[0]["index"], calls[0]["id"], calls[0]["name"]) == (0, "call_9", "get_weather")
    assert json.loads(calls[0]["arguments"]) == {"city": "MX"}


def test_stream_accumulates_frames_and_orders_trailing_events() -> None:
    body = _chat_body(content=["Hel", "lo"], reasoning=["think"], tool_calls=[("call_1", "{}")],
                      finish=2, usage=(11, 7, MODEL))
    events, _fake = _run_chat([{"role": "user", "content": "hi"}], body=body)
    assert "".join(e["content"] for e in _of_type(events, "delta")) == "Hello"
    assert [e["content"] for e in _of_type(events, "reasoning")] == ["think"]
    assert [e["type"] for e in events][-3:] == ["tool_call", "finish", "usage"], events
    usage = _of_type(events, "usage")[0]
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["model"]) == (11, 7, MODEL)
    assert _of_type(events, "finish")[0]["reason"] == "stop"


def test_finish_signal_maps_to_openai_reason() -> None:
    for signal, reason in ((2, "stop"), (3, "length"), (0, "stop")):
        events, _fake = _run_chat([{"role": "user", "content": "hi"}],
                                  body=_chat_body(content=["x"], finish=signal))
        assert _of_type(events, "finish")[0]["reason"] == reason, signal


CLASSIFICATION = [
    (("permission_denied", "Unable to process request due to an MCP configuration issue"), ("upstream_error", 502)),
    (("invalid_argument", "Request blocked: violates the usage policy"), ("invalid_request", 400)),
    (("unknown", "The model refused this request under its policy"), ("invalid_request", 400)),
    (("unauthenticated", "invalid api key"), ("unauthenticated", 401)),
    (("unauthenticated", "session expired"), ("unauthenticated", 401)),
    (("rate_limit", "rate limit exceeded"), ("rate_limited", 429)),
    (("internal", "an internal error occurred"), ("upstream_internal", 502)),
    (("weird_code", "something unexpected"), ("upstream_error", 502)),
]


def test_error_frames_classify_in_marker_order() -> None:
    for (code, message), expected in CLASSIFICATION:
        err = _stream_error(_error_frame(code, message))
        assert (err.code, err.status) == expected, (code, message, err.code, err.status)


def test_error_text_never_echoes_the_token() -> None:
    secret = "SECRET-TOKEN-XYZ"
    fake = FakePost(_error_frame("unauthenticated", f"bad token {secret}"))

    def go() -> None:
        with _patched_post(fake):
            list(p.stream_chat(secret, messages=[{"role": "user", "content": "hi"}], model=MODEL))

    err = _raises(go)
    assert secret not in str(err) and "<redacted>" in str(err)
    assert (err.code, err.status) == ("unauthenticated", 401)


# ---------------------------------------------------------------- GetCliModelConfigs catalog

def test_catalog_request_is_raw_proto_with_metadata_only() -> None:
    fake = FakePost(_catalog_body([_model_entry(label="A", provider=1, selector="a-1")]))
    with _patched_post(fake):
        p.list_models(TOKEN, refresh=True)
    call = fake.calls[0]
    assert call["path"] == CATALOG_PATH
    assert "application/proto" in fake.values() and "application/connect+proto" not in fake.values()
    assert call["proto"][:1] == b"\x0a"  # raw wmsg(1, ClientMetadata); no envelope on this path
    top = parse_fields(call["proto"])
    assert [f for f, _wt, _v in top] == [1]
    assert _s(_one(parse_fields(_one(top, 1)), 3)) == TOKEN


def test_catalog_entries_map_to_model_dicts() -> None:
    models = _list_models_with(_catalog_body([
        _model_entry(label="Claude Sonnet 5.5", disabled=False, provider=3,
                     selector="claude-sonnet-5-5-medium", alias="sonnet"),
        _model_entry(label="Old Model", disabled=True, provider=99, selector="old-1"),
    ]))
    assert models == [
        {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5", "provider": "anthropic", "enabled": True},
        {"id": "old-1", "label": "Old Model", "provider": "provider-99", "enabled": False},
    ]


def test_catalog_skips_bad_and_selectorless_entries_and_reads_field_2() -> None:
    body = _catalog_body([
        _model_entry(label="Good", provider=2, selector="good-1"),
        b"\x0f",  # unparseable entry: skipped, does not raise
        _model_entry(label="No selector", provider=2),  # no selector: skipped
    ]) + wmsg(2, _model_entry(label="Second", provider=5, selector="second-1"))
    assert _list_models_with(body) == [
        {"id": "good-1", "label": "Good", "provider": "openai", "enabled": True},
        {"id": "second-1", "label": "Second", "provider": "xai", "enabled": True},
    ]


def test_catalog_unparseable_raw_body_raises_catalog_parse_failed() -> None:
    err = _raises(lambda: _list_models_with(b"\x0f\x01"))
    assert (err.code, err.status) == ("upstream_error", 502)
    assert "catalog parse failed" in str(err)


def test_catalog_json_error_body_maps_to_unauthenticated() -> None:
    body = b'{"error":{"code":"unauthenticated","message":"invalid api key"}}'
    err = _raises(lambda: _list_models_with(body))
    assert (err.code, err.status) == ("unauthenticated", 401)


def test_catalog_envelope_fallback_parses_framed_body() -> None:
    body = _catalog_envelope([_model_entry(label="Framed", provider=3, selector="framed-1")])
    assert _list_models_with(body) == [
        {"id": "framed-1", "label": "Framed", "provider": "anthropic", "enabled": True},
    ]


def test_catalog_cache_serves_repeat_calls_without_refresh() -> None:
    fake = FakePost(_catalog_body([_model_entry(label="A", provider=1, selector="a-1")]))
    with _patched_post(fake):
        first = p.list_models(TOKEN, refresh=True)
        second = p.list_models(TOKEN)
    assert first == second
    assert len(fake.calls) == 1


# ---------------------------------------------------------------- incremental frame consumption

def _split_every(body: bytes, size: int) -> list[bytes]:
    return [body[i:i + size] for i in range(0, len(body), size)]


def _events_from_pieces(pieces: list[bytes]) -> list[dict]:
    return list(p._chat_events(pieces, token=TOKEN, model=MODEL, tool_specs=[]))


def test_frame_reader_is_split_invariant() -> None:
    body = _frame(b"abc") + _frame(b"", flag=0x02) + _frame(b"xyz")
    whole = list(p._iter_frames(body))
    assert len(whole) == 3
    for size in (1, 2, 3, 7, len(body)):
        reader = p._FrameReader()
        got = [frame for piece in _split_every(body, size) for frame in reader.feed(piece)]
        assert got == whole and reader.partial_error() is None


def test_partial_tail_names_header_or_payload() -> None:
    reader = p._FrameReader()
    assert list(reader.feed(b"\x00\x00\x00")) == []
    assert reader.partial_error() == "truncated envelope header"
    reader = p._FrameReader()
    assert list(reader.feed(_frame(b"abcdef")[:-2])) == []
    assert reader.partial_error() == "truncated envelope payload"
    err = _raises(lambda: list(p._iter_frames(_frame(b"abcdef")[:-2])))
    assert err.status == 502 and "truncated envelope payload" in str(err)


def test_split_pieces_yield_the_same_events_as_one_body() -> None:
    body = _chat_body(content=["Hel", "lo"], reasoning=["think"],
                      tool_calls=[("call_1", '{"city":"MX"}')], finish=2, usage=(11, 7, MODEL))
    whole = _events_from_pieces([body])
    assert [e["content"] for e in _of_type(whole, "delta")] == ["Hel", "lo"]
    for size in (1, 3, 5):
        assert _events_from_pieces(_split_every(body, size)) == whole


def test_delta_is_yielded_before_the_next_piece_is_requested() -> None:
    requested: list[str] = []

    def pieces() -> Iterator[bytes]:
        yield _frame(wstr(3, "first"))
        requested.append("second piece")
        yield _frame(wstr(3, "second")) + END

    stream = p._chat_events(pieces(), token=TOKEN, model=MODEL, tool_specs=[])
    assert next(stream) == {"type": "delta", "content": "first"}
    assert requested == []
    assert [e["content"] for e in _of_type(list(stream), "delta")] == ["second"]
    assert requested == ["second piece"]


def test_end_frame_stops_reading_the_stream() -> None:
    pulled: list[int] = []

    def pieces() -> Iterator[bytes]:
        pulled.append(0)
        yield _chat_body(content=["ok"])  # finish signal, then END
        pulled.append(1)
        yield b"\x00\x00\x00"

    events = list(p._chat_events(pieces(), token=TOKEN, model=MODEL, tool_specs=[]))
    assert pulled == [0]
    assert [e["content"] for e in _of_type(events, "delta")] == ["ok"]


def test_finish_only_stream_is_drained_to_eof_for_usage() -> None:
    body = _chat_body(content=["ok"], finish=2, usage=(11, 7, MODEL), end=False)
    events = _events_from_pieces([body])
    assert _of_type(events, "finish") == [{"type": "finish", "reason": "stop"}]
    assert _of_type(events, "usage") == [
        {"type": "usage", "prompt_tokens": 11, "completion_tokens": 7, "model": MODEL}]


def test_truncation_before_completion_names_the_partial_frame() -> None:
    body = _chat_body(content=["partial"], finish=None)  # content frame, then END
    for cut, message in ((1, "truncated envelope payload"), (3, "truncated envelope header")):
        err = _raises(lambda: _events_from_pieces([body[:-cut]]))
        assert err.status == 502 and message in str(err)
    clean = _chat_body(content=["partial"], finish=None, end=False)
    err = _raises(lambda: _events_from_pieces([clean]))
    assert err.status == 502 and "ended before completion" in str(err)


def test_truncated_tail_after_completion_is_discarded() -> None:
    body = _chat_body(content=["ok"], finish=2, usage=(1, 2, MODEL), end=False)
    events = _events_from_pieces([body + _frame(b"zz")[:-1]])
    assert [e["content"] for e in _of_type(events, "delta")] == ["ok"]
    assert _of_type(events, "usage") and _of_type(events, "finish")


def test_trailing_bytes_after_end_frame_are_ignored() -> None:
    events = _events_from_pieces([_chat_body(content=["ok"]) + b"\x00\x00"])
    assert [e["content"] for e in _of_type(events, "delta")] == ["ok"]
    assert _of_type(events, "finish") == [{"type": "finish", "reason": "stop"}]


# ---------------------------------------------------------------- runner

def _run_all() -> int:
    tests = sorted((name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # report every failure and keep going
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
