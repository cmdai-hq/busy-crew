"""Hand drawn sprites at the front panel's real 72 × 16 resolution."""
from .model import ORDER, aggregate, layout

PALETTE = {".": "#000000", "c": "#dc866b", "B": "#7895ff", "s": "#4669c9",
           "v": "#192b50", "C": "#8cf1e9", "m": "#8cf1e9", "y": "#ffcb64",
           "g": "#87dd9b", "d": "#65717e", "w": "#dfe9ee"}
CLAWD = ["0011111111100", "0011111111100", "0011011101100", "1111011101111",
         "1111111111111", "0011111111100", "0011111111100", "0001100011000", "0001000001000"]
MINI_CLAWD = ["011111110", "011111110", "110111011", "110111011", "111111111", "011111110", "010101010"]
CODEX = ["000BB0BB00000", "00BBBBBBBBB00", "0BBBBBBBBBBB0", "BBBvvvvvvvBBB",
         "BBvvvvvvvvvBB", "BBvvCvvvCvvBB", "BBvvCvvvCvvBB", "0BBvvvvvvvBB0",
         "00BBBBBBBBB00", "000BBBmBBB000", "000sBBBBBs000", "000BB000BB000", "000ss000ss000"]
MINI_CODEX = ["00BB0BB00", "0BBBBBBB0", "BBBvvvBBB", "BBvCvCvBB", "BBvvvvvBB",
              "0BBBBBBB0", "00BBmBB00", "00BBBBB00", "00ss0ss00"]
# Dimming paints sprites gray. Codex's painted visor and mouth ("m", cyan like the
# eyes when lit) become holes, so the eyes stand out on the visor and the mouth on the body.
DIM = {"v": ".", "m": "."}
INK_ON_LIGHT = (36, 41, 47)  # White pixels drawn on a light page.
FONT = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
    "+": ["000", "010", "111", "010", "000"], "?": ["111", "001", "010", "000", "010"],
    "K": ["101", "101", "110", "101", "101"], "M": ["101", "111", "111", "101", "101"],
    "G": ["111", "100", "101", "101", "111"], "T": ["111", "010", "010", "010", "010"],
    "z": ["11", "01", "10", "11"],
    ".": ["0", "0", "0", "0", "1"], "-": ["000", "000", "111", "000", "000"],
    # Letters for the token period labels.
    "A": ["010", "101", "111", "101", "101"], "D": ["110", "101", "101", "101", "110"],
    "H": ["101", "101", "111", "101", "101"], "N": ["1001", "1101", "1011", "1001", "1001"],
    "O": ["010", "101", "101", "101", "010"], "Y": ["101", "101", "010", "010", "010"],
}
PERIODS = {"tokens": "TODAY", "month": "MONTH"}
MARKS = {"working": ["110", "110", "001"], "waiting": ["010", "010", "000", "010"],
         "ready": ["001", "101", "010"], "unknown": FONT["?"], "idle": ["000", "111", "000"]}


class Frame:
    def __init__(self):
        self.rows = [["."] * 72 for _ in range(16)]

    def dot(self, x, y, color):
        if 0 <= x < 72 and 0 <= y < 16:
            self.rows[y][x] = color

    def sprite(self, rows, x, y, color=None, dim=False):
        for dy, row in enumerate(rows):
            for dx, value in enumerate(row):
                if value != "0":
                    self.dot(x + dx, y + dy, DIM.get(value, "d") if dim else (color or value))

    def text(self, text, x, y, color="w", scale=1):
        for ch in text:
            glyph = FONT.get(ch, FONT["?"])
            for dy, row in enumerate(glyph):
                for dx, bit in enumerate(row):
                    if bit == "1":
                        for a in range(scale):
                            for b in range(scale):
                                self.dot(x + dx * scale + a, y + dy * scale + b, color)
            x += (len(glyph[0]) + 1) * scale

    def mark(self, state, provider, x, y, sleeping=False):
        if sleeping:
            self.text("zzz", x, y, "d")
            return
        color = {"waiting": "y", "ready": "g", "idle": "d", "unknown": "d"}.get(state)
        self.sprite(MARKS[state], x, y, color or ("c" if provider == "claude" else "B"))

    def xpm(self):
        used = sorted({v for row in self.rows for v in row})
        return "! XPM2\n72 16 %s 1\n%s\n%s\n" % (
            len(used), "\n".join(f"{v} c {PALETTE[v]}" for v in used),
            "\n".join("".join(row) for row in self.rows))

    def rgb(self):
        return bytes(component for row in self.rows for v in row
                     for component in bytes.fromhex(PALETTE[v][1:]))

    def save(self, path, scale=1, leds=False, tiled=None):
        from PIL import Image, ImageDraw
        img = Image.frombytes("RGB", (72, 16), self.rgb())
        if tiled:  # "dark" or "light": the page the tiles will sit on.
            img = tiles(img, scale if scale > 1 else 20, tiled)
        elif leds:
            img = Image.new("RGB", (72 * scale, 16 * scale), "#070b10")
            draw = ImageDraw.Draw(img)
            for y, row in enumerate(self.rows):
                for x, value in enumerate(row):
                    color = PALETTE[value] if value != "." else "#141b24"
                    draw.ellipse((x*scale+2, y*scale+2, (x+1)*scale-3, (y+1)*scale-3), fill=color)
        elif scale != 1:
            img = img.resize((72 * scale, 16 * scale), Image.Resampling.NEAREST)
        img.save(path)


