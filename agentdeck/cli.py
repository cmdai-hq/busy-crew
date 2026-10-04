import argparse
import json
import logging
import time
import uuid
from pathlib import Path

from .storage import read_json, state_dir, write_json


def main():
    parser = argparse.ArgumentParser(description="Claude / Codex on the BUSY Bar front display")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    install = sub.add_parser("install")
    install.add_argument("--address", default="http://10.0.4.20")
    install.add_argument("--timezone", default="Europe/Paris")
    install.add_argument("--no-claude-hooks", action="store_true")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    sub.add_parser("ack")
    view = sub.add_parser("view"); view.add_argument("name", choices=["sessions", "tokens"])
    shot = sub.add_parser("snapshot"); shot.add_argument("path", type=Path)
    shot.add_argument("--leds", action="store_true")
    demo = sub.add_parser("demo"); demo.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        from .service import Service
        Service().run()
    elif args.command == "install":
        from .install import install
        install(args.address, args.timezone, not args.no_claude_hooks)
    elif args.command == "uninstall":
        from .install import uninstall
        uninstall()
    elif args.command == "status":
        state = read_json(state_dir() / "status.json", {})
        if not state or time.time() - state.get("at", 0) > 5:
            raise SystemExit("Service is not running")
        # CLI output intentionally omits session IDs.
        state["sessions"] = [{"provider": s["provider"], "state": s["state"]} for s in state["sessions"]]
        print(json.dumps(state, indent=2))
    elif args.command in {"view", "ack"}:
        state = read_json(state_dir() / "status.json", {})
        if not state or time.time() - state.get("at", 0) > 5:
            raise SystemExit("Service is not running")
        write_json(state_dir() / "inbox" / (str(uuid.uuid4()) + ".json"),
                   {"command": args.name if args.command == "view" else "ack"})
    elif args.command == "snapshot":
        from .device import Device
        from PIL import Image, ImageDraw
        config = read_json(state_dir() / "config.json", {})
        rgb = Device(config.get("address", "http://10.0.4.20"), config.get("token")).screen()
        image = Image.frombytes("RGB", (72, 16), rgb)
        if args.leds:
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
        }
        for name, items in cases.items():
            sessions = [Session(provider, str(i), state, "demo") for i, (provider, state) in enumerate(items)]
            render(sessions).save(args.directory / (name + ".png"), scale=14, leds=True)
        print("Generated fictional examples; nothing was sent to the device")


if __name__ == "__main__":
    main()
