"""Build Fumi, the llm-mailroom pixel mascot.

Fumi is drawn procedurally on a 64x64 pixel grid, split into layers
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

W = H = 64
OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "assets" / "mascot"

# ---------------------------------------------------------------- palette
OUT = "#2b1b33"
HAIR, HAIR_L, HAIR_D = "#8a5430", "#b97c45", "#5e3520"
FEATHER = "#f3e3c8"
SKIN, SKIN_D = "#ffe6d3", "#f6c7ad"
BLUSH = "#ff8fa8"
LASH, IRIS, IRIS_D, IRIS_L, SHINE = "#3a1f33", "#f5a623", "#c85f1a", "#ffd86b", "#ffffff"
CAP, CAP_D, BRIM, GOLD = "#fbf8f2", "#d9d1c3", "#28336a", "#f4c34d"
JACKET, JACKET_L, JACKET_D = "#34459a", "#4d62c0", "#232e6a"
COLLAR, TIE = "#ffffff", "#e8475d"
ENV, ENV_D, SEAL, SEAL_L = "#fff7e3", "#e6d4ad", "#d92b45", "#ff6b80"
BAG, BAG_D, BAG_L = "#a5622f", "#6f3e1d", "#cf8748"
SOCK, SHOE = "#ffffff", "#5b3221"
WING, WING_D = "#ffffff", "#c9d3ff"
SPARK = "#ffd34d"
MOUTH = "#a8364f"


class Layer:
    def __init__(self) -> None:
        self.px: dict[tuple[int, int], str] = {}

    def set(self, x: int, y: int, c: str) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.px[(x, y)] = c

    def pts(self, pts, c: str) -> None:
        for x, y in pts:
            self.set(x, y, c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: str) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.set(x, y, c)

    def ellipse(self, cx: float, cy: float, rx: float, ry: float, c: str) -> None:
        for y in range(H):
            for x in range(W):
                if ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2 <= 1:
                    self.set(x, y, c)

    def outline(self, c: str = OUT) -> None:
        """Add a 1px outline around every filled pixel (4-neighbourhood)."""
        add = set()
        for x, y in self.px:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (x + dx, y + dy)
                if n not in self.px:
                    add.add(n)
        for x, y in add:
            self.set(x, y, c)

    def merge(self, other: "Layer") -> None:
        self.px.update(other.px)


def mirror(pts):
    """Mirror points around the character's vertical axis (x=31.5)."""
    return [(63 - x, y) for x, y in pts]


