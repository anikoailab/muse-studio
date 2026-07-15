# Muse Studio - Audit Report (CRAFT stage A)

Date: 2026-07-12 · Tester: Claude (Playwright-driven real browser against http://127.0.0.1:5057)
Brands chosen by Aniko: pataks.ca · bluedragon.ca · alfez.ca

## Verdict: PASS

All three brands went through the entire app in a real browser: paste the site, read the brand,
generate ideas, render slides, and (for Al'Fez) schedule to Instagram via Blotato. One bug and
three weaknesses were found and fixed during the audit (see Fixes). Zero console errors in any run.

## Per-brand results

| Check | Patak's | Blue Dragon | Al'Fez Canada |
|---|---|---|---|
| Scrape: palette (>= 3 real colors) | PASS (6) | PASS (6, AI-sampled from logo) | PASS (3, junk favicon colors filtered) |
| Scrape: voice non-empty | PASS | PASS (AI) | PASS (AI) |
| Scrape: products found | 12 | 12 | 12 |
| Scrape: logo found | yes | yes | no (none on site, render still fine) |
| Ideas: on-brand, in brand language | PASS | PASS | PASS |
| Captions: zero em dashes | PASS | PASS | PASS |
| Render: slides completed | 2/2 | 2/2 | 2/2 + 1 (schedule test) |
| Renders saved to renders/ | yes | yes | yes |
| Console errors | 0 | 0 | 0 |

Note: bluedragon.ca is the site the OLD app failed on (1 color, empty voice, no images).
Muse Studio's two-pass scraper returns a real palette, written voice, logo, and 12 products.

## Blotato scheduling test (spam-safe protocol)

- ONE post, scheduled through the app's own UI (account picker -> arianasolen.ai 43679),
  30 days in the future at an odd minute (2026-08-11 12:43 UTC).
- Verified present in the Blotato queue via API (schedule id 2479811).
- Deleted immediately via API; deletion confirmed. Nothing ever reached Instagram.
- The account picker listed all connected accounts live from Blotato; no account ID is
  hardcoded anywhere in the app.

## Cost

7 paid image renders total (GPT Image 2 high, ~2 megapixel) ~= $1.40-1.75, well under the $5 cap.
Ideas + enrichment calls (Claude Sonnet via OpenRouter): a few cents.

## Fixes applied during the audit

1. Render timeout on slow Fal jobs: server-side Fal timeout raised 240s -> 360s, and the
   data-URI size guard lowered to 6MB so giant reference PNGs fall back to the raw URL
   instead of stalling the upload.
2. Palette pollution: #ffffff theme-color meta and Safari/Microsoft favicon defaults
   (#5bbad5, #da532c, #2b5797, #00aba9) now filtered from scraped palettes.
3. Security: the static file server now serves ONLY html/css/js/image files - a direct
   request for /env.local or /server.py returns 404 (the old app served any file in its folder).
4. _pretty_font left a stray quote on quoted font stacks ("Open Sans" -> Open Sans"). Fixed.

## Known limitations (documented, not blockers)

- Al'Fez renders in this audit used a pre-fix palette that included favicon junk colors;
  the palette is clean after fix 2. The rendered slides still came out on-brand.
- persistRender saves slides to disk asynchronously; closing the tab immediately after a
  render can skip the disk copy for later slides (the Fal URL still works for scheduling).
- On Railway the data-URI CDN workaround runs from a datacenter IP; manual photo upload
  in the app is the reliable fallback (documented in README).

## Test evidence

- audit/screenshots/<brand>-1-brand.png, -2-ideas.png, -3-rendered.png per brand
- audit/screenshots/schedule-1-drawer.png, schedule-2-confirmed.png
- renders/patak-s/, renders/blue-dragon/, renders/al-fez-canada/
- Unit tests: 51/51 green (python3 -m unittest discover -s tests)
