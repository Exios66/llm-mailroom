# Fumi, the mailroom mascot

Fumi (文, "letter") is a pixel-art chibi postal clerk with owl-tuft hair (a nod to the owl on `../banner.png`), a regulation cap with an envelope badge, a heart-sealed letter, and Tegami, the winged envelope who flutters beside her.

| File | Use |
| --- | --- |
| `fumi.svg` | Animated SVG (CSS keyframes: bob, blink, wing flap, heart bubble, sparkles). Respects `prefers-reduced-motion`. Best for the README and the web. |
| `fumi.gif` | Animated GIF, 384×384, 3.2 s loop. For places that don't animate SVG. |
| `fumi.png` | Static 512×512 still. Favicons, avatars, social cards. |
| `fumi-sheet.png` | All 32 animation frames, for reference. |

Everything here is generated. Edit `src/scripts/build_mascot.py` and rerun it:

```bash
python src/scripts/build_mascot.py   # needs Pillow
```