# ---------------------------------------------------------------- body
def draw_body() -> Layer:
    L = Layer()

    # back hair (long, falls behind shoulders)
    L.ellipse(32, 27, 15.5, 14, HAIR)
    L.rect(17, 27, 21, 44, HAIR)
    L.rect(42, 27, 46, 44, HAIR)
    ends = [(18, 45), (19, 45), (21, 45), (18, 46), (21, 46), (17, 45)]
    L.pts(ends, HAIR)
    L.pts(mirror(ends), HAIR)
    for y in range(30, 44):
        L.set(17, y, HAIR_D)
        L.set(46, y, HAIR_D)
    for y in range(29, 41):
        L.set(19, y, HAIR_L if y % 5 else HAIR)
        L.set(44, y, HAIR_L if y % 5 else HAIR)

    # owl ear tufts
    rows = {3: (12, 12), 4: (12, 13), 5: (12, 14), 6: (13, 15), 7: (13, 16), 8: (14, 17),
            9: (14, 18), 10: (15, 19), 11: (16, 20), 12: (17, 21), 13: (18, 21)}
    tuft = [(x, y) for y, (a, b) in rows.items() for x in range(a, b + 1)]
    L.pts(tuft, HAIR)
    L.pts(mirror(tuft), HAIR)
    fl = [(13, 5), (14, 7), (15, 9), (16, 10)]
    L.pts(fl, FEATHER)
    L.pts(mirror(fl), FEATHER)
    dk = [(15, 7), (16, 8), (17, 9), (18, 10), (19, 11)]
    L.pts(dk, HAIR_D)
    L.pts(mirror(dk), HAIR_D)

    # body: navy postal jacket
    for i, y in enumerate(range(39, 52)):
        half = 7 + min(i, 5) // 2 + (1 if i > 8 else 0)
        L.rect(32 - half, y, 31 + half, y, JACKET)
    L.rect(22, 47, 23, 51, JACKET_D)
    L.rect(40, 47, 41, 51, JACKET_D)
    L.rect(26, 52, 37, 52, JACKET_D)  # hem
    for y in range(41, 52):  # buttons placket
        L.set(31, y, JACKET_L)
    # sleeves
    L.rect(22, 41, 24, 46, JACKET)
    L.rect(39, 41, 41, 46, JACKET)
    L.pts([(22, 41), (41, 41)], JACKET_L)

    # white sailor collar + red tie
    L.pts([(26, 38), (27, 38), (28, 39), (29, 39), (30, 40), (37, 38), (36, 38), (35, 39),
           (34, 39), (33, 40), (25, 39), (26, 39), (27, 40), (38, 39), (37, 39), (36, 40)], COLLAR)
    L.pts([(31, 40), (32, 40), (30, 41), (31, 41), (32, 41), (33, 41)], TIE)

    # legs
    L.rect(27, 53, 29, 55, SKIN)
    L.rect(34, 53, 36, 55, SKIN)
    L.rect(27, 55, 29, 56, SOCK)
    L.rect(34, 55, 36, 56, SOCK)
    L.rect(26, 57, 29, 58, SHOE)
    L.rect(34, 57, 37, 58, SHOE)

    # satchel strap (diagonal, behind the envelope) + bag on right hip
    for i in range(12):
        L.set(25 + i, 39 + i, BAG_D)
    L.rect(38, 46, 45, 52, BAG)
    L.rect(38, 46, 45, 47, BAG_L)  # flap
    L.pts([(41, 48), (42, 48)], GOLD)  # buckle
    L.rect(38, 52, 45, 52, BAG_D)
    L.pts([(40, 45), (41, 45), (42, 45), (43, 44), (44, 44)], ENV)  # letter peeking out

    # held envelope in front of chest (with tiny hands)
    L.rect(25, 42, 37, 49, ENV)
    for i in range(6):  # flap V
        L.set(25 + i, 42 + i, ENV_D)
        L.set(37 - i, 42 + i, ENV_D)
    L.pts([(29, 46), (30, 46), (32, 46), (33, 46), (29, 47), (30, 47), (31, 47), (32, 47),
           (33, 47), (30, 48), (31, 48), (32, 48), (31, 49)], SEAL)  # heart wax seal
    L.set(29, 46, SEAL_L)
    L.pts([(24, 45), (24, 46), (25, 46), (38, 45), (38, 46), (37, 46)], SKIN)  # hands

    # face
    L.ellipse(32, 28.5, 10.5, 9.5, SKIN)
    L.rect(29, 37, 34, 38, SKIN)  # neck/chin blend
    # side locks in front of face
    lock = [(21, 24), (21, 25), (21, 26), (22, 24), (22, 25), (22, 26), (22, 27), (22, 28),
            (22, 29), (22, 30), (23, 30), (22, 31), (23, 31), (22, 32), (23, 32), (23, 33),
            (23, 34), (22, 33), (22, 34), (22, 35), (23, 35), (22, 36), (23, 36), (23, 37),
            (22, 38), (23, 38), (22, 39), (21, 37), (21, 38), (21, 36)]
    L.pts(lock, HAIR)
    L.pts(mirror(lock), HAIR)
    L.pts([(22, 26), (22, 27)], HAIR_L)
    L.pts(mirror([(22, 26), (22, 27)]), HAIR_L)

    # bangs: jagged fringe across the forehead
    L.rect(21, 15, 42, 20, HAIR)
    fringe_drop = {22: 3, 23: 3, 24: 2, 25: 1, 26: 2, 27: 1, 28: 2, 29: 3, 30: 4, 31: 2,
                   32: 3, 33: 4, 34: 3, 35: 2, 36: 1, 37: 2, 38: 1, 39: 2, 40: 3, 41: 3}
    for x, d in fringe_drop.items():
        L.rect(x, 21, x, 20 + d, HAIR)
    L.pts([(26, 18), (27, 18), (28, 17), (29, 17), (34, 17), (35, 17), (36, 18)], HAIR_L)
    L.pts([(29, 22), (30, 23), (33, 23), (34, 22)], HAIR_D)

    # blush + cat mouth (ω)
    L.pts([(23, 31), (24, 31), (25, 31), (24, 32)], BLUSH)
    L.pts([(38, 31), (39, 31), (40, 31), (39, 32)], BLUSH)
    L.pts([(30, 33), (31, 34), (32, 34), (33, 33)], MOUTH)

    # postal cap (white crown, navy brim, gold badge)
    L.ellipse(32, 11.5, 12.5, 5.5, CAP)
    L.rect(21, 12, 43, 14, CAP)
    L.rect(22, 9, 42, 9, CAP)
    L.rect(21, 14, 43, 14, CAP_D)
    L.rect(19, 15, 45, 16, BRIM)
    L.rect(20, 17, 44, 17, BRIM)
    L.rect(30, 9, 33, 11, GOLD)
    L.pts([(30, 9), (31, 10), (32, 10), (33, 9)], "#c48a22")

    # ahoge sticking out of the cap
    L.pts([(33, 6), (34, 5), (35, 4), (36, 4), (37, 5), (37, 6), (36, 6)], HAIR)

    L.outline()

    # small internal shading done after the outline so it stays inside
    L.pts([(31, 23), (32, 23)], SKIN_D)  # fringe shadow on forehead
    return L


