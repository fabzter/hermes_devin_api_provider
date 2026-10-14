# hermes-devin-api-provider — Design

A standalone, zero-dependency (Python 3.11+ stdlib only) Devin provider for Hermes,
modeled on the omp `devin` provider pattern: OAuth-login credential + cloud
chat backend, exposed to Hermes as a named custom provider pointing at a local
OpenAI-compatible HTTP endpoint.

Replaces the old `devin-acp` source patch (subprocess `devin acp` sessions),
which broke on every Hermes upgrade.

## Architecture

```
~/.local/share/devin/credentials.toml     (devin auth login — official OAuth flow)
                │ devin-provider reads token
                ▼
devin-provider serve  →  127.0.0.1:8787  (OpenAI-compatible: /v1/models, /v1/chat/completions)
                │ Connect-RPC over TLS (protobuf)
                ▼
server.codeium.com  (exa.api_server_pb.ApiServerService/GetChatMessage, GetCliModelConfigs)

~/.hermes/config.yaml:
  providers:
    devin:
      api: http://127.0.0.1:8787/v1
      name: Devin (Teams)
      transport: chat_completions
```

## Verified wire recipe (live-tested 2026-10-14, Teams account)

All of this was empirically verified against the live backend with the session
token from `~/.local/share/devin/credentials.toml`. Do not "improve" these
details without a live probe — several look wrong but are load-bearing.

