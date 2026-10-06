# Fumi, the mailroom mascot

Fumi (文, "letter") is a pixel-art chibi postal clerk: rose-pink twin tails tied with red ribbons, big blue eyes, a little navy postal cap with an envelope badge, a sailor collar, and a heart-sealed letter held to her chest. Tegami, the winged envelope, flutters beside her.

| File | Use |
| --- | --- |
| `fumi.svg` | Animated SVG (CSS keyframes: bob, blink, wing flap, heart bubble, sparkles). Respects `prefers-reduced-motion`. Best for the README and the web. |
| `fumi.gif` | Animated GIF, 400×400, 3.2 s loop. For places that don't animate SVG. |
| `fumi.png` | Static 640×640 still. Favicons, avatars, social cards. |
| `fumi-sheet.png` | All 32 animation frames, for reference. |

Everything here is generated. Edit `src/scripts/build_mascot.py` and rerun it:

```bash
python src/scripts/build_mascot.py   # needs Pillow
```