EYE = [  # left eye, 5x7, top-left at (24, 23); right eye reuses it at x=35
    "LLLLL",
    "DDDDD",
    "WWDPD",
    "WDPPD",
    "IIPPI",
    "IIIIS",
    ".KKK.",
]
EYE_COL = {"L": LASH, "D": IRIS_D, "W": SHINE, "P": LASH, "I": IRIS, "S": SHINE, "K": IRIS_L}


def draw_eyes(closed: bool) -> Layer:
    L = Layer()
    if closed:  # happy ^ ^ closed eyes
        for ox in (24, 35):
            L.pts([(ox, 28), (ox + 1, 27), (ox + 2, 26), (ox + 3, 27), (ox + 4, 28)], LASH)
        return L
    for ox in (24, 35):
        for dy, row in enumerate(EYE):
            for dx, ch in enumerate(row):
                if ch != ".":
                    L.set(ox + dx, 23 + dy, EYE_COL[ch])
    L.pts([(23, 23), (22, 22), (40, 23), (41, 22)], LASH)  # lash flicks
    return L


# ---------------------------------------------------------------- buddy
BUDDY_X, BUDDY_Y = 50, 27


def draw_buddy(wings_up: bool) -> Layer:
    L = Layer()
    x0, y0 = BUDDY_X, BUDDY_Y
    if wings_up:
        lw = [(x0 - 3, y0 - 2), (x0 - 2, y0 - 2), (x0 - 4, y0 - 1), (x0 - 3, y0 - 1),
              (x0 - 2, y0 - 1), (x0 - 1, y0), (x0 - 2, y0)]
    else:
        lw = [(x0 - 1, y0 + 3), (x0 - 2, y0 + 3), (x0 - 3, y0 + 4), (x0 - 2, y0 + 4),
              (x0 - 4, y0 + 5), (x0 - 3, y0 + 5), (x0 - 1, y0 + 4)]
    rw = [(2 * x0 + 9 - x, y) for x, y in lw]
    L.pts(lw, WING)
    L.pts(rw, WING)
    # envelope body 10x7
    L.rect(x0, y0, x0 + 9, y0 + 6, ENV)
    L.outline()
    for i in range(5):
        L.set(x0 + i, y0 + i, ENV_D)
        L.set(x0 + 9 - i, y0 + i, ENV_D)
    L.pts([(x0 + 4, y0 + 4), (x0 + 5, y0 + 4)], SEAL)  # heart seal
    L.pts([(x0 + 4, y0 + 3), (x0 + 5, y0 + 3)], SEAL_L)
    L.pts([(x0 + 2, y0 + 5), (x0 + 7, y0 + 5)], LASH)  # eyes
    L.pts([(x0 + 1, y0 + 6), (x0 + 8, y0 + 6)], BLUSH)
    for p in lw[:2] + rw[:2]:
        L.set(*p, WING_D)
    return L


# ---------------------------------------------------------------- extras
def draw_bubble() -> Layer:
    """Little speech bubble with a pixel heart, above-left of her head."""
    L = Layer()
    x0, y0 = 3, 14
    L.rect(x0, y0, x0 + 10, y0 + 7, "#ffffff")
    L.pts([(x0 + 8, y0 + 8), (x0 + 9, y0 + 8), (x0 + 9, y0 + 9)], "#ffffff")
    L.outline()
    heart = [(1, 0), (2, 0), (4, 0), (5, 0), (0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (5, 1),
             (6, 1), (0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (5, 2), (6, 2), (1, 3), (2, 3),
             (3, 3), (4, 3), (5, 3), (2, 4), (3, 4), (4, 4), (3, 5)]
    L.pts([(x0 + 2 + x, y0 + 1 + y) for x, y in heart], SEAL)
    L.pts([(x0 + 3, y0 + 2)], "#ffffff")
    return L


SPARKS = [(10, 34), (55, 14), (8, 52), (56, 47)]


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
<title id="t">Fumi, the llm-mailroom mascot: a chibi postal girl with owl-tuft hair, holding a wax-sealed letter while a winged envelope flutters beside her</title>
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
    to_image(still, 8).save(OUT_DIR / "fumi.png", optimize=True)

    scale = 6
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
