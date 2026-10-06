"""Build Fumi, the llm-mailroom mascot.

Fumi is a chibi pixel maid who works the mailroom. Her body is a native-
resolution pixel sprite (docs/assets/mascot/source/fumi-base.png, 62x107,
cleaned from reference art supplied by the project owner); this script adds
the mailroom props (the letter in her hands, her owl assistant Hoot, the
winged envelope Tegami), the blink frames, and the animation, then emits:

  docs/assets/mascot/fumi.svg        animated SVG (CSS keyframes, no JS)
  docs/assets/mascot/fumi.gif        animated GIF (same timeline)
  docs/assets/mascot/fumi.png        static PNG, 6x scale
  docs/assets/mascot/fumi-icon.png   square head-and-shoulders icon (favicons, avatars)
  docs/assets/mascot/fumi-sheet.png  every GIF frame side by side

Run:  python src/scripts/build_mascot.py   (needs Pillow)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "docs" / "assets" / "mascot"
BASE = OUT_DIR / "source" / "fumi-base.png"

W, H = 96, 116          # canvas, in sprite pixels
SX, SY = 17, 6          # where the base sprite sits on the canvas
CX = SX + 30.5

# ---------------------------------------------------------------- palette
OUT = "#0f1433"
SKIN = "#ffd3b4"
LASH = "#0f1433"
ENV, ENV_D = "#fff8e6", "#e6d3a8"
SEAL, SEAL_L = "#e8445e", "#ff8a9c"
BLUSH = "#ff9db5"
WING, WING_D = "#ffffff", "#cfd8ff"
SPARK = "#ffd34d"
OWL = {"K": "#3a2a24", "b": "#8a6a4a", "d": "#6b4f36", "f": "#f1e3c6", "c": "#c9b08a",
       "E": "#2a1d1a", "w": "#ffffff", "O": "#e8a33a", "y": "#e8a33a"}


class Layer:
    def __init__(self) -> None:
        self.px: dict[tuple[int, int], str] = {}

    def set(self, x: int, y: int, c: str) -> None:
        if -2 <= x < W + 2 and -2 <= y < H + 2:
            self.px[(x, y)] = c

    def pts(self, pts, c: str) -> None:
        for x, y in pts:
            self.set(x, y, c)

    def sym(self, pts, c: str) -> None:
        """Set points and their mirror image across the character's axis."""
        for x, y in pts:
            self.set(x, y, c)
            self.set(int(2 * CX - x), y, c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: str) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.set(x, y, c)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, c: str, where=None) -> None:
        for y in range(H + 2):
            for x in range(W):
                if ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2 <= 1:
                    if where is None or where(x, y):
                        self.set(x, y, c)

    def sprite(self, x0: int, y0: int, rows, cmap, mirror: bool = False) -> None:
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch != ".":
                    x = x0 + (len(row) - 1 - dx if mirror else dx)
                    self.set(x, y0 + dy, cmap[ch])

    def outline(self, c: str = OUT) -> None:
        """Add a 1px outline around every filled pixel (4-neighbourhood)."""
        add = set()
        for x, y in self.px:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (x + dx, y + dy) not in self.px:
                    add.add((x + dx, y + dy))
        for x, y in add:
            self.set(x, y, c)

    def merge(self, other: "Layer") -> None:
        self.px.update(other.px)


# ---------------------------------------------------------------- Fumi
def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb[:3])


def draw_body() -> Layer:
    """The base sprite plus the letter she holds and Hoot on her shoulder."""
    L = Layer()
    base = Image.open(BASE).convert("RGBA")
    for y in range(base.height):
        for x in range(base.width):
            r, g, b, a = base.getpixel((x, y))
            if a:
                L.set(SX + x, SY + y, _hex((r, g, b)))

    # a heart-sealed letter held in her clasped hands
    env = Layer()
    ex, ey = SX + 21, SY + 69
    env.rect(ex, ey, ex + 14, ey + 9, ENV)
    env.outline(OUT)
    for i in range(6):
        env.set(ex + 1 + i, ey + 1 + i, ENV_D)
        env.set(ex + 13 - i, ey + 1 + i, ENV_D)
    heart = [".S.S.", "SLSSS", ".SSS.", "..S.."]
    env.sprite(ex + 5, ey + 5, heart, {"S": SEAL, "L": SEAL_L})
    L.merge(env)

    # Hoot, the owl assistant, perched on her shoulder
    L.sprite(SX + OWL_X, SY + OWL_Y, OWL_ROWS, OWL)
    return L


