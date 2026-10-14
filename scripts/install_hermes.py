#!/usr/bin/env python3
"""Merge (or remove) the Devin provider entry in Hermes' config.yaml. Idempotent.

Only the `devin` entry under the top-level `providers:` mapping is touched; every
other byte of the file (comments, ordering, quoting) is preserved. Stdlib only.
If PyYAML happens to be importable, the proposed file is also parsed as a check.

  python3 scripts/install_hermes.py [--dry-run] [--remove]
      [--config PATH] [--api URL] [--transport NAME]

Config: --config, else $HERMES_HOME/config.yaml, else ~/.hermes/config.yaml.
Exit: 0 written / already up to date / nothing to remove; 1 error (nothing written).
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
import json
import urllib.request

PROVIDER_KEY = "devin"
PROVIDER_NAME = "Devin (Teams)"
DEFAULT_API = "http://127.0.0.1:8787/v1"
DEFAULT_TRANSPORT = "chat_completions"  # Hermes api_mode for OpenAI-compatible chat endpoints
MARKER = "# managed by scripts/install_hermes.py"
API_ALIASES = ("api", "base_url", "url")  # Hermes spellings for the endpoint
TRANSPORT_ALIASES = ("transport", "api_mode")  # api_mode wins over transport when both are set
EMPTY_VALUES = ("", "{}", "[]", "null", "~")

# Starter catalog snapshot. Written as a list under `models:`, matching the live config.
# With --live (or when the provider server is up on 127.0.0.1:8787 without
# --offline), the full live catalog replaces this snapshot. Hand-added models
# survive both; hand-removed starters come back on rerun (additive installer).
MODEL_IDS = [
    "glm-5-2", "glm-5-3-max", "glm-5-3-high", "glm-5-3-low", "glm-5-3-flash-max",
    "claude-opus-5-5-max", "claude-opus-5-5-medium", "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high", "claude-5-fable-max", "claude-5-fable-medium",
    "claude-5-fable-high", "claude-haiku-5-5-medium", "claude-opus-4-8-max",
    "claude-opus-4-8-medium", "claude-opus-4-7-max", "claude-opus-4-7-medium",
    "claude-sonnet-4-6", "claude-sonnet-4-6-thinking", "claude-opus-4-6",
    "gpt-6-1-sol-max", "gpt-6-1-sol-xhigh", "gpt-6-1-sol-medium", "gpt-6-sol-max",
    "gpt-6-sol-medium", "gpt-6-astra-medium", "gpt-6-luna-medium", "gpt-5-6-sol-max",
    "gpt-5-6-sol-medium", "gpt-5-6-luna-medium", "gpt-5-5-xhigh", "gpt-5-5-medium",
    "gpt-5-5-low", "gpt-5-4-medium", "gpt-5-4-mini-medium", "gpt-5-3-codex-medium",
    "gemini-4-argon-medium", "gemini-3-8-flash-medium", "gemini-3-7-flash-medium",
    "gemini-3-6-flash-medium", "gemini-3-5-flash-medium", "gemini-3-1-pro-low",
    "kimi-k3-max", "kimi-k3-high", "kimi-k2-7", "swe-2-max", "swe-2-high",
    "swe-2-medium", "swe-1-7", "swe-1-7-lightning", "deepseek-v4-pro-max",
    "deepseek-v4-pro-high", "deepseek-v4-flash-max", "deepseek-v4-1-flash-high",
    "grok-4-7-xhigh", "grok-4-7-medium", "grok-4-5-high",
]
MODEL_SET = set(MODEL_IDS)
STRUCTURAL_KEYS = (frozenset({"providers", PROVIDER_KEY, "name", "models"})
                   | frozenset(API_ALIASES) | frozenset(TRANSPORT_ALIASES))

PROVIDERS_RE = re.compile(r"""(["']?)providers\1[ \t]*:(?=[ \t]|$)(.*)$""")
DEVIN_RE = re.compile(r"""(["']?)devin\1[ \t]*:(?=[ \t]|$)(.*)$""")
FIELD_RE = re.compile(r"""(["']?)([A-Za-z0-9_][\w.\-/]*)\1[ \t]*:(?=[ \t]|$)(.*)$""")
MODEL_KEY_RE = re.compile(r"""(["']?)([A-Za-z0-9][\w.\-/]*)\1[ \t]*:(?=[ \t]|$)""")
MODEL_ITEM_RE = re.compile(r"""-[ \t]+(["']?)([A-Za-z0-9][\w.\-/]*)\1[ \t]*(?:#.*)?$""")
REDACT_KEY_RE = re.compile(r"""^(\s*(?:-\s+)?)(["']?)([^"':\s]+)\2\s*:(.*)$""")
URL_RE = re.compile(r"https?://[\w.:/\-]+")
IDENT_RE = re.compile(r"[A-Za-z_][\w\-]*")
NAME_RE = re.compile(r"[\w .()\-]+")
DEVIN_ACP_RE = re.compile(r"devin[-_]acp", re.IGNORECASE)


class ConfigError(Exception):
    """A config shape this script refuses to edit. Nothing is written."""


# ---------------------------------------------------------------- text helpers

def split_lines(text: str) -> list[str]:
    """Split keeping EOLs, so untouched lines round-trip byte-for-byte."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def detect_eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def content(raw: str) -> str:
    return raw.rstrip("\r\n")


def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def is_blank(s: str) -> bool:
    return not s.strip()


def is_comment(s: str) -> bool:
    return s.lstrip().startswith("#")


def is_content(s: str) -> bool:
    return not is_blank(s) and not is_comment(s)


def value_of(rest: str) -> str:
    """Text after `key:` with any trailing comment removed."""
    return re.sub(r"\s+#.*$", "", rest).strip()


def comment_of(rest: str) -> str:
    m = re.search(r"\s(#.*)$", rest)
    return m.group(1) if m else ""


def suffix(comment: str) -> str:
    return f"  {comment}" if comment else ""


def unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def fix_eols(lines: list[str], eol: str) -> list[str]:
    """Every line except the last must end in an EOL once we append after it."""
    last = len(lines) - 1
    return [x if (x.endswith("\n") or i == last) else x + eol for i, x in enumerate(lines)]


# ------------------------------------------------------------- structure finder

def find_providers(lines: list[str]) -> tuple[int | None, str]:
    """Index and trailing text of the single top-level `providers:` line."""
    hits = []
    for i, raw in enumerate(lines):
        m = PROVIDERS_RE.match(content(raw))
        if m:
            hits.append((i, m.group(2)))
    if len(hits) > 1:
        raise ConfigError("more than one top-level `providers:` key; fix by hand")
    return hits[0] if hits else (None, "")


def providers_block(lines: list[str], hdr: int) -> tuple[int, int]:
    """(child indent, exclusive end index) of the providers block whose header is `hdr`."""
    end = len(lines)
    for j in range(hdr + 1, len(lines)):
        s = content(lines[j])
        if is_content(s) and indent_of(s) == 0:
            end = j
            break
    ci = 2
    for j in range(hdr + 1, end):
        s = content(lines[j])
        if is_content(s):
            ci = indent_of(s)
            break
    return ci, end


def find_entry(lines: list[str], hdr: int, end: int, ci: int) -> int | None:
    hits = [j for j in range(hdr + 1, end)
            if is_content(content(lines[j])) and indent_of(content(lines[j])) == ci
            and DEVIN_RE.match(content(lines[j])[ci:])]
    if len(hits) > 1:
        raise ConfigError("more than one `devin` entry under providers:; fix by hand")
    return hits[0] if hits else None


def entry_end(lines: list[str], start: int, end: int, ci: int) -> int:
    """Exclusive end of the devin entry starting at `start`. Trailing blanks and
    comments at the entry's own indent belong to the next sibling, not to us."""
    last = start
    for j in range(start + 1, end):
        s = content(lines[j])
        if is_blank(s) or (is_comment(s) and indent_of(s) <= ci):
            continue
        if indent_of(s) <= ci:
            break
        last = j
    return last + 1


def field_indent(lines: list[str], start: int, stop: int, ci: int) -> int:
    for j in range(start + 1, stop):
        s = content(lines[j])
        if is_content(s):
            return indent_of(s)
    return ci + 2


def field_units(lines: list[str], start: int, stop: int, fi: int) -> dict[str, list[tuple[int, int]]]:
    """Map field name -> [(first line, exclusive end)] for direct children at indent `fi`.
    A unit runs to the next key at `fi`, so same-indent list items stay with their key."""
    starts = [j for j in range(start + 1, stop)
              if is_content(content(lines[j])) and indent_of(content(lines[j])) == fi
              and FIELD_RE.match(content(lines[j])[fi:])]
    units: dict[str, list[tuple[int, int]]] = {}
    for n, j in enumerate(starts):
        nxt = starts[n + 1] if n + 1 < len(starts) else stop
        last = j + 1
        for k in range(j + 1, nxt):
            s = content(lines[k])
            if is_content(s) and indent_of(s) >= fi:
                last = k + 1
        key = FIELD_RE.match(content(lines[j])[fi:]).group(2)
        units.setdefault(key, []).append((j, last))
    return units


# ------------------------------------------------------------------ rendering

def fresh_entry(ci: int, eol: str, api: str, transport: str) -> list[str]:
    fi = ci + 2
    rows = [
        f"{' ' * ci}{PROVIDER_KEY}:  {MARKER}",
        f"{' ' * fi}name: {PROVIDER_NAME}",
        f"{' ' * fi}api: {api}",
        f"{' ' * fi}transport: {transport}",
        f"{' ' * fi}models:",
    ]
    rows += [f"{' ' * (fi + 2)}- {mid}" for mid in MODEL_IDS]
    return [row + eol for row in rows]


def merge_entry(lines: list[str], start: int, stop: int, ci: int, eol: str,
                api: str, transport: str) -> tuple[list[str], list[str]]:
    """New lines for the existing devin entry lines[start:stop], plus change notes.
    Only the fields we manage are rewritten; user-added fields and models are kept."""
    fi = field_indent(lines, start, stop, ci)
    pad_c, pad_f = " " * ci, " " * fi
    units = field_units(lines, start, stop, fi)
    replace: dict[int, tuple[int, list[str]]] = {}
    before: dict[int, list[str]] = {}
    changes: list[str] = []

    head_rest = DEVIN_RE.match(content(lines[start])[ci:]).group(2)
    head_value = value_of(head_rest)
    if head_value in EMPTY_VALUES and head_value:
        replace[start] = (start + 1, [f"{pad_c}{PROVIDER_KEY}:{suffix(comment_of(head_rest))}{eol}"])
    elif head_value:
        raise ConfigError("the devin entry is written inline; edit it by hand")

    def scalar(canon: str, aliases: tuple[str, ...], want: str) -> None:
        found = [(k, st, en) for k in aliases for (st, en) in units.get(k, [])]
        if not found:
            before.setdefault(start + 1, []).append(f"{pad_f}{canon}: {want}{eol}")
            changes.append(f"add {canon}")
            return
        for k, st, en in found:
            rest = FIELD_RE.match(content(lines[st])[fi:]).group(3)
            if unquote(value_of(rest)) != want:
                replace[st] = (en, [f"{pad_f}{k}: {want}{eol}"])
                changes.append(f"set {k}")

    scalar("name", ("name",), PROVIDER_NAME)
    scalar("api", API_ALIASES, api)
    scalar("transport", TRANSPORT_ALIASES, transport)

    hits = units.get("models", [])
    if len(hits) > 1:
        raise ConfigError("the devin entry has more than one `models:` key; fix by hand")
    if not hits:
        block = [f"{pad_f}models:{eol}"]
        block += [f"{' ' * (fi + 2)}- {mid}{eol}" for mid in MODEL_IDS]
        before.setdefault(start + 1, []).extend(block)
        changes.append(f"add {len(MODEL_IDS)} models")
    else:
        st, en = hits[0]
        rest_m = FIELD_RE.match(content(lines[st])[fi:]).group(3)
        m_value = value_of(rest_m)
        kids = [j for j in range(st + 1, en) if is_content(content(lines[j]))]
        if m_value not in EMPTY_VALUES or (m_value and kids):
            raise ConfigError("devin.models is written inline; edit it by hand")
        if m_value:
            replace[st] = (st + 1, [f"{pad_f}models:{suffix(comment_of(rest_m))}{eol}"])
        mi = indent_of(content(lines[kids[0]])) if kids else fi + 2
        kid_text = [content(lines[j])[mi:] for j in kids if indent_of(content(lines[j])) == mi]
        list_style = not kid_text or any(t.startswith("- ") for t in kid_text)
        have: set[str] = set()
        for t in kid_text:
            km = MODEL_ITEM_RE.match(t) if list_style else MODEL_KEY_RE.match(t)
            if km:
                have.add(km.group(2))
        missing = [mid for mid in MODEL_IDS if mid not in have]
        if missing:
            at = st + 1
            for k in range(st + 1, en):
                if is_content(content(lines[k])):
                    at = k + 1
            if list_style:
                new = [f"{' ' * mi}- {mid}{eol}" for mid in missing]
            else:
                new = [f"{' ' * mi}{mid}: {{}}{eol}" for mid in missing]
            before.setdefault(at, []).extend(new)
            changes.append(f"add {len(missing)} models")

    out: list[str] = []
    idx = start
    while True:
        out.extend(before.get(idx, []))
        if idx >= stop:
            break
        if idx in replace:
            nxt, new_lines = replace[idx]
            out.extend(new_lines)
            idx = nxt
        else:
            out.append(lines[idx])
            idx += 1
    return out, changes


# ------------------------------------------------------------------- planners

def plan_install(text: str, api: str, transport: str) -> tuple[str, str]:
    lines = split_lines(text)
    eol = detect_eol(text)
    hdr, rest = find_providers(lines)

    if hdr is None:
        body = list(lines)
        if body and not body[-1].endswith("\n"):
            body[-1] += eol
        if body and not is_blank(content(body[-1])):
            body.append(eol)
        body.append(f"providers:{eol}")
        body.extend(fresh_entry(2, eol, api, transport))
        return "".join(body), f"added providers: with devin entry ({len(MODEL_IDS)} models)"

    value = value_of(rest)
    if value not in EMPTY_VALUES:
        raise ConfigError("providers: is an inline mapping; add the devin entry by hand")
    head = lines[hdr] if value == "" else f"providers:{suffix(comment_of(rest))}{eol}"
    ci, end = providers_block(lines, hdr)
    s = find_entry(lines, hdr, end, ci)
    if s is None:
        new = lines[:hdr] + [head] + fresh_entry(ci, eol, api, transport) + lines[hdr + 1:]
        summary = f"added devin entry under providers: ({len(MODEL_IDS)} models)"
    else:
        e = entry_end(lines, s, end, ci)
        merged, changes = merge_entry(lines, s, e, ci, eol, api, transport)
        new = lines[:hdr] + [head] + lines[hdr + 1:s] + merged + lines[e:]
        summary = ("updated devin entry: " + "; ".join(changes)) if changes else "already up to date"
    return "".join(fix_eols(new, eol)), summary


def plan_remove(text: str) -> tuple[str, str]:
    lines = split_lines(text)
    eol = detect_eol(text)
    hdr, rest = find_providers(lines)
    if hdr is None:
        return text, "no providers: key; nothing to remove"
    value = value_of(rest)
    if value not in EMPTY_VALUES:
        raise ConfigError("providers: is an inline mapping; remove the devin entry by hand")
    ci, end = providers_block(lines, hdr)
    s = find_entry(lines, hdr, end, ci)
    if s is None:
        return text, "no devin entry; nothing to remove"
    e = entry_end(lines, s, end, ci)
    new = lines[:s] + lines[e:]
    _, end2 = providers_block(new, hdr)
    if not any(is_content(content(x)) for x in new[hdr + 1:end2]):
        new[hdr] = f"providers: {{}}{suffix(comment_of(rest))}{eol}"
    return "".join(fix_eols(new, eol)), f"removed devin entry ({e - s} lines)"


# ------------------------------------------------------------------ checks

def _without_devin(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "providers"}
    prov = doc.get("providers")
    if prov is None:
        out["providers"] = {}
    elif isinstance(prov, dict):
        out["providers"] = {k: v for k, v in prov.items() if k != PROVIDER_KEY}
    else:
        out["providers"] = prov
    return out


def yaml_verify(old: str, new: str, *, present: bool, api: str, transport: str) -> str:
    """Parse both texts if PyYAML is available and check the result. Raises ConfigError."""
    try:
        import yaml  # optional extra check; the text edit is correct without it
    except ImportError:
        return "skipped (PyYAML not installed)"
    try:
        doc_old = yaml.safe_load(old)
    except yaml.YAMLError:
        raise ConfigError("current config does not parse as YAML; refusing to edit") from None
    if doc_old is None:
        doc_old = {}
    try:
        doc_new = yaml.safe_load(new)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"proposed config does not parse as YAML{where}; nothing written") from None
    if doc_new is None:
        doc_new = {}
    if not isinstance(doc_old, dict) or not isinstance(doc_new, dict):
        raise ConfigError("config root is not a mapping; nothing written")
    prov = doc_new.get("providers") or {}
    if not isinstance(prov, dict):
        raise ConfigError("proposed providers: is not a mapping; nothing written")
    entry = prov.get(PROVIDER_KEY)
    if present:
        models = entry.get("models") if isinstance(entry, dict) else None
        if isinstance(models, dict):
            have = {str(k) for k in models}
        elif isinstance(models, list):
            have = {m for m in models if isinstance(m, str)}
        else:
            have = set()
        ok = (isinstance(entry, dict) and entry.get("name") == PROVIDER_NAME
              and any(entry.get(k) == api for k in API_ALIASES)
              and any(entry.get(k) == transport for k in TRANSPORT_ALIASES)
              and MODEL_SET <= have)
        if not ok:
            raise ConfigError("proposed devin entry failed verification; nothing written")
    elif entry is not None:
        raise ConfigError("devin entry still present after removal; nothing written")
    if _without_devin(doc_old) != _without_devin(doc_new):
        raise ConfigError("unrelated keys differ after the edit; nothing written")
    return "ok (parsed; unrelated keys unchanged)"


