# Fumi, the mailroom mascot

Fumi (文, "letter") is a chibi postal maid who runs the mailroom: long indigo hair, a frilled headdress with a mini postal cap pinned to it, a red collar bow with an envelope charm, a crossbody mail satchel, a postage-stamp patch on her apron, a heart-sealed letter held in both hands, and Hoot, a little owl, on her shoulder. Tegami, the winged envelope, flutters beside her.

| File | Use |
| --- | --- |
| `fumi.svg` | Animated SVG (CSS keyframes: bob, blink, wing flap, heart bubble, sparkles). Respects `prefers-reduced-motion`. Best for the README and the web. |
| `fumi.gif` | Animated GIF, 384×464, 3.2 s loop. For places that don't animate SVG. |
| `fumi.png` | Static 576×696 still. |
| `fumi-icon.png` | Square 256×256 head-and-shoulders icon for favicons and avatars. |
| `fumi-sheet.png` | All 32 animation frames, for reference. |
| `source/fumi-base.png` | The 62×107 base sprite at native pixel size. Everything else is built from it. |

The base sprite was cleaned up from reference art supplied by the project owner: resampled onto its native pixel grid, background removed, palette reduced to 32 colours. The script adds the postal uniform pieces, the letter, Hoot, Tegami, the blink frames and the animation, and copies the web files into `landing/assets/mascot/`. To change any of them, edit `src/scripts/build_mascot.py` and rerun it:

```bash
python src/scripts/build_mascot.py   # needs Pillow
```