OWL_X, OWL_Y = 46, 49
OWL_ROWS = [
    ".K.........K.",
    ".KbK.....KbK.",
    ".KbbKKKKKbbK.",
    "KbbbbbbbbbbbK",
    "KbffffbffffbK",
    "KffEwfffEwffK",
    "KffEEfffEEffK",
    "KdbfffOfffbdK",
    "KdbcbcbcbcbdK",
    ".KdbcbcbcbdK.",
    ".KdbbbbbbbdK.",
    "..KKyKKKyKK..",
]
OWL_EYES_CLOSED = {(3, 5): "f", (4, 5): "f", (8, 5): "f", (9, 5): "f",
                   (3, 6): "E", (4, 6): "E", (8, 6): "E", (9, 6): "E"}

# eye boxes in base-sprite coordinates (x0, x1, y0, y1)
EYE_BOXES = [(15, 22, 37, 44), (34, 42, 37, 44)]


def draw_eyes(closed: bool) -> Layer:
    """Open eyes come from the base sprite; this layer only paints the blink."""
    L = Layer()
    if not closed:
        return L
    for x0, x1, y0, y1 in EYE_BOXES:
        L.rect(SX + x0, SY + y0, SX + x1, SY + y1, SKIN)
        n = x1 - x0
        for i in range(n + 1):
            dip = 2 if 1 < i < n - 1 else (1 if i in (1, n - 1) else 0)
            L.set(SX + x0 + i, SY + 41 + dip, LASH)
    L.pts([(SX + 14, SY + 40), (SX + 43, SY + 40)], LASH)
    for (x, y), ch in OWL_EYES_CLOSED.items():
        L.set(SX + OWL_X + x, SY + OWL_Y + y, OWL[ch])
    return L


# ---------------------------------------------------------------- buddy
BUDDY_X, BUDDY_Y = 80, 30


def draw_buddy(wings_up: bool) -> Layer:
    L = Layer()
    x0, y0 = BUDDY_X, BUDDY_Y
    if wings_up:
        lw = [(x0 - 3, y0 - 2), (x0 - 2, y0 - 2), (x0 - 4, y0 - 1), (x0 - 3, y0 - 1),
              (x0 - 2, y0 - 1), (x0 - 1, y0), (x0 - 2, y0), (x0 - 1, y0 + 1)]
    else:
        lw = [(x0 - 1, y0 + 3), (x0 - 2, y0 + 3), (x0 - 3, y0 + 4), (x0 - 2, y0 + 4),
              (x0 - 4, y0 + 5), (x0 - 3, y0 + 5), (x0 - 1, y0 + 4), (x0 - 1, y0 + 2)]
    rw = [(2 * x0 + 9 - x, y) for x, y in lw]
    L.pts(lw, WING)
    L.pts(rw, WING)
    L.rect(x0, y0, x0 + 9, y0 + 6, ENV)
    L.outline()
    for i in range(5):
        L.set(x0 + i, y0 + i, ENV_D)
        L.set(x0 + 9 - i, y0 + i, ENV_D)
    L.pts([(x0 + 4, y0 + 4), (x0 + 5, y0 + 4)], SEAL)
    L.pts([(x0 + 4, y0 + 3), (x0 + 5, y0 + 3)], SEAL_L)
    L.pts([(x0 + 2, y0 + 5), (x0 + 7, y0 + 5)], LASH)
    L.pts([(x0 + 1, y0 + 6), (x0 + 8, y0 + 6)], BLUSH)
    for p in lw[:2] + rw[:2]:
        L.set(*p, WING_D)
    return L


# ---------------------------------------------------------------- extras
BUBBLE_X, BUBBLE_Y = 3, 14


