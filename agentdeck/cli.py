import argparse
import json
import logging
import math
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .storage import load_json, state_dir, write_json


def current_status(runtime):
    try:
        state = load_json(runtime / "status.json", {})
    except (OSError, ValueError):
        raise SystemExit("Service status is invalid; check the service log") from None
    stamp = state.get("at")
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 5):
        raise SystemExit("Service is not running")
    sessions = state.get("sessions")
    if (not isinstance(sessions, list) or not all(
            isinstance(s, dict) and isinstance(s.get("provider"), str)
            and s["provider"] in {"claude", "codex"}
            and isinstance(s.get("state"), str)
            and s["state"] in {"working", "waiting", "ready", "idle", "unknown"}
            for s in sessions)):
        raise SystemExit("Service status is invalid; check the service log")
    return state


def main():
    parser = argparse.ArgumentParser(description="Busy Crew: Claude / Codex on the BUSY Bar front display")
    try:
        package_version = version("busy-crew")
    except PackageNotFoundError:
        package_version = "development"
    parser.add_argument("--version", action="version", version="%(prog)s " + package_version)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    install = sub.add_parser("install")
    install.add_argument("--address", help="BUSY Bar URL; preserve existing address on reinstall")
    install.add_argument("--timezone", help="IANA time zone; preserve existing zone on reinstall")
    install.add_argument("--no-claude-hooks", action="store_true")
    sub.add_parser("uninstall")
    menubar = sub.add_parser("menubar", help="optional macOS menu bar status icon")
    menubar.add_argument("action", choices=["install", "uninstall"])
    sub.add_parser("status")
    sub.add_parser("ack")
    sub.add_parser("refresh", help="refresh token totals now instead of within 2 minutes")
    view = sub.add_parser("view"); view.add_argument("name", choices=["sessions", "tokens", "month"])
    shot = sub.add_parser("snapshot"); shot.add_argument("path", type=Path)
    style = shot.add_mutually_exclusive_group()
    style.add_argument("--leds", action="store_true", help="round LEDs on a dark panel")
    style.add_argument("--tiles", nargs="?", const="dark", choices=["dark", "light"],
                       help="rounded tiles on a transparent ground, for a dark (default) or light page")
    demo = sub.add_parser("demo"); demo.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        from .service import Service, configure_logging
        runtime = state_dir()
        handler = configure_logging(runtime)
        try:
            Service().run()
        except Exception as exc:
            if handler not in logging.getLogger("agentdeck").handlers:
                handler = configure_logging(runtime)
            logging.getLogger("agentdeck").error("Service failed: %s", type(exc).__name__)
            raise SystemExit("Service failed; check the private service.log") from None
        finally:
            logger = logging.getLogger("agentdeck")
            if handler in logger.handlers:
                logger.removeHandler(handler)
                handler.close()
    elif args.command == "install":
        from .install import install
        install(args.address, args.timezone, not args.no_claude_hooks)
    elif args.command == "uninstall":
        from .install import uninstall
        uninstall()
    elif args.command == "menubar":
        from .install import install_menubar, uninstall_menubar
        if args.action == "install":
            install_menubar()
        else:
            uninstall_menubar()
    elif args.command == "status":
        state = current_status(state_dir())
        # CLI output intentionally omits session IDs.
        state["sessions"] = [{"provider": s["provider"], "state": s["state"]} for s in state["sessions"]]
        print(json.dumps(state, indent=2))
    elif args.command in {"view", "ack", "refresh"}:
        runtime = state_dir()
        current_status(runtime)
        if sum(1 for _ in (runtime / "inbox").glob("*.json")) >= 128:
            raise SystemExit("Command queue is full; retry after the service catches up")
        write_json(runtime / "inbox" / (str(uuid.uuid4()) + ".json"),
                   {"command": args.name if args.command == "view" else args.command})
    elif args.command == "snapshot":
        from .device import Device
        from PIL import Image, ImageDraw
        try:
            config = load_json(state_dir() / "config.json", {})
        except (OSError, ValueError):
            raise SystemExit("Configuration is invalid; check config.json") from None
        rgb = Device(config.get("address", "http://busybar.local"), config.get("token")).screen()
        image = Image.frombytes("RGB", (72, 16), rgb)
        if args.tiles:
            from .render import tiles
            image = tiles(image, ground=args.tiles)
        elif args.leds:
            result = Image.new("RGB", (1008, 224), "#070b10")
            draw = ImageDraw.Draw(result)
            for y in range(16):
                for x in range(72):
                    color = image.getpixel((x, y))
                    draw.ellipse((x*14+2, y*14+2, x*14+11, y*14+11),
                                 fill=color if any(color) else "#141b24")
            image = result
        image.save(args.path)
    elif args.command == "demo":
        from .model import Session
        from .render import render
        args.directory.mkdir(parents=True, exist_ok=True)
        cases = {
            "duo": [("claude", "working")] * 3 + [("claude", "waiting"), ("codex", "working"), ("codex", "ready")],
            "claude-team": [("claude", "working")] * 3 + [("claude", "waiting"), ("claude", "ready")],
            "codex-team": [("codex", "working")] * 3 + [("codex", "waiting"), ("codex", "ready")],
            "idle-duo": [("claude", "idle"), ("codex", "working")],
        }
        for name, items in cases.items():
            sessions = [Session(provider, str(i), state, "demo") for i, (provider, state) in enumerate(items)]
            render(sessions).save(args.directory / (name + ".png"), scale=14, leds=True)
        print("Generated fictional examples; nothing was sent to the device")


if __name__ == "__main__":
    main()
