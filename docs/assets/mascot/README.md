# Fumi, the mailroom mascot

Fumi (文, "letter") is a chibi postal maid who runs the mailroom, in a USPS-style carrier uniform: a postal-blue shirt, a navy skirt with a red and white hem stripe, a mini navy carrier cap with a red and white band pinned to her frilled headdress, a shoulder patch, a red collar bow, a leather mail satchel, and a postage-stamp patch on her apron. Hermes, a little owl, rides on her shoulder.

| File | Use |
| --- | --- |
| `fumi.svg` | Animated SVG (CSS keyframes: bob, blink, heart bubble, sparkles). Respects `prefers-reduced-motion`. Best for the README and the web. |
| `fumi.gif` | Animated GIF, 384×464, 3.2 s loop. For places that don't animate SVG. |
| `fumi.png` | Static 576×696 still. |
| `fumi-icon.png` | Square 256×256 head-and-shoulders icon for avatars. |
| `hoot-icon.png` | Square 256×256 pixel owl (Hermes). Favicon for `landing/` and the GitBook Customize site icon. |
| `fumi-sheet.png` | All 32 animation frames, for reference. |
| `source/fumi-base.png` | The 62×107 base sprite at native pixel size. Everything else is built from it. |

The base sprite was cleaned up from reference art supplied by the project owner: resampled onto its native pixel grid, background removed, palette reduced to 32 colours. The script recolours the dress into the postal uniform and adds the cap, patches, satchel, Hermes, the blink frames and the animation, and copies the web files into `landing/assets/mascot/` (including the still `fumi.png` for reduced-motion visitors) and the GitBook home GIF into `docs/assets/fumi/fumi.gif`. To change any of them, edit `src/scripts/build_mascot.py` and rerun it:

```bash
python src/scripts/build_mascot.py   # needs Pillow
```