def draw_bubble() -> Layer:
    """Little speech bubble with a pixel heart, up and to the left of her head."""
    L = Layer()
    x0, y0 = BUBBLE_X, BUBBLE_Y
    L.rect(x0, y0, x0 + 10, y0 + 7, "#ffffff")
    L.pts([(x0 + 8, y0 + 8), (x0 + 9, y0 + 8), (x0 + 9, y0 + 9)], "#ffffff")
    L.outline()
    heart = ["SS.SS..", "SSSSSS."]
    heart = [".SS.SS.", "SSSSSSS", "SSSSSSS", ".SSSSS.", "..SSS..", "...S..."]
    L.sprite(x0 + 2, y0 + 1, heart, {"S": SEAL})
    L.set(x0 + 3, y0 + 2, "#ffffff")
    return L


SPARKS = [(8, 52), (86, 66), (10, 88), (84, 10)]


def draw_spark(i: int, big: bool) -> Layer:
    L = Layer()
    x, y = SPARKS[i]
    L.set(x, y, SPARK)
    if big:
        L.pts([(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)], SPARK)
        L.set(x, y, "#ffffff")
    return L


# ---------------------------------------------------------------- timeline
# One loop = 32 ticks of 100 ms (3.2 s). The SVG keyframes mirror these.
TICKS, TICK_MS = 32, 100