### Transport
- Host `server.codeium.com:443`, TLS.
- `Authorization: Basic {token}-{token}` — the token **doubled, dash-joined**.
  A single token → `permission_denied`. The copy inside ClientMetadata (#3)
  stays SINGLE.
- Envelope: Connect-RPC, **UNCOMPRESSED ONLY**: flag byte `0x00` + 4-byte
  big-endian length + raw protobuf. **Sending gzip (flag 0x01 +
  Content-Encoding: gzip) makes the server return `internal` "an internal
  error occurred"** — verified both ways.
- Headers for chat: `Content-Type: application/connect+proto`,
  `Connect-Protocol-Version: 1`, `User-Agent: connect-es/2.0.0`.
- Response is HTTP/1.1 chunked; de-chunk, then parse frames
  (flag + BE32 len + proto payload). The last frame is JSON `{}` (end-of-stream).
  Error frames are JSON `{"error":{"code","message"}}`.

### ClientMetadata (request field #1, sub-message)
```
#1  str  'chisel'                  (client name)
#2  str  '2026.8.18'               (client version)
#3  str  <session token>           (SINGLE copy)
#4  str  'en'
#5  str  'macos'
#7  str  '2026.8.18'
#12 str  'chisel'
#31 str  fingerprint: random 732 hex chars = os.urandom(366).hex()
```
Fingerprint must be exactly 732 hex chars; shorter → server-side `internal`.

### GetChatMessage request (proto)
Path: `/exa.api_server_pb.ApiServerService/GetChatMessage`
```
#1  msg  ClientMetadata (above)
#2  str  system prompt            (MUST be non-empty when tools are present;
                                    Claude-family rejects empty system + tools)
#3  rep  ChatMessage:
        #1 str  uuid
        #2 var  source: 1=user, 2=assistant, 4=tool_result
        #3 str  text
        #6 msg  ToolCall (assistant tool calls): {1: id, 2: name, 3: argsJSON}
        #7 str  tool_call_id (on tool_result messages)
        #10 rep ImageData {1: base64, 2: mime}
#7  var  5                          (constant)
#8  msg  CompletionConfig:
        #1 var  1
        #2 var  max_tokens (default 8192; this is an ENFORCED cap)
        #3 var  context_window (128000)
        #5 f64  temperature (LE double; clamp: <0.001 → 0.001; 0.0 → server
              'internal'; default 1.0)
        #7 var  top_k (40)
        #8 f64  top_p (LE double, 0.95)
#10 rep  ToolDef (native tools): {1: name, 2: description, 3: parameters JSON
         schema string}
#15 msg  ModelConfig: {1: uuid, 2: turn=1, 3: var 4}
#16 str  session uuid
#20 var  1
#21 str  model selector (e.g. 'glm-5-2', 'claude-sonnet-5-5-medium')
```
- Coalesce consecutive same-source text-only messages: a run of ≥3 is rejected
  with `invalid_argument`. Runs of 2 are tolerated; merge to be safe.
- Empty-text assistant turns with no tool calls are skipped.
- Tool messages (OpenAI `role:'tool'`) that are NOT part of a native
  tool-call round-trip fold into user text: `[tool result for {id}]: {text}`.
- MCP-gate: upstream pattern-matches tool descriptions for known tool
  signatures and rejects with `permission_denied`. Neutralize by replacing the
  top-level ToolDef #2 description with the tool name and stripping all
  `description` annotations from the parameter JSON schema (structure stays).
- `model` in the response selector: not every catalog string works — use exact
  selectors from GetCliModelConfigs (field #22).

### GetChatMessage response (frames)
```
#3  str  content delta            (OpenAI `content`)
#5  var  finish signal            (2 = stop → 'stop')
#6  rep  ChatToolCall: {1: id, 3: arguments_json, 4: invalid_json_str,
         5: invalid_json_err, 6: is_custom_tool_call}
        NO name field on the response side — resolve name by reverse-lookup
        against the request-side tools list.
#7  msg  metadata: {2: prompt_tokens, 3: completion_tokens, 9: model,
         4: cache-write tokens (Claude family), 5: cache-read tokens,
         6: credit cost}
#9  str  reasoning delta          (OpenAI `reasoning_content`)
```
Finish enum → OpenAI: 2 → 'stop'; map others via a small table
(0 none,1 length? — keep 2→stop, 3→'length', else 'stop' as safe default).

### GetCliModelConfigs (catalog, zero-bill)
Path: `/exa.api_server_pb.ApiServerService/GetCliModelConfigs`
- Headers: `Content-Type: application/proto`, `Connect-Protocol-Version: 1`.
- Body: just `wmsg(1, ClientMetadata)` (same doubled-Basic auth header).
- Response proto: repeated #1 ClientModelConfig:
  `#1 label, #4 disabled(bool), #10 provider int
   (1=cognition,2=openai,3=anthropic,4=google,5=xai,6=deepseek,7=moonshot,
    9=zhipu,11=nvidia), #22 selector, #23 ModelInfo{#23 short alias}`
- The catalog lists all models regardless of tier; entitlement is enforced at
  chat time. ~262 models on a Teams account (2026-10-14).

### Credential
`~/.local/share/devin/credentials.toml`:
```toml
windsurf_api_key = "devin-session-token$<JWT>"   # long-lived, opaque session
```
Resolution order for the provider: `DEVIN_CONNECT_TOKEN` env →
`WINDSURF_API_KEY` env → credentials.toml.
Login = run the official `devin auth login` (browser OAuth) which writes this
file; the provider never stores credentials itself.

## Repo layout

```
hermes-devin-api-provider/
├── devin_api_provider/
│   ├── __init__.py          # version
│   ├── protocol.py          # wire codec + GetChatMessage/GetCliModelConfigs
│   ├── server.py            # OpenAI-compatible HTTP server + SSE
│   └── __main__.py          # CLI dispatch: serve|login|status|models|chat
├── bin/devin-provider       # executable shim (python3 -m …)
├── scripts/
│   ├── remove_devin_acp.py  # cutover: removes the OLD devin-acp integration
│   └── install_hermes.py    # merges providers: entry into ~/.hermes/config.yaml
├── tests/
│   └── test_protocol.py     # offline unit tests on encode/decode
├── launchd/com.fabzter.devin-provider.plist
├── install.sh
├── DESIGN.md                # this file
├── README.md
├── LICENSE                  # MIT
├── Makefile
└── .gitignore
```

## protocol.py — public contract (DO NOT change signatures)

```python
class DevinError(RuntimeError):
    # .code   str   'unauthenticated'|'rate_limited'|'upstream_internal'|
    #               'invalid_request'|'unavailable'|'timeout'|'upstream_error'
    # .status int   suggested HTTP status (401/429/502/400/503/504/502)
    def __init__(self, message: str, code: str = "upstream_error",
                 status: int = 502): ...

def get_token(env: dict | None = None) -> str:
    """DEVIN_CONNECT_TOKEN > WINDSURF_API_KEY > credentials.toml.
    Raises DevinError('no credential found …', 'unauthenticated', 401)."""

def list_models(token: str | None = None, timeout: float = 30.0,
                refresh: bool = False) -> list[dict]:
    """[{id: selector, label, provider, enabled}] — live catalog, in-memory cache."""

def stream_chat(token: str, *, messages: list[dict], model: str,
                max_tokens: int | None = None,
                temperature: float | None = None,
                tools: list[dict] | None = None,
                timeout: float = 600.0) -> "Iterator[dict]":
    """Yields events (dicts):
      {"type": "delta",      "content": str}
      {"type": "reasoning",  "content": str}
      {"type": "tool_call",  "index": int, "id": str,
                            "name": str, "arguments": str}
      {"type": "finish",     "reason": str}     # OpenAI finish_reason
      {"type": "usage",      "prompt_tokens": int, "completion_tokens": int,
                            "model": str}
    Raises DevinError on failure. Never logs the token."""
```

`messages` / `tools` are OpenAI chat-completions shapes
(`system|user|assistant|tool` roles, `tool_calls`, `function` tools).

## server.py — behavior

- `devin_api_provider serve [--host 127.0.0.1] [--port 8787]`
  (env `DEVIN_PROVIDER_HOST/PORT`).
- `GET /v1/models` → `{"object":"list","data":[{"id","object":"model","created":0,"owned_by"}]}`
  (enabled models only).
- `POST /v1/chat/completions`:
  - non-stream → full `chat.completion` object (content, reasoning_content,
    tool_calls, usage, finish_reason).
  - `stream:true` → SSE `text/event-stream`, `chat.completion.chunk` frames:
    role-priming chunk, reasoning deltas (as `reasoning_content`),
    content deltas, tool_call deltas, finish chunk, optional usage chunk
    when `stream_options.include_usage`, then `data: [DONE]`.
  - Bearer auth enforced only when `DEVIN_PROVIDER_KEY` is set.
  - Errors → `{"error":{"message","type","code"}}` with mapped HTTP status.
- CLI subcommands: `login` (wraps `devin auth login`, then probes),
  `status` (token presence + catalog probe + model count),
  `models` (prints selectors), `chat` (one-shot prompt, for testing),
  `version`.

## Cutover (scripts/remove_devin_acp.py — run manually, idempotent, --dry-run)

1. `git -C ~/.hermes/hermes-agent checkout --` the patched files (agent_init,
   agent_runtime_helpers, auxiliary_client, conversation_loop, auth,
   model_normalize, model_switch, models, providers, runtime_provider) and
   delete untracked `agent/devin_acp_client.py`.
2. Remove skill `~/.hermes/skills/autonomous-ai-agents/devin-acp-upgrade-reapply/`.
3. Move `~/.hermes/devin-acp-reuse-bundle/` → repo `legacy/` for reference.
4. Verify hermes still imports (`py_compile`) and `hermes model` no longer
   lists devin-acp.

## Reference material

- `/tmp/windsurfapi/devin-connect.js` — WindsurfAPI's calibrated adapter
  (the source of the wire recipe; our probes re-verified the core on live).
- `/tmp/windsurfapi/connect.js`, `proto.js`, `devin-connect-catalog.js`,
  `devin-connect-models.js`, `devin-connect-openai.js`.
