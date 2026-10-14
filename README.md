# hermes-devin-api-provider

Use a Devin (Teams) account as a model provider in Hermes. Pure Python 3.11+ standard library, no dependencies.

Unofficial. Not affiliated with Devin/Cognition, Windsurf/Codeium, or Hermes. It replaces the old `devin-acp` source patch for Hermes, which had to be re-applied after Hermes upgrades.

**Status:** live-verified (2026-10-14, Teams account) — see DESIGN.md "Verified wire recipe" for the live record. Remaining unverified: non-loopback bind, re-running the installer with a changed `--api`.

## How it works

1. `devin auth login` (the official Devin CLI) signs you in. The provider reads the token from `~/.local/share/devin/credentials.toml` unless an environment override is set (see Configuration).
2. `devin-provider serve` runs an OpenAI-compatible endpoint on `127.0.0.1:8787` (chat completions, streaming SSE, tool calls).
3. Hermes sees a custom provider named **Devin (Teams)**.

The design follows the `devin` provider in omp. Full design: `DESIGN.md`.

```
~/.local/share/devin/credentials.toml     (token source; sign in with the official CLI)
        │ token
        ▼
devin-provider serve  →  127.0.0.1:8787  (/v1/models, /v1/chat/completions)
        │ Connect-RPC over TLS (protobuf, uncompressed)
        ▼
server.codeium.com:443  (ApiServerService: GetChatMessage, GetCliModelConfigs)

~/.hermes/config.yaml   providers.devin:  api http://127.0.0.1:8787/v1, transport chat_completions
```

## Requirements

- Python 3.11+ as `python3` on `PATH`. The shim checks the version and refuses older interpreters, including the macOS system `/usr/bin/python3` (3.9). Homebrew's `/opt/homebrew/bin/python3` is an example of a newer one.
- The official `devin` CLI, for sign-in.
- Developed and tested on macOS arm64 only.

## Quick start

```sh
devin auth login                        # 1. sign in once (official CLI)
./install.sh                            # 2. symlink the CLI into ~/.local/bin and add the Hermes entry
devin-provider serve                    # 3. run the server (or: make launchd, see LaunchAgent)
curl -s http://127.0.0.1:8787/healthz   # optional check (send your key if DEVIN_PROVIDER_KEY is set)
hermes model                            # 4. pick "Devin (Teams)"
```

If `~/.local/bin` is not on your `PATH`, the installer prints the line to add.

## Commands

| Command | Purpose |
|---|---|
| `devin-provider serve [--host H] [--port P]` | Run the HTTP server. Defaults: `$DEVIN_PROVIDER_HOST` (or `127.0.0.1`) and `$DEVIN_PROVIDER_PORT` (or `8787`). |
| `devin-provider models [--all]` | List model selectors from the live catalog (needs a token). `--all` includes models the catalog marks disabled. |
| `devin-provider chat MODEL PROMPT [--max-tokens N] [--temperature T]` | Send one prompt (one-shot, for testing). Defaults: `--max-tokens` 8192, `--temperature` 1.0 (minimum 0.001). |
| `devin-provider status` | Show where the token comes from and the catalog size. Prints `token: not found` and exits 1 when no token is set. |
| `devin-provider login` | Run `devin auth login`, then verify the catalog. |
| `devin-provider version` | Print the version. |

## HTTP API

| Route | Notes |
|---|---|
| `GET /healthz` | `{"ok": true, "version": ...}`. With `DEVIN_PROVIDER_KEY` set, the request needs the bearer key (verified: 401 without it, 200 with it). |
| `GET /` | Service name and endpoint list. |
| `GET /v1/models` | Enabled catalog entries in OpenAI list shape. With no credentials: 401. |
| `POST /v1/chat/completions` | Designed for streaming (SSE) and non-streaming replies, `tools`, `stream_options.include_usage`, and `reasoning_content`. Not verified live in this sandbox. |

Errors are OpenAI-style JSON: `{"error": {"message", "type", "code"}}` with the mapped HTTP status (401 unauthenticated, 429 rate-limited, 502 upstream, 503 capacity, 504 timeout) — classifier order pinned by fixtures in `tests/test_protocol.py`.


Backend errors are designed to keep the backend's HTTP status and code. In this sandbox, this was not checked against the live backend.

Each request logs one line, `[devin-provider] METHOD PATH STATUS`. Chat requests add `model=` and `stream=`.

## Hermes setup

`install.sh` does the following:

1. Symlinks `bin/devin-provider` to `~/.local/bin/devin-provider`. What happens when that path already exists is untested. Check it first with `ls -l ~/.local/bin/devin-provider`.
2. Merges a `devin` entry under `providers:` in `$HERMES_HOME/config.yaml`:

```yaml
providers:
  devin:  # managed by scripts/install_hermes.py
    name: Devin (Teams)
    api: http://127.0.0.1:8787/v1
    transport: chat_completions
    models:
      - glm-5-2
      - glm-5-3-max
      # ... full live catalog (262 models) by default — see below
```

3. Prints next steps and a `devin-acp` note. `--cutover` also runs the old-patch removal (see Cutover). The `--cutover` path was not run through install.sh.

How the merge behaves (`scripts/install_hermes.py`):

- It edits the file as text, instead of re-serializing YAML.
- It backs up the config to `config.yaml.bak-devin-provider-<timestamp>` before writing.
- It is idempotent. A re-run changes only what differs.
- It is additive. A re-run adds back any starter missing from the entry, keeps models you added by hand, and removes nothing. A starter you deleted by hand comes back on the next run.
- The `models:` list is a **snapshot written at install time**, not a live view. Hermes never live-probes the devin entry
  (an explicit `models:` list with no `api_key` is skipped by the picker's probe policy by design — offline endpoints
  must not block the picker). New Devin models appear after re-running `scripts/install_hermes.py`: default pulls the
  live catalog from the provider server, `--live` requires it (exits non-zero if the server is down), `--offline` pins
  the 57-model starter snapshot.
- An inline `models:` value (for example `models: {}`) is replaced with the list form.
- If the config file is missing, it stops with an error.
- Flags: `--dry-run` prints the proposed edit and writes nothing. `--remove` deletes the `devin` entry. `--config`, `--api`, and `--transport` override the defaults (`http://127.0.0.1:8787/v1` and `chat_completions`).

Preview first:

```sh
python3 scripts/install_hermes.py --dry-run
```

## Models

The shipped entry carries the full live catalog (262 models, verified 2026-10-14) when the provider server is up at install time; the 57-entry starter snapshot is the offline fallback.
## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DEVIN_PROVIDER_HOST` | `127.0.0.1` | Bind address for `serve`. |
| `DEVIN_PROVIDER_PORT` | `8787` | Port for `serve`. |
| `DEVIN_PROVIDER_KEY` | unset | Optional bearer key (see below). |
| `DEVIN_CONNECT_TOKEN` | unset | Token override, checked first. |
| `WINDSURF_API_KEY` | unset | Token override, checked second. |
| `HERMES_HOME` | unset | Hermes home. The installer (`config.yaml`) and the cutover both use `$HERMES_HOME`, else `~/.hermes`. |

Token order: `DEVIN_CONNECT_TOKEN`, then `WINDSURF_API_KEY`, then `~/.local/share/devin/credentials.toml` (key `windsurf_api_key`). `devin-provider status` shows the source in use.

## Bearer key (optional)

Loopback needs no key. To require one:

```sh
export DEVIN_PROVIDER_KEY="$(openssl rand -hex 32)"
```

With the key set, requests need `Authorization: Bearer <key>`. Hermes must send the same key when it calls the provider. The installer does not configure that, and the Hermes-side setting has not been verified.

## Cutover: removing the old devin-acp patch

`scripts/remove_devin_acp.py` reverts the old source patch in `~/.hermes/hermes-agent`. Without `--apply` it only prints what it would do.

```sh
python3 scripts/remove_devin_acp.py       # dry run
make cutover                              # applies (runs --apply)
```

**Destructive:** `--apply` runs `git checkout --` on each modified file among these ten Hermes files, which discards uncommitted changes to them: `agent/agent_init.py`, `agent/agent_runtime_helpers.py`, `agent/auxiliary_client.py`, `agent/conversation_loop.py`, `hermes_cli/auth.py`, `hermes_cli/model_normalize.py`, `hermes_cli/model_switch.py`, `hermes_cli/models.py`, `hermes_cli/providers.py`, `hermes_cli/runtime_provider.py`.

Steps. Each one is skipped when already clean, so re-running is safe:

1. Restore the modified files above (`git checkout --`).
2. Delete `agent/devin_acp_client.py` if present.
3. Remove `~/.hermes/skills/autonomous-ai-agents/devin-acp-upgrade-reapply` if present.
4. Move `~/.hermes/devin-acp-reuse-bundle` to `legacy/devin-acp-reuse-bundle` in this repo, if present.
5. Syntax-check the restored files with Hermes' venv Python (`py_compile`), then list any leftover `devin_acp` / `devin-acp` references.

`auth.json`, caches, and the model cache are not touched.

## Troubleshooting

