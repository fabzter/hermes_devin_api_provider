"""CLI entry point: `python3 -m devin_api_provider <command>`.

Commands: serve, login, status, models, chat, version. Wire details live in protocol.py
(DESIGN.md § protocol.py contract). This module parses arguments, prints results and maps
errors to exit codes; it never prints the session token.
"""

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence

from . import __version__, protocol
from .protocol import DevinError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _fail(exc: DevinError) -> int:
    """Print a DevinError as `error [code]: message` on stderr; return the error exit code."""
    print(f"error [{exc.code}]: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _cmd_serve(args: argparse.Namespace) -> int:
    # Lazy import: only `serve` needs the HTTP server, so the other commands stay import-light.
    from .server import run_server

    run_server(host=args.host, port=args.port)
    return EXIT_OK


def _cmd_login(args: argparse.Namespace) -> int:
    """Run the interactive `devin auth login`, then confirm the catalog answers with the new token."""
    try:
        completed = subprocess.run(["devin", "auth", "login"], check=False)
    except FileNotFoundError:
        print("error [unavailable]: `devin` not found on PATH — install Devin for Terminal first",
              file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"error [unavailable]: could not start `devin`: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR
    if completed.returncode != 0:
        print(f"error [upstream_error]: `devin auth login` exited with status {completed.returncode}",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        models = protocol.list_models(refresh=True)
    except DevinError as exc:
        return _fail(exc)
    print(f"logged in — catalog lists {len(models)} models")
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        token, source = protocol.find_credential()
    except DevinError as exc:
        print("token: not found")
        return _fail(exc)
    print(f"token: found (source: {source})")
    try:
        models = protocol.list_models(token=token)
    except DevinError as exc:
        return _fail(exc)
    enabled = sum(1 for item in models if item["enabled"])
    print(f"catalog: {len(models)} models ({enabled} enabled)")
    return EXIT_OK


def _cmd_models(args: argparse.Namespace) -> int:
    try:
        models = protocol.list_models()
    except DevinError as exc:
        return _fail(exc)
    for item in models:
        if args.all or item["enabled"]:
            print(f"{item['id']}\t{item['label']}\t{item['provider']}")
    return EXIT_OK


def _cmd_chat(args: argparse.Namespace) -> int:
    """One-shot prompt: deltas to stdout, reasoning and a usage summary to stderr."""
    token = protocol.get_token()
    stream = protocol.stream_chat(
        token,
        messages=[{"role": "user", "content": args.prompt}],
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )
    finish, prompt_tokens, completion_tokens = "unknown", "?", "?"
    in_reasoning = False
    last_char = ""
    for event in stream:
        kind = event.get("type")
        if kind == "reasoning":
            if not in_reasoning:
                sys.stderr.write("[reasoning] ")
                in_reasoning = True
            sys.stderr.write(str(event.get("content", "")))
            sys.stderr.flush()
            continue
        if in_reasoning:
            sys.stderr.write("\n")
            in_reasoning = False
        if kind == "delta":
            text = str(event.get("content", ""))
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
                last_char = text[-1]
        elif kind == "tool_call":
            print(f"[tool_call] {event.get('name')} {event.get('arguments')}", file=sys.stderr)
        elif kind == "finish":
            finish = event.get("reason", finish)
        elif kind == "usage":
            prompt_tokens = event.get("prompt_tokens", prompt_tokens)
            completion_tokens = event.get("completion_tokens", completion_tokens)
    if in_reasoning:
        sys.stderr.write("\n")
    if last_char and last_char != "\n":
        sys.stdout.write("\n")
    sys.stdout.flush()
    print(f"[usage] model={args.model} finish={finish} prompt_tokens={prompt_tokens} "
          f"completion_tokens={completion_tokens}", file=sys.stderr)
    return EXIT_OK


def _cmd_version(args: argparse.Namespace) -> int:
    print(__version__)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devin_api_provider",
        description="OpenAI-compatible local provider backed by the Devin (Teams) model catalog.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve = sub.add_parser("serve", help="run the local OpenAI-compatible HTTP server")
    serve.add_argument("--host", default=None,
                       help="bind address (default: $DEVIN_PROVIDER_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None,
                       help="bind port (default: $DEVIN_PROVIDER_PORT or 8787)")
    serve.set_defaults(func=_cmd_serve)

    login = sub.add_parser("login", help="run `devin auth login`, then verify the catalog")
    login.set_defaults(func=_cmd_login)

    status = sub.add_parser("status", help="show where the token comes from and the catalog size")
    status.set_defaults(func=_cmd_status)

    models = sub.add_parser("models", help="list models as selector<TAB>label<TAB>provider")
    models.add_argument("--all", action="store_true",
                        help="include models the catalog marks disabled")
    models.set_defaults(func=_cmd_models)

    chat = sub.add_parser("chat", help="send one prompt and stream the reply to stdout")
    chat.add_argument("model", help="model selector, e.g. glm-5-2")
    chat.add_argument("prompt", help="user prompt text")
    chat.add_argument("--max-tokens", type=int, default=None,
                      help=f"completion cap (default {protocol.DEFAULT_MAX_TOKENS})")
    chat.add_argument("--temperature", type=float, default=None,
                      help=f"sampling temperature (default {protocol.DEFAULT_TEMPERATURE}, "
                           f"floored at {protocol.MIN_TEMPERATURE})")
    chat.set_defaults(func=_cmd_chat)

    version = sub.add_parser("version", help="print the provider version")
    version.set_defaults(func=_cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args)
    except DevinError as exc:
        return _fail(exc)
    except BrokenPipeError:  # reader closed the pipe, e.g. `models | head -5`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
