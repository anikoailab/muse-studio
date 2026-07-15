# Plan · Muse Studio Story Format

## Brief
Muse Studio currently renders 1080x1350 feed carousel slides and schedules them to
Instagram/TikTok/Facebook feed posts. Add a Story format: vertical 1088x1920 slides from the
same scraped brand, scheduled as Instagram Stories and Facebook Page Stories.

## Stack
- Fal (GPT Image 2 edit/text endpoint) - paid per render, flag cost approval before any batch run
- Blotato API (`mediaType: "story"` on Instagram and Facebook post targets)
- Python 3 stdlib only (server.py, services.py) - no new deps
- Vanilla JS only (index.html) - no new deps, no new `<script src>`

## Scope

**Visuals**
- Story slides render at 1088x1920 (vertical 9:16, multiple-of-16 for the edit endpoint), using
  the brand's existing scraped palette, fonts, voice, and product-photo references
- A distinct "Story" section/toggle in index.html, visually separate from the carousel section,
  reusing the existing render-and-preview pattern (thumbnail grid, lightbox, sequential
  one-at-a-time render with a progress note)

**Functionality**
- A story slide-count control (like the carousel's "Slides" stepper); each slide gets its own
  distinct headline/prompt, never the same image or prompt repeated across slides (the carousel
  build already hit and fixed this exact bug once - do not reintroduce it)
- Render path (extend `/api/generate` or add a clearly separate one) accepts the story
  dimensions, with the same 422 fallback-preset retry pattern the carousel path already uses
- Scheduling: extend `/api/schedule` so Instagram and Facebook targets can carry
  `mediaType: "story"`; each story slide is its own separate Blotato post (verify this against
  the live Blotato API, see Risks), staggered a few minutes apart, reusing the already-built
  multi-platform account/page picker and the `blotato_upload_file` local-file fallback
- Same approval gate as the carousel flow: every render shown in the UI, explicit user action
  required before scheduling, never auto-chained

## Out of Scope
- No music, ffmpeg, or baked-in captions (that's the separate Story Factory / AICA pipeline,
  unrelated to Muse Studio)
- No TikTok story or photo-mode support
- No automatic multi-slide compilation into a single video
- No changes to the existing 1080x1350 carousel rendering or scheduling code paths

## Constraints
- Pure Python stdlib + vanilla JS only, no new pip installs, no new JS dependencies
- Render one slide at a time, never batch-generate (`num_images` stays forced to 1 server-side)
- Every render shown and explicitly approved before any scheduling call fires
- Sanity-check real render cost on 1-2 test slides before a full story batch
- The existing carousel test suite must keep passing untouched
- No hardcoded account/page IDs anywhere

## Definition of Done
From the running app, a user can switch to Story mode for a scraped brand, render at least 2
distinct 1088x1920 story slides via GPT Image 2, preview them, and schedule them as separate
Instagram Story and Facebook Page Story posts via Blotato, with the existing carousel test suite
still green.

## Acceptance Criteria
- Story mode is a distinct, clearly labeled section in index.html, not merged into the carousel UI
- Rendered story images are 1088x1920 (or the nearest multiple-of-16 Fal accepts), verified against
  the actual returned image dimensions
- Two sequential story slides for the same brand show visibly different compositions/headlines
- The story render path forces `num_images=1` and fires one request per slide, never batched
- Each rendered slide requires an explicit user action before scheduling, no auto-chaining
- Scheduling a story posts each slide as its own Blotato post with `mediaType: "story"`, through
  the existing multi-platform account/page picker, no new hardcoded IDs
- `blotato_upload_file` works for story slides exactly as it does for carousel slides when a
  slide's original render URL is unavailable at schedule time
- `python3 -m unittest discover -s tests` passes, including new tests for the story render size
  and story schedule target-building, with zero regressions to existing tests
- No pip installs added, no new external JS dependencies in index.html
- Cost of the verification render run (slide count x per-render cost) is reported before running
  a full end-to-end story batch

## Verification
- `python3 -m unittest discover -s tests` exits 0, all green
- Start the server, load a previously-scraped brand, switch to Story mode, render exactly 2 story
  slides, confirm in-browser: both are 1088x1920, both show different content, zero console errors
- Inspect the actual request body sent to Fal for a story render and confirm the image size
  differs from the carousel's 1080x1350 / 1088x1360
- Inspect the request body sent to `POST /v2/posts` for a story slide and confirm
  `mediaType: "story"` is present and exactly one mediaUrl is included per post
- Confirm via Blotato's schedule list that the test story posts appear as separate scheduled
  items with the correct stagger, then delete/cancel them immediately - do not leave real test
  posts live in the queue

## Turn Budget
Stop after 45 turns, or sooner once the DoD condition holds.

## References
- `server.py` - existing `_generate`, `_schedule`, `_resolve_media`, `blotato_upload_file` usage
- `services.py` - `blotato_req`, `blotato_upload_file`
- `index.html` - existing carousel render/schedule UI to mirror
- Blotato OpenAPI spec: `https://backend.blotato.com/openapi.json` (mediaType field on
  Instagram/Facebook post targets)

## Risks / Open Questions
- Blotato's exact story-post payload shape (single mediaUrl vs an array treated as a sequence)
  should be checked against the live OpenAPI spec before writing the schedule code - "one post
  per slide" is well precedented in Aniko's other pipelines but has not been verified against
  this specific endpoint
- Fal's GPT Image 2 may handle a 1088x1920 request differently than the existing portrait sizes -
  the carousel's 422 `portrait_4_3` retry fallback may need a story-specific equivalent instead
  of being reused as-is
- Any real scheduled test posts created during verification must be deleted immediately after
  confirming they exist in the queue - never leave stray test content scheduled live
