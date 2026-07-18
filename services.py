#!/usr/bin/env python3
"""
Muse Studio - external service clients.
Three services, one job each:
  Thinker    - Claude via OpenRouter (ideas, captions, photo matching, brand enrichment)
  Designer   - GPT Image 2 via Fal (slide renders)
  Publisher  - Blotato (Instagram scheduling)
Keys read from (1) env var, (2) ./env.local, (3) ./.env, (4) ~/.config/muse-studio/.env
Keys never leave this process - never returned in a response, never logged.
"""
import os, re, json, base64, mimetypes, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Branding lives here so the Tailor stage is two env vars + one CSS block.
APP_NAME = os.environ.get("APP_NAME") or "Muse Studio"
APP_URL = os.environ.get("APP_URL") or "https://muse-studio.local"


def load_key(name):
    if os.environ.get(name):
        return os.environ[name].strip()
    # env.local wins over .env (documented order in env.example)
    for path in [os.path.join(ROOT, "env.local"), os.path.join(ROOT, ".env"),
                 os.path.expanduser("~/.config/muse-studio/.env")]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


FAL_KEY = load_key("FAL_KEY")
FAL_ENDPOINT = "https://fal.run/openai/gpt-image-2"
FAL_EDIT_ENDPOINT = "https://fal.run/openai/gpt-image-2/edit"  # image-to-image w/ reference photos

BLOTATO_KEY = load_key("BLOTATO_API_KEY")
BLOTATO_DEFAULT_ACCOUNT = load_key("BLOTATO_IG_ACCOUNT_ID") or ""  # optional fallback only; UI picker wins
BLOTATO_BASE = "https://backend.blotato.com"

OPENROUTER_KEY = load_key("OPENROUTER_API_KEY")
OPENROUTER_MODEL = load_key("OPENROUTER_MODEL") or "z-ai/glm-5.2"
# ideas/matching/enrichment need VISION (to see product + logo photos) - GLM is text-only
IDEAS_MODEL = load_key("OPENROUTER_IDEAS_MODEL") or "anthropic/claude-sonnet-4.6"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

STUDIO_USER = load_key("STUDIO_USER") or "client"
STUDIO_PASS = load_key("STUDIO_PASSWORD")


# ---- shared image-url helpers ----------------------------------------------

def _img_url(it):
    """A brand image can be a bare URL string or a {url:...} object (scraped or uploaded data URI)."""
    if isinstance(it, str):
        return it
    if isinstance(it, dict):
        return it.get("url") or it.get("src") or ""
    return ""


def _vision_url(u):
    """Vision/render models need a RASTER image URL (https or data:). Upgrade http, drop SVG + the rest."""
    if not u:
        return ""
    base = u.split("?", 1)[0].split("#", 1)[0].lower()
    if base.endswith(".svg") or u.startswith("data:image/svg"):
        return ""   # SVG is not a raster image - GPT Image 2 / the vision model reject it (HTTP 415)
    if u.startswith("https://") or u.startswith("data:"):
        return u
    if u.startswith("http://"):
        return "https://" + u[len("http://"):]
    if u.startswith("//"):
        return "https:" + u
    return ""


