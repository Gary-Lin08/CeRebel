"""`cerebel` command line: sign in, analyse a clip, ask the coach, run the glasses bridge, run mocks."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from cerebel.analysis import fix_summary
from cerebel.client import DEFAULT_BASE_URL, CerebelClient
from cerebel.errors import CerebelError, ConsentRequired

CREDENTIALS = Path(os.environ.get("CEREBEL_CREDENTIALS", str(Path.home() / ".config" / "cerebel" / "credentials.json")))


def _load_tokens() -> dict[str, str]:
    try:
        data = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_token(base_url: str, token: str | None) -> None:
    tokens = _load_tokens()
    if token:
        tokens[base_url] = token
    else:
        tokens.pop(base_url, None)
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDENTIALS.with_suffix(".tmp")
    # The token signs in as this account for 30 days: readable by this user only.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle, indent=2)
    tmp.replace(CREDENTIALS)


def _client(args: argparse.Namespace) -> CerebelClient:
    base = (args.base_url or os.environ.get("CEREBEL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    token = args.token or os.environ.get("CEREBEL_TOKEN") or _load_tokens().get(base)
    upload = args.upload_base_url or os.environ.get("CEREBEL_UPLOAD_BASE_URL") or None
    return CerebelClient(base, token, upload_base_url=upload)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_fix(finding: dict[str, Any] | None) -> None:
    if not finding:
        print("No fix to show for this clip.")
        return
    parts = fix_summary(finding)
    print(f"Main fix: {parts['headline']}")
    if parts["metric"]:
        print(f"  Measured: {parts['metric']} ({parts['metric_caption']})")
    for label in ("why", "cue", "drill", "check"):
        if parts[label]:
            print(f"  {label.capitalize()}: {parts[label]}")


# ---------------------------------------------------------------- commands


def cmd_login(args: argparse.Namespace) -> int:
    client = _client(args)
    email = args.email or input("Email: ").strip()
    password = os.environ.get("CEREBEL_PASSWORD") or getpass.getpass("Password: ")
    out = client.login(email, password)
    _save_token(client.base_url, client.token)
    print(f"Signed in to {client.base_url} as {out['user']['email']}.")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    client = _client(args)
    if client.token:
        try:
            client.logout()
        except CerebelError:
            pass
    _save_token(client.base_url, None)
    print("Signed out.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    me = _client(args).me()
    if not me.get("user"):
        print("Not signed in.")
        return 1
    quota = me.get("quota") or {}
    print(f"{me['user']['email']} · {quota.get('plan')} plan · {quota.get('remaining')} of {quota.get('monthly_limit')} analyses left this month")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    report = _client(args).health()
    _print_json(report)
    return 0 if report.get("ok") else 1


def cmd_consent(args: argparse.Namespace) -> int:
    client = _client(args)
    consent = client.me().get("video_consent") or {}
    if not consent:
        print("Sign in first: cerebel login")
        return 1
    print(consent.get("text", ""))
    print(f"\nVersion {consent.get('version')} · {'needs agreement' if consent.get('required') else 'agreed'}")
    if args.accept:
        client.accept_video_consent(consent.get("version"))
        print("Recorded your agreement.")
    elif consent.get("required"):
        print("Run `cerebel consent --accept` once you agree.")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    client = _client(args)

    def show(prog: dict[str, Any]) -> None:
        pct = prog.get("percent")
        line = f"{prog.get('stage')}" + (f" {pct}%" if pct is not None else "")
        if prog.get("queue_message"):
            line += f" · {prog['queue_message']}"
        for warning in prog.get("warnings") or []:
            print(f"note: {warning}", file=sys.stderr)
        print(line, file=sys.stderr)

    trim = tuple(args.trim) if args.trim else None
    try:
        result = client.analyze(args.video, sport=args.sport, handedness=args.handedness, trim=trim, on_progress=show)
    except ConsentRequired as exc:
        print(exc.message, file=sys.stderr)
        print("Read the text with `cerebel consent`, then agree with `cerebel consent --accept`.", file=sys.stderr)
        return 2
    if args.json:
        _print_json(result)
    else:
        print(f"Session {result['session_id']}")
        _print_fix(result["main_fix"])
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    _print_fix(_client(args).main_fix(args.session_id, sport=args.sport))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    client = _client(args)
    answer = ""
    for event in client.ask_stream(args.question, args.session):
        kind = event.get("type")
        if kind == "status" and args.verbose:
            print(f"… {event.get('message')}", file=sys.stderr)
        elif kind == "tool" and args.verbose:
            print(f"tool: {event.get('name')}", file=sys.stderr)
        elif kind == "error":
            print(f"error: {event.get('message')}", file=sys.stderr)
        elif kind == "done":
            answer = str(event.get("answer") or "")
    print(answer)
    return 0


def _glasses_sink(args: argparse.Namespace):
    from cerebel.glasses import CommandHudSink, ConsoleHudSink, GlassesLink, LinkHudSink

    if args.hud_command:
        return CommandHudSink(args.hud_command), None
    if args.glasses:
        host, _, port = args.glasses.partition(":")
        link = GlassesLink(host, int(port or 9080)).connect()
        return LinkHudSink(link), link
    return ConsoleHudSink(), None


def cmd_glasses_bridge(args: argparse.Namespace) -> int:
    from cerebel.glasses import GlassesBridge

    client = _client(args)
    sink, link = _glasses_sink(args)
    bridge = GlassesBridge(client, sink, sport=args.sport, lang=args.lang, fps=args.fps)
    try:
        if args.clip:
            result = bridge.process_clip(args.clip)
            _print_fix(result["main_fix"])
        else:
            print(f"Watching {args.watch} for new recordings (Ctrl+C to stop).", file=sys.stderr)
            bridge.watch(args.watch)
    except KeyboardInterrupt:
        pass
    finally:
        if link is not None:
            link.close()
    return 0


def cmd_glasses_hud(args: argparse.Namespace) -> int:
    from cerebel.glasses import GlassesLink

    host, _, port = args.glasses.partition(":")
    with GlassesLink(host, int(port or 9080), ping_interval=0) as link:
        link.send_hud(mode=args.mode, status=args.status, reply=args.reply, you=args.you)
    print("Sent.")
    return 0


def cmd_mock_api(args: argparse.Namespace) -> int:
    from cerebel.testing import MockCerebelServer

    server = MockCerebelServer(args.host, args.port, stage_seconds=args.stage_seconds)
    print(f"Mock Cerebel API on {server.url} (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


def cmd_mock_glasses(args: argparse.Namespace) -> int:
    from cerebel.glasses import MockGlasses

    glasses = MockGlasses(args.host, args.port, echo=True).start()
    print(f"Mock glasses on {glasses.host}:{glasses.port}. Type photo, rec_start or rec_stop to press a key.", file=sys.stderr)
    try:
        for line in sys.stdin:
            action = line.strip()
            if action:
                glasses.press(action)
    except KeyboardInterrupt:
        pass
    finally:
        glasses.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cerebel", description="Cerebel motion-analysis API from the command line.")
    parser.add_argument("--base-url", help=f"API address (env CEREBEL_BASE_URL, default {DEFAULT_BASE_URL})")
    parser.add_argument("--token", help="login token (env CEREBEL_TOKEN; `cerebel login` stores one)")
    parser.add_argument(
        "--upload-base-url",
        help="send uploads to this origin instead (env CEREBEL_UPLOAD_BASE_URL); needed when the API sits behind a proxy with a body limit",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="sign in and remember the token for this API address")
    p.add_argument("--email")
    p.set_defaults(func=cmd_login)
    sub.add_parser("logout", help="sign out and forget the token").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="account and remaining analyses").set_defaults(func=cmd_whoami)
    sub.add_parser("health", help="server readiness (exit 1 when not ready)").set_defaults(func=cmd_health)
    p = sub.add_parser("consent", help="show the video-processing text; --accept records agreement")
    p.add_argument("--accept", action="store_true")
    p.set_defaults(func=cmd_consent)

    p = sub.add_parser("analyze", help="upload a clip, wait, print the main fix")
    p.add_argument("video")
    p.add_argument("--sport", default="golf")
    p.add_argument("--handedness", default="right", choices=("right", "left"))
    p.add_argument("--trim", nargs=2, type=float, metavar=("START", "END"))
    p.add_argument("--json", action="store_true", help="print the whole result as JSON")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("fix", help="print the main fix of a processed capture")
    p.add_argument("session_id")
    p.add_argument("--sport", default="golf")
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("ask", help="ask the AI coach about a capture")
    p.add_argument("question")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="show tool calls")
    p.add_argument("--session", help="session id (default: your newest ready capture)")
    p.set_defaults(func=cmd_ask)

    g = sub.add_parser("glasses", help="Cerebel AI glasses tools").add_subparsers(dest="glasses_command", required=True)
    p = g.add_parser("bridge", help="analyse recordings and show the fix on the glasses")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--watch", help="folder where new recordings land")
    src.add_argument("--clip", help="analyse one recording and exit")
    p.add_argument("--glasses", help="HOST[:PORT] of the glasses' 9080 link")
    p.add_argument("--hud-command", nargs=argparse.REMAINDER, help='command to show text; "{text}" is replaced (must be last)')
    p.add_argument("--sport", default="golf")
    p.add_argument("--lang", default="zh", choices=("zh", "en"))
    p.add_argument("--fps", type=float, default=30.0, help="frame rate for raw .h264 recordings")
    p.set_defaults(func=cmd_glasses_bridge)
    p = g.add_parser("hud", help="send one HUD card over the 9080 link")
    p.add_argument("--glasses", required=True, help="HOST[:PORT]")
    p.add_argument("--mode", default="info", choices=("info", "assist", "fact", "end"))
    p.add_argument("--status", default="")
    p.add_argument("--reply", default="")
    p.add_argument("--you", default="")
    p.set_defaults(func=cmd_glasses_hud)

    m = sub.add_parser("mock", help="local stand-ins for development").add_subparsers(dest="mock_command", required=True)
    p = m.add_parser("api", help="fake Cerebel API (no GPU, no model)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--stage-seconds", type=float, default=2.0)
    p.set_defaults(func=cmd_mock_api)
    p = m.add_parser("glasses", help="fake glasses on the 9080 link; prints HUD cards")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9080)
    p.set_defaults(func=cmd_mock_glasses)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    try:
        return int(args.func(args) or 0)
    except CerebelError as exc:
        ref = f" (reference {exc.error_id})" if exc.error_id else ""
        print(f"error: {exc.message}{ref}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
