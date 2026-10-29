# ShrinkRay — logomark files

Amber `#FFB347`, ink tile `#141A20`, beam `#FFF1D9`.
Geometry lives on a 64-unit grid; clearspace is a quarter tile on every side.

## svg/ — masters, edit these

| file | use |
|---|---|
| `shrinkray-mark.svg` | **primary.** Amber glyph, ink tile, cream beam. Works on any ground. |
| `shrinkray-mark-inverted.svg` | amber tile, ink glyph. For light/amber surfaces. |
| `shrinkray-mark-mono.svg` | one colour via `currentColor`; wedges knocked out, beam reads as the solid column. Stencil, print, single-ink. |
| `shrinkray-glyph.svg` | tile-less glyph in `currentColor`. Inline UI icon, 24px and up. |
| `shrinkray-avatar.svg` | circular. GitHub org, Discord, forum avatars. |
| `shrinkray-square.svg` | full-bleed, square corners. For platforms that apply their own mask. |
| `shrinkray-mark-small.svg` | 20–32px master. Beam thickened to 3 units so it survives. |
| `shrinkray-mark-16.svg` | 16px master. Pixel-fitted: wider wedges, 8-unit beam in the same amber, every edge on a whole pixel. |

## png/ — rendered at source size, transparent outside the tile

`shrinkray-{16,24,32,48,64,128,192,256,512,1024}.png`
`shrinkray-inverted-512.png` · `shrinkray-avatar-{256,512}.png`

- `apple-touch-icon.png` — 180px, square, opaque. iOS rounds it itself.
- `shrinkray-maskable-512.png` — Android/PWA `purpose: "maskable"`. Glyph sits inside the 80% safe zone.

## favicon.ico

16 + 32 + 48 in one file.

## Which master rendered which PNG

16 → `shrinkray-mark-16.svg` · 24 → `shrinkray-mark-small.svg` · 32 and up → `shrinkray-mark.svg`.
Re-render the small sizes from their own masters, never by downscaling the 512.