def acp_mentions(text: str) -> list[int]:
    return [n for n, raw in enumerate(split_lines(text), 1) if DEVIN_ACP_RE.search(raw)]


def _redact(body: str) -> str:
    """Show a diff line only when it is structural or a known safe value; mask the rest."""
    if not body.strip():
        return body
    m = REDACT_KEY_RE.match(body)
    if m:
        lead, _quote, key, rest = m.groups()
        val = value_of(rest)
        if not val:
            return body  # header line; its children are checked on their own lines
        if val == "{}" and (key in STRUCTURAL_KEYS or key in MODEL_SET):
            return body
        if key in API_ALIASES and URL_RE.fullmatch(val):
            return body
        if key in TRANSPORT_ALIASES and IDENT_RE.fullmatch(val):
            return body
        if key == "name" and NAME_RE.fullmatch(val):
            return body
        return f"{lead}{key}: ***"
    item = re.fullmatch(r"\s*-\s+(\S+)", body)
    if item and item.group(1) in MODEL_SET:
        return body
    return "  ***"


def show_diff(old: str, new: str, cfg: Path) -> None:
    diff = difflib.unified_diff(split_lines(old), split_lines(new),
                                fromfile=f"{cfg} (current)", tofile=f"{cfg} (proposed)",
                                n=0, lineterm="")
    for idx, raw in enumerate(diff):
        line = raw.rstrip("\r\n")
        if idx < 2 or line.startswith("@@"):
            print(line)
        elif line[:1] in ("-", "+", " "):
            print(line[0] + _redact(line[1:]))
        else:
            print(line)


