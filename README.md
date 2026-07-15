# Muse Studio

Brand in, scheduled posts out. Paste a brand's website; Muse reads its look (colors, fonts,
voice, product photos), writes Instagram carousel ideas with captions, renders on-brand
slides, and schedules them straight to Instagram.

A ground-up rebuild of the RoboNuggets "RENDER Studio" concept (CRAFT framework), with a
two-pass scraper, an in-app Instagram account picker, sequential rendering, and a hardened
static file server. Pure Python standard library - nothing to pip install.

## The three services

| Role | Service | Key in env.local |
|---|---|---|
| Thinker (ideas, captions, brand enrichment) | Claude Sonnet via OpenRouter | OPENROUTER_API_KEY |
| Designer (slide renders) | GPT Image 2 via Fal | FAL_KEY |
| Publisher (Instagram scheduling) | Blotato | BLOTATO_API_KEY |

## Run it

```
cp env.example env.local     # then fill in the keys
python3 server.py            # -> http://localhost:5057
```

No keys? The app still opens in demo mode with sample content so every button is clickable.

## How a post happens

1. Paste a site, click "Read it". Pass 1 parses HTML+CSS deterministically; pass 2 makes one
   small vision call that samples colors from the logo and writes the brand voice when the
   site's code alone is too thin. Everything is editable; badges show what came from where.
2. Set a creative direction and generate ideas. Ideas and captions are written in the brand's
   own language, one CTA, no em dashes (enforced server-side).
3. Render a carousel. Slides render ONE at a time (num_images is forced to 1 server-side).
   Reference photos are re-sent as base64 data URIs so CDNs that block Fal cannot break renders.
   Finished slides are saved to renders/<brand>/ so they survive Fal URL expiry.
4. Open the post, pick date + time, pick the Instagram account (loaded live from Blotato,
   never hardcoded), and schedule.

## Deploy (Railway)

1. `railway init` in this folder, then `railway up` (Procfile + empty requirements.txt make
   Nixpacks detect a Python app; server binds 0.0.0.0 when PORT is set).
2. Set STUDIO_USER / STUDIO_PASSWORD in env.local, then push all keys as service variables:
   `bash scripts/push-railway-vars.sh`  (deliberately excludes BLOTATO_IG_ACCOUNT_ID -
   clients pick their account in the UI).
3. With STUDIO_PASSWORD set, the whole app + API sits behind HTTP Basic Auth.

### Hosted caveats
- The data-URI workaround fetches reference photos from the server; hosted, that is a
  datacenter IP, so some brand CDNs may refuse it. The in-app photo upload (client-side
  data URIs) always works - use it when a scraped photo fails to render.
- No Chrome on the host: the scraper's browser fallback is skipped there; a text-reader
  fallback and the manual edit path cover bot-walled sites.
- renders/ and data/ are ephemeral on redeploy.

## Rebrand for a client (Tailor stage)

- `APP_NAME` and `APP_URL` env vars rename the app everywhere (UI reads it from /api/health).
- All interface colors and fonts live in one `/* MUSE THEME */` CSS block at the top of index.html.

## Tests

`python3 -m unittest discover -s tests` - 51 tests: scraper parsing fixtures, enrichment
merge rules, em-dash stripping, auth gate, path traversal, and route validation.
See audit/AUDIT.md for the end-to-end audit on three real brands.
