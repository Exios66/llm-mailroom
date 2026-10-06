"""Build Fumi, the llm-mailroom pixel mascot.

Fumi is drawn procedurally on an 80x80 pixel grid, split into layers
(body, open/closed eyes, the flying envelope buddy, sparkles, speech bubble).
The same layers are emitted as:

  docs/assets/mascot/fumi.svg        animated SVG (CSS keyframes, no JS)
  docs/assets/mascot/fumi.gif        animated GIF (same timeline)
  docs/assets/mascot/fumi.png        static PNG, 8x scale
  docs/assets/mascot/fumi-sheet.png  every GIF frame side by side

Run:  python src/scripts/build_mascot.py   (needs Pillow)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

W = H = 80
OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "assets" / "mascot"
CX = 39.5  # vertical axis of symmetry

# ---------------------------------------------------------------- palette
OUT = "#3b2240"                      # soft plum outline (never pure black)
HAIR, HAIR_L, HAIR_LL, HAIR_D, HAIR_DD = "#c46d8a", "#e396ad", "#f6c3d2", "#9c4f6d", "#723a57"
SKIN, SKIN_D, SKIN_DD = "#fff0e8", "#fbd9cc", "#f0bfb1"
BLUSH, BLUSH_L = "#ff9db5", "#ffd0dc"
LASH, IRIS_D, IRIS, IRIS_L, IRIS_LL, SHINE = "#3b2240", "#3d5fae", "#5e95dc", "#8fc4f2", "#c9e8ff", "#ffffff"
MOUTH, TONGUE = "#b4416a", "#ff8fa8"
NAVY, NAVY_L, NAVY_D = "#3a4a9e", "#5468c4", "#283478"
COLLAR, COLLAR_D = "#ffffff", "#d7dcf2"
RIBBON, RIBBON_D = "#ef4f6a", "#c23552"
GOLD, GOLD_D = "#f6c54f", "#d0962c"
ENV, ENV_D = "#fff8e6", "#ecdcb6"
SEAL, SEAL_L = "#e8445e", "#ff8a9c"
WING, WING_D = "#ffffff", "#cfd8ff"
SPARK = "#ffd34d"


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
def draw_body() -> Layer:
    L = Layer()

    # --- back hair + twin tails (behind everything)
    L.ellipse(CX, 31, 24, 23, HAIR)
    L.ellipse(CX, 31, 24, 23, HAIR_D, where=lambda x, y: y >= 37)  # depth behind the face
    for side in (1, -1):
        # tails taper and curl outward toward the bottom
        for y in range(36, 76):
            t = (y - 36) / 40
            w = 5.5 - 2.5 * t
            cx = CX + side * (21.5 + 3 * math.sin(t * math.pi * 0.9))
            for x in range(int(cx - w), int(cx + w) + 1):
                L.set(x, y, HAIR)
            L.set(int(cx - side * (w - 1)), y, HAIR_D)
            if 0.15 < t < 0.8:
                L.set(int(cx + side * 1), y, HAIR_L)
        # curl tip
        tip_x = int(CX + side * 24)
        L.pts([(tip_x, 76), (tip_x + side, 76), (tip_x + 2 * side, 75)], HAIR)

    # --- body (bust, runs off the bottom edge)
    for y in range(55, H + 2):
        half = 9 + (y - 55) * 0.55
        for x in range(int(CX - half), int(CX + half) + 1):
            L.set(x, y, NAVY)
    for y in range(64, H + 2):  # cardigan side shading
        half = 9 + (y - 55) * 0.55
        L.set(int(CX - half) + 1, y, NAVY_D)
        L.set(int(CX + half) - 1, y, NAVY_D)
    L.rect(36, 52, 43, 56, SKIN)  # neck
    L.rect(36, 55, 43, 55, SKIN_D)
    # sailor collar: two white flaps meeting in a V at the ribbon
    for i in range(7):
        L.sym([(x, 56 + i) for x in range(26 + i, 34 + i // 2)], COLLAR)
    L.sym([(26 + i, 56 + i) for i in range(7)], COLLAR_D)
    L.sym([(29 + i, 57 + i) for i in range(0, 5)], NAVY_L)  # stripe on collar
    # ribbon bow
    bow = ["RR.....RR", "RRRR.RRRR", "RRRDDDRRR", "RRRR.RRRR", ".RR...RR.", "..R...R.."]
    L.sprite(35, 56, bow, {"R": RIBBON, "D": RIBBON_D})

    # held envelope + mitten hands
    L.rect(29, 64, 50, 76, ENV)
    for i in range(7):
        L.set(29 + i * 3 // 2, 64 + i, ENV_D)
        L.set(50 - i * 3 // 2, 64 + i, ENV_D)
    heart = ["SS.SS", "SLSSS", "SSSSS", ".SSS.", "..S.."]
    L.sprite(37, 69, heart, {"S": SEAL, "L": SEAL_L})
    hand = [".hhh.", "hhhhh", "hhhhh", "hhhhd", ".hhd."]
    L.sprite(26, 66, hand, {"h": SKIN, "d": SKIN_DD})
    L.sprite(49, 66, hand, {"h": SKIN, "d": SKIN_DD}, mirror=True)

    # --- face
    L.ellipse(CX, 40, 15.5, 14, SKIN, where=lambda x, y: y >= 28)
    # soften the jaw into a small rounded chin
    L.ellipse(CX, 46, 11.5, 8, SKIN)

    # --- side locks framing the face (in front of the face edge)
    lock = [(24, y) for y in range(28, 52)] + [(25, y) for y in range(28, 50)] + \
           [(26, y) for y in range(28, 44)] + [(23, y) for y in range(30, 54)] + \
           [(27, y) for y in range(28, 37)] + [(24, 52), (24, 53), (25, 50), (25, 51)]
    L.sym(lock, HAIR)
    L.sym([(25, y) for y in range(33, 45)], HAIR_L)
    L.sym([(26, y) for y in range(37, 44)], HAIR_D)

    # --- bangs: rounded fringe made of soft pointed strands
    L.ellipse(CX, 26, 19, 10, HAIR)
    strands = {  # x: lowest y of the strand
        26: 35, 27: 37, 28: 36, 29: 34, 30: 33, 31: 35, 32: 37, 33: 38, 34: 36, 35: 34,
        36: 33, 37: 35, 38: 37, 39: 38,
    }
    for x, low in strands.items():
        for y in range(28, low + 1):
            L.set(x, y, HAIR)
            L.set(int(2 * CX - x), y, HAIR)
    # strands radiating from the crown give the top of the head some texture
    for x0, slope in ((31, -0.25), (35, -0.1)):
        for y in range(11, 24):
            x = int(x0 + slope * (y - 11))
            L.set(x, y, HAIR_D)
            L.set(int(2 * CX - x), y, HAIR_D)
    # a few darker strand partings + soft highlight ring ("angel ring")
    L.sym([(29, 31), (29, 32), (29, 33), (35, 31), (35, 32), (35, 33)], HAIR_D)
    ring = [(x, 17 + int(abs(x - CX) / 6)) for x in range(24, 40)]
    L.sym(ring, HAIR_L)
    L.sym([(x, y + 1) for x, y in ring if 28 <= x <= 36], HAIR_L)
    L.sym([(30, 17), (31, 17), (32, 18)], HAIR_LL)
    # shadow of the fringe on the forehead
    L.sym([(x, 39) for x in range(33, 40) if x % 3 == 0], SKIN_D)

    # --- ahoge (the one rebellious strand)
    L.pts([(38, 9), (37, 8), (36, 7), (35, 6), (35, 5), (36, 4), (37, 3), (38, 3), (39, 3),
           (40, 4), (41, 5), (36, 5), (37, 4), (38, 4), (38, 8), (39, 9)], HAIR)
    L.pts([(36, 5), (37, 4)], HAIR_L)

    # --- little postal cap perched on the right, with an envelope badge
    L.ellipse(51, 10.5, 8, 4.5, NAVY)
    L.rect(42, 13, 60, 14, NAVY_D)
    L.rect(44, 12, 58, 12, NAVY)
    L.rect(48, 7, 52, 7, NAVY_L)
    L.rect(49, 9, 53, 11, GOLD)
    L.pts([(49, 9), (50, 10), (51, 11), (52, 10), (53, 9)], GOLD_D)

    # --- envelope hair clip on the left
    L.rect(16, 25, 21, 28, ENV)
    L.pts([(16, 25), (17, 26), (18, 27), (19, 27), (20, 26), (21, 25)], ENV_D)
    L.set(18, 28, SEAL)
    L.set(19, 28, SEAL)

    # --- ribbons tying the twin tails
    tie = [".RR.RR.", "RRRRRRR", ".RRDRR.", "..R.R.."]
    L.sprite(15, 44, tie, {"R": RIBBON, "D": RIBBON_D})
    L.sprite(58, 44, tie, {"R": RIBBON, "D": RIBBON_D})

    L.outline()

    # --- face details drawn after the outline so they stay inside
    blush = ["bbbb", "bLbb"]
    L.sprite(27, 47, blush, {"b": BLUSH, "L": BLUSH_L})
    L.sprite(49, 47, blush, {"b": BLUSH, "L": BLUSH_L}, mirror=True)
    # tiny soft smile
    L.pts([(38, 50), (39, 51), (40, 51), (41, 50)], MOUTH)
    # inner line between face and hair, so the face reads soft but defined
    for (x, y), c in list(L.px.items()):
        if c == SKIN and y < 52:
            for dx in (-1, 1):
                if L.px.get((x + dx, y)) == HAIR:
                    L.set(x + dx, y, HAIR_D)
    return L


EYE = [  # 8x9 left eye; rows top to bottom
    "..KKKK..",
    ".KKKKKKK",
    "KKDDDDKK",
    "KWWDDDDK",
    "KWWIIIDK",
    "KIIIIIIK",
    "KILLLIIK",
    ".KLLLWK.",
    "..KKKK..",
]
EYE_COL = {"K": LASH, "D": IRIS_D, "W": SHINE, "I": IRIS, "L": IRIS_L}
EYE_Y = 39


def draw_eyes(closed: bool) -> Layer:
    L = Layer()
    if closed:
        # gentle closed lids (a soft smile-curve) over clean skin
        for x0 in (27, 45):
            L.rect(x0, EYE_Y, x0 + 7, EYE_Y + 8, SKIN)
        arc = [(0, 43), (1, 44), (2, 45), (3, 45), (4, 45), (5, 45), (6, 44), (7, 43)]
        L.pts([(27 + x, y) for x, y in arc], LASH)
        L.pts([(52 - x, y) for x, y in arc], LASH)
        L.pts([(26, 42), (53, 42)], LASH)
        return L
    L.sprite(27, EYE_Y, EYE, EYE_COL)
    L.sprite(45, EYE_Y, EYE, EYE_COL, mirror=True)
    # keep the big catch-light on the same side for both eyes (one light source)
    L.pts([(46, 42), (47, 42), (46, 43), (47, 43)], IRIS_D)
    L.pts([(49, 42), (50, 42), (49, 43), (50, 43)], SHINE)
    L.pts([(46, 43), (47, 43)], IRIS)
    # outward lash flicks
    L.pts([(26, 40), (25, 39), (53, 40), (54, 39)], LASH)
    # a little sparkle in each iris
    L.pts([(30, 45), (48, 45)], IRIS_LL)
    return L


# ---------------------------------------------------------------- buddy
BUDDY_X, BUDDY_Y = 65, 22


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
BUBBLE_X, BUBBLE_Y = 2, 6


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


SPARKS = [(6, 40), (74, 52), (8, 64), (73, 7)]


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
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="384" height="384" shape-rendering="crispEdges" role="img" aria-labelledby="t">
<title id="t">Fumi, the llm-mailroom mascot: a chibi postal girl with rose-pink twin tails and a little postal cap, holding a heart-sealed letter while a winged envelope flutters beside her</title>
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
.bubble{{opacity:0;transform-origin:12px 16px;animation:pop {loop}s infinite}}
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
    to_image(still, 8).save(OUT_DIR / "fumi.png", optimize=True)

    scale = 5
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