def backup_path(cfg: Path) -> Path:
    base = f"{cfg.name}.bak-devin-provider-{datetime.now():%Y%m%d-%H%M%S}"
    cand, n = cfg.with_name(base), 1
    while cand.exists():
        cand = cfg.with_name(f"{base}-{n}")
        n += 1
    return cand


def atomic_write(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def default_config_path() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser() / "config.yaml"


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1



# ------------------------------------------------------------- live catalog

def _live_model_ids() -> list[str] | None:
    """Ask the provider server (127.0.0.1:8787) for the live catalog, enabled only.
    Returns None when the server is down or the answer is unusable — caller
    falls back to the starter snapshot."""
    api = DEFAULT_API.rsplit("/", 1)[0]  # http://127.0.0.1:8787
    try:
        with urllib.request.urlopen(f"{api}/v1/models", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return sorted(ids) if ids else None


def _resolve_models(live: bool, offline: bool) -> tuple[list[str], str]:
    """(model ids to write, source note). --live forces the server; default tries
    it opportunistically; --offline (or a down server) keeps the snapshot."""
    if not offline:
        ids = _live_model_ids()
        if ids is not None:
            if live or True:  # opportunistic: a reachable server always wins
                return ids, f"live catalog ({len(ids)} models)"
    return list(MODEL_IDS), f"starter snapshot ({len(MODEL_IDS)} models)"

# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge or remove the Devin provider in Hermes config.yaml.")
    ap.add_argument("--live", action="store_true",
                    help="pull the live catalog from the provider (262 models) instead of the starter snapshot")
    ap.add_argument("--offline", action="store_true",
                    help="never contact the provider; force the starter snapshot")
    ap.add_argument("--dry-run", action="store_true", help="print the proposed edit; write nothing")
    ap.add_argument("--remove", action="store_true", help="remove the devin provider entry")
    ap.add_argument("--config", type=Path, help="config file (default: $HERMES_HOME/config.yaml)")
    ap.add_argument("--api", default=DEFAULT_API, help=f"endpoint URL (default: {DEFAULT_API})")
    ap.add_argument("--transport", default=DEFAULT_TRANSPORT,
                    help=f"Hermes transport value (default: {DEFAULT_TRANSPORT})")
    args = ap.parse_args(argv)

    if not re.fullmatch(r"https?://[^\s'\"#]+", args.api):
        return _fail("--api must be an http(s) URL without spaces, quotes or '#'")
    if not re.fullmatch(r"[A-Za-z_][\w\-]*", args.transport):
        return _fail("--transport must be a bare identifier")

    cfg = (args.config or default_config_path()).expanduser()
    if not cfg.is_file():
        if args.remove:
            print(f"config:  {cfg} not found; nothing to remove")
            return 0
        return _fail(f"config not found: {cfg} (use --config or set HERMES_HOME)")
    cfg = cfg.resolve()  # write through symlinks instead of replacing them

    try:
        old_all = cfg.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return _fail("config is not valid UTF-8; nothing written")
    except OSError as exc:
        return _fail(f"cannot read config: {exc.strerror or type(exc).__name__}")
    bom = "\ufeff" if old_all.startswith("\ufeff") else ""
    old = old_all[len(bom):]

    models_note = ""
    if not args.remove:
        effective, models_note = _resolve_models(args.live, args.offline)
        MODEL_IDS[:] = effective
        MODEL_SET.clear()
        MODEL_SET.update(effective)

    try:
        if args.remove:
            new, summary = plan_remove(old)
        else:
            new, summary = plan_install(old, args.api, args.transport)
            summary = f"{summary}; {models_note}"
        verdict = yaml_verify(old, new, present=not args.remove,
                              api=args.api, transport=args.transport)
    except ConfigError as exc:
        return _fail(str(exc))
    new_all = bom + new

    print(f"config:  {cfg}")
    print(f"action:  {summary}")
    print(f"yaml:    {verdict}")
    acp = acp_mentions(old)
    if acp:
        shown = ", ".join(str(n) for n in acp[:20]) + (" ..." if len(acp) > 20 else "")
        print(f"note:    devin-acp referenced on line(s) {shown}; left untouched "
              "(see scripts/remove_devin_acp.py)")
    if new_all == old_all:
        print("result:  no changes needed")
        return 0
    if args.dry_run:
        show_diff(old, new, cfg)
        print("result:  dry run; nothing written")
        return 0

    backup = backup_path(cfg)
    try:
        shutil.copy2(cfg, backup)
        atomic_write(cfg, new_all)
    except OSError as exc:
        return _fail(f"write failed: {exc.strerror or type(exc).__name__}")
    print(f"backup:  {backup}")
    print("result:  written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