def tiles(image, cell=20, ground="dark"):
    """Each lit pixel as a rounded tile on a transparent ground, the way the LEDs read on the bar.

    White text vanishes on a light page, so ground="light" draws white pixels in dark ink.
    """
    from PIL import Image, ImageDraw
    result = Image.new("RGBA", (image.width * cell, image.height * cell), (0, 0, 0, 0))
    draw = ImageDraw.Draw(result)
    gap, radius = max(1, cell // 6), max(1, cell // 5)
    for y in range(image.height):
        for x in range(image.width):
            color = image.getpixel((x, y))[:3]
            if ground == "light" and min(color) >= 200:
                color = INK_ON_LIGHT
            if any(color):  # An unlit LED stays transparent.
                draw.rounded_rectangle((x * cell + gap, y * cell + gap, (x + 1) * cell - gap - 1, (y + 1) * cell - gap - 1),
                                       radius=radius, fill=color)
    return result


def number(n):
    return str(n) if n < 100 else "99+"


def compact(n):
    if n is None:
        return "?"
    if type(n) is not int or n < 0:
        return "?"
    for unit, divisor in [("T", 10**12), ("G", 10**9), ("M", 10**6)]:
        if n >= divisor:
            if n >= 1000 * divisor and unit == "T":
                return "999T+"
            if n < 100 * divisor:
                # Integer rounding also handles counts larger than float's range.
                tenths = (n * 10 + divisor // 2) // divisor
                return f"{tenths // 10}.{tenths % 10}{unit}"
            return f"{n // divisor}{unit}"
    return f"{n // 1000}K" if n >= 1000 else str(n)


def ordered_sessions(sessions):
    return sorted(sessions, key=lambda s: (ORDER[s.state], s.id))


def visible_sessions(sessions, view="sessions", errors=()):
    """Results represented by tiles, state dots or an aggregate completion."""
    if view in PERIODS and not any(s.state == "waiting" for s in sessions):
        return []
    mode, provider = layout(sessions, errors)
    if mode == "team":
        group = ordered_sessions([s for s in sessions if s.provider == provider])
        return group[:5 if len(group) > 6 else 6]
    visible = []
    for name in ["claude", "codex"]:
        group = ordered_sessions([s for s in sessions if s.provider == name])
        # A green aggregate and the total represent all this family's results;
        # otherwise only the four individual state dots expose completions.
        visible.extend(group if name not in errors and aggregate(group) == "ready" else group[:4])
    return visible


def render(sessions, view="sessions", tokens=None, phase=0, errors=()):
    frame = Frame()
    mode, provider = layout(sessions, errors)
    claude = [s for s in sessions if s.provider == "claude"]
    sleeping = bool(claude) and "claude" not in errors and all(s.state == "idle" for s in claude)
    # Required input always takes precedence over the optional token view.
    if view in PERIODS and not any(s.state == "waiting" for s in sessions):
        for name, x in [("claude", 2), ("codex", 39)]:
            frame.sprite(MINI_CLAWD if name == "claude" else MINI_CODEX, x, 1,
                         "c" if name == "claude" else None)
            frame.text(compact((tokens or {}).get(name)), x + 11, 3)
        frame.dot(35, 3, "d"); frame.dot(35, 6, "d")
        label = PERIODS[view]
        width = sum(len(FONT[ch][0]) + 1 for ch in label) - 1
        frame.text(label, (72 - width) // 2, 11, "d")
    elif mode == "duo":
        for name, x in [("claude", 0), ("codex", 37)]:
            group = [s for s in sessions if s.provider == name]
            count = len(group)
            sprite = CLAWD if name == "claude" else CODEX
            # Centered vertically on each other: Clawd 3–11, the taller Codex 1–13.
            frame.sprite(sprite, x + 2, 3 if name == "claude" else 1,
                         "c" if name == "claude" else None,
                         dim=all(s.state in {"idle", "unknown"} for s in group))
            label = "?" if name in errors else number(count)
            frame.text(label, x + (19 if len(label) == 1 else 16), 3, scale=2 if len(label) == 1 else 1)
            state = "unknown" if name in errors else aggregate(group)
            is_sleeping = sleeping and name == "claude"
            frame.mark(state, name, x + (27 if is_sleeping else 28), 3, sleeping=is_sleeping)
            for i, session in enumerate(ordered_sessions(group)[:4]):
                color = {"waiting": "y", "ready": "g", "idle": "d", "unknown": "d"}.get(session.state, "c" if name == "claude" else "B")
                frame.dot(x + 17 + i * 4, 14, color); frame.dot(x + 18 + i * 4, 14, color)
        for y in [4, 7, 10]: frame.dot(35, y, "d")
    else:
        group = ordered_sessions([s for s in sessions if s.provider == provider])
        # Six tiles maximum. The last becomes a numeric overflow tile, never a fake agent.
        overflow = len(group) > 6
        visible = group[:5 if overflow else 6]
        count = len(visible)
        xs = [2 + i * 12 for i in range(6)] if overflow else [int(36 - (count * 12)/2 + i*12 + 1) for i in range(count)]
        for x, session in zip(xs, visible):
            frame.sprite(MINI_CLAWD if provider == "claude" else MINI_CODEX, x, 7 if provider == "claude" else 5,
                         "c" if provider == "claude" else None, dim=session.state in {"idle", "unknown"})
            frame.mark(session.state, provider, x if sleeping else x + 3, 0, sleeping=sleeping)
            if session.state == "working":
                frame.dot(x + 3 + phase * 2, 15, "c" if provider == "claude" else "B")
        if overflow:
            hidden = group[5:]
            frame.text("+", 62, 2, "y" if any(s.state == "waiting" for s in hidden) else "w")
            frame.text(number(len(hidden)), 60, 9)
    if errors:
        frame.text("?", 69, 11, "d")
    return frame