- **`devin-provider status` prints `token: not found`, or `/v1/models` returns 401**: no token is set. Run `devin auth login`, or set `DEVIN_CONNECT_TOKEN`.
- **Backend rejects the token (`unauthenticated`)**: sign in again with `devin auth login`, then run `devin-provider status` to confirm which token is in use.
- **Hermes gets 401 from the provider with `DEVIN_PROVIDER_KEY` set**: Hermes is not sending the key. The Hermes-side setting is not verified (see Bearer key).
- **Backend error `internal`**: the request was compressed. The provider sends uncompressed frames by design (per DESIGN.md). Do not add gzip.
- **Unknown or rejected model**: use a selector exactly as listed by `devin-provider models`.
- **"Devin (Teams)" missing from Hermes**: check the `devin` block in `$HERMES_HOME/config.yaml`, and preview the merge with `scripts/install_hermes.py --dry-run`.
- **Port already in use**: set `DEVIN_PROVIDER_PORT` or pass `serve --port`, then update the Hermes entry's `api` by hand. Re-running the installer with a changed `--api` is untested (see Known limitations).
- **LaunchAgent not running**: read `/tmp/devin-provider.log`, then run `launchctl print gui/$(id -u)/com.fabzter.devin-provider`.

## Security

- Tokens come from the environment or your Devin credentials file. By design they go only to `server.codeium.com` over TLS.
- The provider does not store credentials itself (DESIGN.md). Sign-in is the official CLI's job.
- The default bind is loopback. Binding elsewhere (`--host` or `DEVIN_PROVIDER_HOST`) is not tested. If you do it, set `DEVIN_PROVIDER_KEY` as well.
- Request logs carry the method, path, status, and for chat requests the model and stream flag.
- The local endpoint is plain HTTP. Keep it on loopback, or set `DEVIN_PROVIDER_KEY` if you bind elsewhere.

## Development

```sh
make test       # python3 -m tests.test_protocol
make serve      # python3 -m devin_api_provider serve
make install    # bash install.sh
make cutover    # python3 scripts/remove_devin_acp.py --apply
make launchd    # bash scripts/install_launchd.sh
```

Pure standard library. Nothing to install.

## LaunchAgent

`make launchd` runs `scripts/install_launchd.sh`. That script installs `launchd/com.fabzter.devin-provider.plist` into `~/Library/LaunchAgents` and loads it with `launchctl`. Not exercised in this sandbox verification.

- The agent runs `devin-provider serve` with `RunAtLoad` and `KeepAlive`. Standard output and error go to `/tmp/devin-provider.log`.
- The plist's `PATH` lists `/opt/homebrew/bin` first, so the agent's `python3` is Homebrew's.
- The repo plist uses the absolute path `/Users/fabzter/.local/bin/devin-provider`. Other home directories are not tested.
- A LaunchAgent does not see variables exported in your shell. To give the agent a value such as `DEVIN_PROVIDER_KEY`, it has to go in the plist. Re-running `install_launchd.sh` may overwrite a hand-edited installed copy (unverified).

## Layout

```
bin/devin-provider              CLI shim: checks python3 (3.11+), then runs python3 -m devin_api_provider
devin_api_provider/
  __init__.py, __main__.py      package and CLI entry point
  protocol.py                   Connect-RPC client for the Devin backend
  server.py                     OpenAI-compatible HTTP server (stdlib)
scripts/
  install_hermes.py             idempotent Hermes config merge (--dry-run, --remove)
  remove_devin_acp.py           cutover: revert the old devin-acp patch
  install_launchd.sh            install and load the LaunchAgent
launchd/com.fabzter.devin-provider.plist
tests/test_protocol.py
install.sh  Makefile  DESIGN.md  LICENSE  README.md
legacy/                         created by cutover; gitignored
```

## Known limitations

- `install_hermes.py --remove` leaves an empty `providers: {}` when `devin` was the only provider (seen in a dry run). Cosmetic.
- Re-runs are additive. Hand-added models are kept, and a starter removed by hand comes back.
- Re-running with a different `--api` or `--transport` is untested.
- `install_launchd.sh` was exercised for real on the author machine (agent active, healthz 200).
- Hermes acceptance verified live: the picker shows the devin row via `build_model_options_payload`, and chat completions route end-to-end (`hermes --provider devin --model …`).
- Binding to a non-loopback address is untested.
- Entitlement is enforced at chat time, not in the catalog: a listed model can still be rejected per-account tier at request time (e.g. `upgrade to access this model`). The catalog lists everything the backend serves.

## Status

Live-verified record (2026-10-14): chat completions non-stream and streaming SSE (role/reasoning/content/tool_call/finish/usage chunks), tool-call round-trips including parallel calls, the live 262-model catalog, Hermes end-to-end, the LaunchAgent, and the CSRF guards (403/415 probes). Offline: 68/68 tests.

**Still unverified:** non-loopback binds, re-running the installer with a changed `--api` against that custom endpoint, and per-model entitlement (the catalog ignores tier; rejection happens at chat time).