def _to_data_uri(url):
    """Fetch an image from THIS machine and return it as a base64 data: URI.
    Lets Fal use brand photos whose CDN blocks Fal's datacenter IP but serves us fine.
    (Note: once hosted, this also runs from a datacenter IP - the in-app manual upload,
    which produces data URIs client-side, is the reliable fallback there.)
    Returns None on failure so the caller can fall back to the raw URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": url})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            mime = r.headers.get("Content-Type") or mimetypes.guess_type(url)[0] or "image/jpeg"
        if len(data) > 6_000_000:  # giant payloads make the Fal upload crawl past its timeout -
            return None            # fall back to the raw URL and let Fal fetch it itself
        return "data:%s;base64,%s" % (mime.split(";")[0], base64.b64encode(data).decode())
    except Exception as e:
        print("[ref] could not fetch %s: %s" % (url, e), flush=True)
        return None


# ---- Thinker: OpenRouter ----------------------------------------------------

def openrouter_chat(messages, max_tokens=2200, temperature=0.85, timeout=180, model=None, provider=None):
    """Call OpenRouter chat completions. Key stays server-side, never returned."""
    body = {"model": model or OPENROUTER_MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    if provider:
        body["provider"] = provider
    payload = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(OPENROUTER_URL, data=payload, headers={
        "Authorization": "Bearer %s" % OPENROUTER_KEY, "Content-Type": "application/json",
        "HTTP-Referer": APP_URL, "X-Title": APP_NAME})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        out = json.loads(resp.read())
    return out["choices"][0]["message"]["content"]


def ideas_prompt(brand, direction, n, nprod=0):
    name = brand.get("name", "the brand")
    colors = ", ".join((brand.get("colors") or [])[:5])
    voice = brand.get("voice", "")
    vt = ", ".join(brand.get("voiceTags") or [])
    lang = (brand.get("lang") or "").strip()
    if lang:
        lang_line = ('LANGUAGE: The brand and its audience are %s. Write EVERYTHING (tone, title, idea, '
                     'caption, headlines, badges, hashtags) in that language. Never default to English '
                     'unless that IS the brand language.' % lang)
    else:
        lang_line = ('LANGUAGE: Write EVERYTHING (tone, title, idea, caption, headlines, badges, hashtags) '
                     'in the SAME language as the Brand voice and taglines shown above. Do NOT translate to English.')
    if direction.strip():
        dline = 'Creative direction for this whole batch: "%s". EVERY idea must clearly fit this direction.' % direction.strip()
    else:
        dline = "No specific direction was given - propose a varied, on-brand mix."
    if nprod:
        photo_note = (
            "\nIMPORTANT - you are shown real PRODUCT photo(s) below, each labelled PRODUCT[k] where k is its index number, "
            "plus some IMAGERY mood photo(s). Look at them. For EACH slide, set \"productRefs\" to the list of index numbers of "
            "EVERY product photo that slide actually shows - one entry per product in the scene, up to 3. A slide about one sauce "
            "lists one index; a duo slide lists both indexes. Match the product/colour to the slide (e.g. a slide about the purple "
            "product picks the purple product photo). Use an empty list only when no product appears (e.g. a pure text hook). "
            "Within one idea's slides, vary which photos the slides use so the carousel doesn't look like the same image repeated. "
            "Let the IMAGERY photos guide the overall vibe of every idea.\n"
        )
        ref_field = '    "productRefs": [<index numbers k of EVERY product photo this slide shows, up to 3; empty list if none>]\n'
    else:
        photo_note = ""
        ref_field = '    "productRef": null\n'
    return (
        "Brand: %s\nPalette: %s\nVoice: %s\nVoice tags: %s\n%s\n%s\n%s\n"
        "Write %d distinct Instagram carousel post ideas for this brand. Each idea is a multi-slide carousel. "
        "Vary the slide counts across the ideas from 1 to 4 (some single-image, some 2, 3, or 4 slides).\n\n"
        "Return ONLY a JSON array of %d objects, no markdown, no preamble. Each object:\n"
        "{\n"
        '  "tone": "3-5 word label",\n'
        '  "title": "short internal title",\n'
        '  "idea": "ONE short sentence (max 30 words) describing the carousel concept, specific to %s and the direction - keep it tight, not a paragraph. NEVER mention the number of slides or images.",\n'
        '  "caption": "a real Instagram caption in the brand voice: a scroll-stopping hook line, then a short value line, then a clear CTA line, each separated by a blank line. End with exactly 2 relevant hashtags.",\n'
        '  "slides": [ {\n'
        '    "role": "HOOK|LINEUP|PROOF|WHY|SHOP", "headline": "ALL-CAPS headline, max 6 words", "badge": "short sticker text",\n'
        "%s"
        "  } ]\n"
        "}\n"
        "Slides must flow hook -> product/lineup -> proof or why -> shop. Each slide role is a DISTINCT job with its own visual "
        "concept - never repeat the same layout, headline idea, or content between two slides in the same carousel. Pricing, "
        "cost, tiers, comparison cards, and 'order/buy' calls-to-action belong ONLY on the slide tagged \"shop\" - every other "
        "role (hook, lineup, proof, why) must NOT mention price or show an order CTA. Use the brand's real voice and palette. "
        "Write every field in the brand's language as instructed above - never default to English. Never use em dashes anywhere "
        "- use ' - ' or a comma instead. Do NOT state how many slides or images the carousel has anywhere (caption, idea, or "
        "headlines) - the user changes that freely. JSON only."
    ) % (name, colors, voice, vt, lang_line, dline, photo_note, n, n, name, ref_field)


_DASH = re.compile(r"\s*[—–]\s*")  # em dash / en dash


def _strip_dashes(s):
    """Hard guarantee: no em/en dashes reach the user, whatever the model was told."""
    return _DASH.sub(", ", s) if isinstance(s, str) else s


def parse_ideas(raw):
    txt = raw.strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        data = json.loads(txt)
    except Exception:
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    ideas = data if isinstance(data, list) else data.get("ideas")
    if not isinstance(ideas, list):
        return None
    for idea in ideas:
        if not isinstance(idea, dict):
            continue
        for k in ("tone", "title", "idea", "caption"):
            if k in idea:
                idea[k] = _strip_dashes(idea[k])
        for slide in (idea.get("slides") or []):
            if isinstance(slide, dict):
                for k in ("headline", "badge"):
                    if k in slide:
                        slide[k] = _strip_dashes(slide[k])
                # normalize product references: models may send productRefs (list) or the
                # legacy productRef (single number). Emit BOTH so old and new frontends work.
                refs = slide.get("productRefs")
                if not isinstance(refs, list):
                    refs = [slide.get("productRef")] if isinstance(slide.get("productRef"), int) else []
                refs = [r for r in refs if isinstance(r, int)][:3]
                slide["productRefs"] = refs
                slide["productRef"] = refs[0] if refs else None
    return ideas


# ---- Publisher: Blotato ------------------------------------------------------

def blotato_req(path, payload=None, timeout=120, method=None):
    """Call Blotato. Key stays here, never returned/logged. payload=None -> GET."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    r = urllib.request.Request(BLOTATO_BASE + path, data=data, method=method,
        headers={"blotato-api-key": BLOTATO_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def blotato_upload_file(local_path, timeout=120):
    """Upload a local file straight to Blotato's storage (POST /v2/media/uploads for a
    presigned URL, PUT the bytes, use the returned publicUrl) - the documented path for when
    there is no publicly-reachable URL to hand Blotato. This is what makes scheduling work off
    the renders/ backup copy even when a slide's original Fal URL has expired or gone missing,
    instead of the schedule step failing with nothing to fall back on."""
    _, body = blotato_req("/v2/media/uploads", {"filename": os.path.basename(local_path)}, timeout=timeout)
    presigned, public_url = body.get("presignedUrl"), body.get("publicUrl")
    if not presigned or not public_url:
        raise RuntimeError("blotato presigned upload: missing presignedUrl/publicUrl in response")
    with open(local_path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(presigned, data=data, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return public_url