def state(t: int) -> dict:
    return {
        "bob": 1 if (t // 4) % 2 else 0,           # 0.8 s bob cycle
        "blink": t in (20, 21),                    # one blink per loop
        "wings_up": (t // 2) % 2 == 0,             # 0.4 s flap cycle
        "buddy_dy": round(-1.5 * math.sin(2 * math.pi * t / 16) - 0.5),  # 1.6 s float
        "bubble": 6 <= t < 16,                     # heart bubble pops up
        "sparks": [((t + 4 * i) // 4) % 4 == 0 for i in range(len(SPARKS))],
    }


def compose(t: int) -> Layer:
    s = state(t)
    out = Layer()
    for i, on in enumerate(s["sparks"]):
        out.merge(draw_spark(i, on))
    shift = Layer()
    body = draw_body()
    body.merge(draw_eyes(s["blink"]))
    shift.px = {(x, y + s["bob"]): c for (x, y), c in body.px.items()}
    out.merge(shift)
    buddy = draw_buddy(s["wings_up"])
    out.px.update({(x, y + s["buddy_dy"]): c for (x, y), c in buddy.px.items()})
    if s["bubble"]:
        out.merge(draw_bubble())
    return out


# ---------------------------------------------------------------- export
def to_image(layer: Layer, scale: int) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for (x, y), c in layer.px.items():
        if 0 <= x < W and 0 <= y < H:
            img.putpixel((x, y), tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) + (255,))
    return img.resize((W * scale, H * scale), Image.NEAREST)


def rects(layer: Layer) -> str:
    """Run-length encode rows into <rect>s grouped by colour."""
    by_color: dict[str, list[str]] = {}
    for y in range(-2, H + 2):
        x = -2
        while x < W + 2:
            c = layer.px.get((x, y))
            if c is None:
                x += 1
                continue
            x1 = x
            while layer.px.get((x1 + 1, y)) == c:
                x1 += 1
            by_color.setdefault(c, []).append(f"M{x} {y}h{x1 - x + 1}v1h-{x1 - x + 1}z")
            x = x1 + 1
    return "".join(f'<path fill="{c}" d="{"".join(d)}"/>' for c, d in by_color.items())


def build_svg() -> str:
    loop = TICKS * TICK_MS / 1000
    sparks = "".join(
        f'<g class="sp sp{i}"><g class="dim">{rects(draw_spark(i, False))}</g>'
        f'<g class="big">{rects(draw_spark(i, True))}</g></g>'
        for i in range(len(SPARKS))
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="288" height="348" shape-rendering="crispEdges" role="img" aria-labelledby="t">
<title id="t">Fumi, the llm-mailroom mascot: a chibi pixel maid with long indigo hair and a frilled headdress, holding a heart-sealed letter, with an owl on her shoulder and a winged envelope fluttering beside her</title>
<style>
.bob{{animation:bob .8s steps(1) infinite}}
@keyframes bob{{0%{{transform:translateY(0)}}50%{{transform:translateY(1px)}}}}
.eo{{animation:eo {loop}s steps(1) infinite}}
.ec{{opacity:0;animation:ec {loop}s steps(1) infinite}}
@keyframes eo{{0%{{opacity:1}}62.5%{{opacity:0}}68.75%{{opacity:1}}}}
@keyframes ec{{0%{{opacity:0}}62.5%{{opacity:1}}68.75%{{opacity:0}}}}
.buddy{{animation:fly 1.6s ease-in-out infinite}}
@keyframes fly{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-2px)}}}}
.wu{{animation:wu .4s steps(1) infinite}}
.wd{{opacity:0;animation:wd .4s steps(1) infinite}}
@keyframes wu{{0%{{opacity:1}}50%{{opacity:0}}}}
@keyframes wd{{0%{{opacity:0}}50%{{opacity:1}}}}
.bubble{{opacity:0;transform-origin:13px 24px;animation:pop {loop}s infinite}}
@keyframes pop{{0%,18%{{opacity:0;transform:scale(.4)}}21%{{opacity:1;transform:scale(1.1)}}24%,46%{{opacity:1;transform:scale(1)}}50%,100%{{opacity:0;transform:scale(.6)}}}}
.big{{opacity:0}}
.sp .big{{animation:tw 1.6s steps(1) infinite}}
.sp .dim{{animation:td 1.6s steps(1) infinite}}
.sp1 *{{animation-delay:-.4s!important}}.sp2 *{{animation-delay:-.8s!important}}.sp3 *{{animation-delay:-1.2s!important}}
@keyframes tw{{0%{{opacity:1}}25%{{opacity:0}}}}
@keyframes td{{0%{{opacity:0}}25%{{opacity:1}}}}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important}}.big{{opacity:1}}}}
</style>
{sparks}
<g class="bob">{rects(draw_body())}<g class="eo">{rects(draw_eyes(False))}</g><g class="ec">{rects(draw_eyes(True))}</g></g>
<g class="buddy"><g class="wu">{rects(draw_buddy(True))}</g><g class="wd">{rects(draw_buddy(False))}</g></g>
<g class="bubble">{rects(draw_bubble())}</g>
</svg>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "fumi.svg").write_text(build_svg())

    still = Layer()
    for i in range(len(SPARKS)):
        still.merge(draw_spark(i, i % 2 == 0))
    still.merge(draw_body())
    still.merge(draw_eyes(False))
    still.merge(draw_buddy(True))
    still.merge(draw_bubble())
    to_image(still, 6).save(OUT_DIR / "fumi.png", optimize=True)
    icon = Layer()
    icon.merge(draw_body())
    box = (SX - 2, SY - 1, SX + 62, SY + 63)  # 64x64 around her head
    to_image(icon, 1).crop(box).resize((256, 256), Image.NEAREST).save(OUT_DIR / "fumi-icon.png", optimize=True)

    scale = 4
    frames = [to_image(compose(t), scale) for t in range(TICKS)]
    pal = []
    for f in frames:  # GIF needs palette mode; keep 1-bit transparency
        p = f.convert("RGBA")
        alpha = p.getchannel("A")
        q = p.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        q.paste(255, mask=Image.eval(alpha, lambda a: 255 if a < 128 else 0))
        pal.append(q)
    pal[0].save(OUT_DIR / "fumi.gif", save_all=True, append_images=pal[1:],
                duration=TICK_MS, loop=0, transparency=255, disposal=2, optimize=False)

    sheet = Image.new("RGBA", (W * 2 * 8, H * 2 * 4), (0, 0, 0, 0))
    for t in range(TICKS):
        sheet.paste(to_image(compose(t), 2), ((t % 8) * W * 2, (t // 8) * H * 2))
    sheet.save(OUT_DIR / "fumi-sheet.png", optimize=True)
    print("wrote", *sorted(p.name for p in OUT_DIR.iterdir()))


if __name__ == "__main__":
    main()
