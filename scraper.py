#!/usr/bin/env python3
"""
Muse Studio - two-pass brand scraper.
Pass 1: deterministic parsing (no AI, no tokens, instant) - fonts, palette, voice, images.
Pass 2: AI enrichment (one small OpenRouter vision call) - fills the gaps Pass 1 leaves on
        sites whose real palette lives in a logo PNG, or whose copy carries the voice.
A scrape degrades gracefully - it returns warnings, it does not hard-fail because one
rung of the fetch ladder or the enrichment call broke.
"""
import os, re, json, shutil, subprocess, urllib.request, urllib.parse, urllib.error
from html import unescape
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from services import (UA, OPENROUTER_KEY, IDEAS_MODEL, openrouter_chat,
                      _to_data_uri, _vision_url, _img_url)

ALT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"


# ---- fetch ladder ------------------------------------------------------------

# A bot-wall challenge page parsed as if it were the brand's site once produced a saved
# brand literally named "Attention Required!" (Cloudflare's block-page title, superpatch.com).
# HTTP status alone cannot catch this: headless Chrome's --dump-dom returns the challenge
# DOM regardless of status, and some walls serve their interstitial with a 200. So every
# ladder rung's CONTENT is checked. Titles are matched only inside <title> (or the first
# line of reader text) to avoid false positives on normal copy; the marker strings are
# infrastructure-specific (Cloudflare, Incapsula/Imperva, PerimeterX, Akamai, Distil) and
# never appear on legitimate pages.
_BLOCK_TITLES = ("attention required", "just a moment", "access denied",
                 "pardon our interruption", "bot verification",
                 "checking your browser", "verify you are human",
                 "you have been blocked")
_BLOCK_MARKERS = ("cf-browser-verification", "cf_chl_opt", "challenge-platform",
                  "cdn-cgi/challenge", "_incapsula_resource", "incapsula incident id",
                  "px-captcha", "perimeterx", "errors.edgesuite.net",
                  "sorry, you have been blocked",
                  "enable javascript and cookies to continue", "ddos protection by")


def _looks_blocked(content):
    low = content[:8000].lower()
    t = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = t.group(1) if t else low[:300]
    if any(b in title for b in _BLOCK_TITLES):
        return True
    return any(m in low for m in _BLOCK_MARKERS)


def _fetch(url, timeout=30, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _chrome_exe():
    # Windows, macOS, then Linux (PATH). Returns None on hosts without Chrome (e.g. Railway).
    for p in (r"C:/Program Files/Google/Chrome/Application/chrome.exe",
              r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if os.path.isfile(p):
            return p
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _fetch_browser(url, timeout=60):
    """Render the page in headless Chrome - gets past Cloudflare / JS-walled sites. Local dev only."""
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("no chrome for browser-fetch fallback")
    out = subprocess.run([exe, "--headless=new", "--disable-gpu", "--virtual-time-budget=6000",
                          "--dump-dom", url], capture_output=True, timeout=timeout)
    html = out.stdout.decode("utf-8", "ignore")
    if len(html) < 500:
        raise RuntimeError("browser-fetch returned empty")
    # --dump-dom returns the DOM no matter what the server sent - including a bot-wall
    # challenge page. That is how a Cloudflare interstitial once got parsed as a brand.
    if _looks_blocked(html):
        raise RuntimeError("browser-fetch hit the site's bot wall")
    return html


def _fetch_jina(url, timeout=45):
    """Reader fallback that works from datacenter IPs (Railway). Returns markdown-ish text,
    good enough for name/voice/language enrichment; images come from og tags it preserves."""
    txt = _fetch("https://r.jina.ai/" + url, timeout=timeout)
    if len(txt) < 200:
        raise RuntimeError("jina reader returned empty")
    return txt


def _fetch_any(url, timeout=30):
    """The ladder: plain fetch -> alternate UA -> headless Chrome (local) -> jina reader.
    Returns (content, kind) where kind is 'html' or 'text'. Every rung's content is
    block-checked - a challenge page counts as a FAILED rung, never as the site. If all
    four rungs fail or come back blocked, raises one clear error so the caller (and the
    UI) says what actually happened instead of saving the bot wall as brand data."""
    for ua in (UA, ALT_UA):
        try:
            html = _fetch(url, timeout, ua=ua)
            if not _looks_blocked(html):
                return html, "html"
        except Exception:
            pass
    try:
        return _fetch_browser(url), "html"  # raises on empty or blocked
    except Exception:
        pass
    try:
        txt = _fetch_jina(url)
    except Exception:
        txt = ""
    if txt and not _looks_blocked(txt):
        return txt, "text"
    raise RuntimeError("the site's bot protection blocked every read attempt "
                       "(plain, browser, and text proxy). Fill the brand in manually "
                       "and add photos with the + buttons.")


# ---- deterministic parsing helpers (ported, proven) ---------------------------

def _all_images(html, origin):
    """Every image URL in the static HTML, with RELATIVE paths resolved to absolute.
    Catches src=, data-src / data-lazy-src / data-original (lazy-load), srcset (first url), and
    plain absolute urls."""
    cands = list(re.findall(r'https?://[^"\'\s)]+?\.(?:png|jpg|jpeg|webp)', html))
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        cands += re.findall(r'%s=["\']([^"\']+?\.(?:png|jpg|jpeg|webp)[^"\']*)["\']' % attr, html, re.I)
    for ss in re.findall(r'srcset=["\']([^"\']+)["\']', html, re.I):
        first = ss.split(",")[0].strip().split(" ")[0]
        if re.search(r'\.(?:png|jpg|jpeg|webp)', first, re.I):
            cands.append(first)
    out, seen = [], set()
    for u in cands:
        u = urllib.parse.urljoin(origin, u.strip()).split("?")[0].split("#")[0]
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _find_logo(html, origin):
    """Find the brand's actual logo file (favicon excluded - too small/generic).
    Kept separate from productImages so it can be handed to the render model as a
    REFERENCE IMAGE - that's what lets a slide carry the real mark instead of a guess."""
    cands = [u for u in _all_images(html, origin)
             if "logo" in u.lower() and "favicon" not in u.lower()]
    if not cands:
        return ""
    cands.sort(key=lambda u: (bool(re.search(r'-\d{2,4}x\d{2,4}\.', u)), len(u)))
    return cands[0]


_PRODUCT_JUNK = ("icon", "logo", "sprite", "favicon", "placeholder", "badge", "flag", "payment",
        "social", "facebook", "instagram", "twitter", "twiter", "linked", "avixa",
        "partnership", "partner", "cgb",
        # Nutrition Facts Table crops - a real per-item photo lives on the same product page,
        # these are just the label, and a wrong reference image for the render model.
        "nutritional-", "nutrition-facts", "nft_", "nft-")
# Theme decoration (dividers, background textures, spike graphics) and generic stock photos or
# lifestyle/recipe shots are NOT the product itself - handing these to the render model as "the
# real product, don't invent a different one" produces a confidently wrong bottle every time.
_DECOR_JUNK = ("background", "hero", "spike", "divider", "banner-bg", "bg-pattern", "pattern-",
        "texture", "shutterstock", "istock", "gettyimages", "stock-photo", "paper-top", "diamond.")


def _html_products(html, name, origin):
    """Pull product images straight from the HTML when there is no Shopify feed and no
    dedicated shop page was found. Best-effort only - _shop_products() (a real product
    listing page) is far more reliable and takes priority when available."""
    out, seen = [], set()
    for u in _all_images(html, origin):
        low = u.lower()
        # theme decoration lives under /themes/.../assets/ - real product photos are uploads
        if "/themes/" in low or u in seen or any(k in low for k in _PRODUCT_JUNK) or any(k in low for k in _DECOR_JUNK):
            continue
        seen.add(u)
        out.append({"name": name, "url": u})
        if len(out) >= 12:
            break
    return out


_SHOP_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


_HUB_RE = re.compile(r'/(?:shop|products?|collections?)/?$', re.I)


def _shop_links(html, origin):
    """Nav links to a shop/products/collections listing page - where real per-item bottle
    photos actually live on most brand sites (the homepage is usually lifestyle imagery).
    A specific category page (e.g. /categories-products/cooking-sauces) is scanned before
    the bare catalog root (e.g. /products): the root often mixes in recipe/lifestyle cards
    for every SKU, while a named category page tends to be a focused product listing. The
    bare root is still kept (not dropped) - it is the HUB page _shop_products() re-visits to
    discover further category tiles the homepage nav didn't link to directly."""
    out, seen = [], set()
    for h in _SHOP_LINK_RE.findall(html):
        low = h.lower()
        if not any(k in low for k in ("/shop", "/product", "/collections", "/categories-product")):
            continue
        u = urllib.parse.urljoin(origin + "/", h).split("#")[0]
        key = u.rstrip("/")
        if key == origin.rstrip("/") or key in seen:
            continue
        seen.add(key)
        out.append(u)
    out.sort(key=lambda u: bool(_HUB_RE.search(u)))
    return out[:6]


def _category_links(html, origin, exclude_url):
    """Distinct category/collection tile links found on a shop page - the level-2 half of the
    hub -> category crawl. Deliberately narrower than _shop_links' keyword match: a bare
    "/product" substring also matches every SINGLE item link on a listing page (WooCommerce
    puts both the catalog root and individual items under /products/<slug> - confirmed on
    pataks.ca and bluedragon.ca), and following those recurses into product detail pages
    instead of category tiles, pulling in Nutrition Facts Table crops as if they were more
    product photos. Only genuinely category-shaped paths count here: "categor" (catches
    /categories-products/, /product-category/, /category/), "/collections" (Shopify), "/shop".
    Locale duplicates (/fr/...) and feed URLs are excluded on top of self-references."""
    out, seen = [], set()
    exclude = exclude_url.rstrip("/")
    for h in _SHOP_LINK_RE.findall(html):
        low = h.lower()
        if "/fr/" in low or "/feed" in low:
            continue
        if not any(k in low for k in ("categor", "/collections", "/shop")):
            continue
        u = urllib.parse.urljoin(origin + "/", h).split("#")[0]
        key = u.rstrip("/")
        if key == exclude or key == origin.rstrip("/") or key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _dedupe_by_base(urls):
    """WP thumbnail variants (sauce-korma.png, sauce-korma-300x300.png, sauce-korma-150x150.png)
    are the same product - keep the no-suffix original if present, else the largest crop."""
    groups = {}
    for u in urls:
        base = re.sub(r'-\d{2,4}x\d{2,4}(?=\.\w+$)', '', u)
        groups.setdefault(base, []).append(u)
    out = []
    for base, variants in groups.items():
        exact = [v for v in variants if v == base]
        if exact:
            out.append(exact[0])
            continue
        def size_of(v):
            m = re.search(r'-(\d{2,4})x(\d{2,4})(?=\.\w+$)', v)
            return int(m.group(1)) if m else 0
        out.append(max(variants, key=size_of))
    return out


# Rank a product-photo pool so a real PACKAGE photo lands at index 0, not a recipe/lifestyle
# shot - index 0 is what color enrichment samples and what the render model treats as "the
# exact real product." Two independent signals, both common across CPG/condiment sites (the
# exact category Aniko's clients are in - sauces, pastes, marinades, chutneys):
#   1. Isolated packshots are almost always PNG (often alpha transparency); recipe/lifestyle
#      photography on the same listing page is almost always JPEG.
#   2. A packshot filename names the PRODUCT TYPE (sauce-, paste-, marinade-, dip-, kit-,
#      jam-, chutney-...); a recipe photo filename names the DISH, usually several food words
#      joined by "-and-" (e.g. "chickpeas-and-veggies", "lentil-and-veggie-korma").
_PACK_WORDS = ("sauce", "paste", "marinade", "kit", "dip", "syrup", "spice", "seasoning",
               "condiment", "jar", "bottle", "jam", "chutney", "powder", "rub", "relish")


def _score(u):
    low = u.lower().split("?")[0]
    is_jpg = 1 if not low.endswith(".png") else 0
    looks_like_recipe = 1 if "-and-" in low else 0
    looks_like_pack = 0 if any(w in low for w in _PACK_WORDS) else 1
    return (looks_like_pack, is_jpg, looks_like_recipe)


def _fetch_shop_page(url):
    """Fetch one shop/category LEAF page via the normal ladder. Category listing pages are
    ordinary server-rendered HTML, so the plain fetch (ladder rung 1) almost always wins and
    this stays fast - it is the function fanned out in parallel across every category."""
    try:
        return _fetch_any(url)[0]
    except Exception:
        return ""


def _fetch_hub(url, origin):
    """Fetch a shop HUB page and return (html, category_links). Plain fetch first via the
    normal ladder (fast); only when it reveals too few category tiles - the grid is JS-rendered,
    confirmed on pataks.ca where plain HTML exposed 3 of 7 categories - pay for an explicit
    headless render and keep whichever exposed more categories. Well-behaved sites that list
    every category in static HTML never touch the slow browser path."""
    html = _fetch_shop_page(url)
    cats = _category_links(html, origin, exclude_url=url) if html else []
    if len(cats) < 4:
        try:
            rendered = _fetch_browser(url)
            rcats = _category_links(rendered, origin, exclude_url=url)
            if rendered and len(rcats) >= len(cats):
                html, cats = rendered, rcats
        except Exception:
            pass
    return html, cats


def _shop_products(html, origin, name):
    """Real two-level crawl: visit the site's own shop/products page(s), and for each one that
    is itself a HUB linking to further category tiles (Cooking Sauces, Chutneys, Meal Kits,
    Pappadums, Seasoning Mixes, ...) follow every distinct tile too. A single-page crawl only
    ever saw whichever one category happened to be linked from the homepage nav - real catalogs
    are wider than that, and a shallow reference pool is what makes the render model invent a
    product that doesn't exist on the site. Each page contributes at most CATEGORY_CAP photos so
    the pool stays broad across categories instead of deep in one; TOTAL_CAP is a hard ceiling.

    Speed: the category leaf pages are fetched in PARALLEL (I/O-bound), because doing them one
    after another is what made a wide catalog crawl feel slow."""
    CATEGORY_CAP, TOTAL_CAP = 2, 16
    seen_img, seen_page = set(), set()

    def _collect(page_html):
        found = []
        for u in _all_images(page_html, origin):
            low = u.lower()
            if "/themes/" in low or u in seen_img or any(k in low for k in _PRODUCT_JUNK) or any(k in low for k in _DECOR_JUNK):
                continue
            seen_img.add(u)
            found.append(u)
        found = _dedupe_by_base(found)
        found.sort(key=_score)
        return found[:CATEGORY_CAP]

    # Wave 1: the shop/hub links off the homepage. Few, and may need a browser render, so they
    # are fetched sequentially. Each contributes its own photos and reveals category tile links.
    hub_pages, category_urls = [], []
    for link in _shop_links(html, origin):
        key = link.rstrip("/")
        if key in seen_page:
            continue
        seen_page.add(key)
        if _HUB_RE.search(link):
            page_html, cats = _fetch_hub(link, origin)
        else:
            page_html, cats = _fetch_shop_page(link), []
        if not page_html:
            continue
        hub_pages.append(page_html)
        for cat_url in cats:
            ckey = cat_url.rstrip("/")
            if ckey in seen_page:
                continue
            seen_page.add(ckey)
            category_urls.append(cat_url)

    # Wave 2: category leaf pages, fetched concurrently. Order preserved for deterministic output.
    cat_pages = []
    if category_urls:
        with ThreadPoolExecutor(max_workers=6) as ex:
            cat_pages = list(ex.map(_fetch_shop_page, category_urls))

    out = []
    for page_html in hub_pages + cat_pages:
        if not page_html:
            continue
        out.extend(_collect(page_html))
        if len(out) >= TOTAL_CAP:
            break

    deduped = _dedupe_by_base(out)
    deduped.sort(key=_score)
    return [{"name": name, "url": u} for u in deduped[:TOTAL_CAP]]


def _pretty_font(s):
    s = s.strip().split(",")[0].strip().strip('"\'').strip()
    s = s.replace("_", " ").replace("-", " ")            # museo_slab -> museo slab
    s = re.sub(r"\s*\d{3}$", "", s)                      # strip trailing weight (300/700)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)           # camelCase -> camel Case
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)      # ITCAvant -> ITC Avant
    return s.strip().title() if s.islower() else s.strip()


def _meta(html, *keys):
    for k in keys:
        m = re.search(r'(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']+)' % re.escape(k), html, re.I)
        if not m:
            m = re.search(r'content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(k), html, re.I)
        if m:
            return unescape(m.group(1).strip())
    return ""


_LANG_NAMES = {"de": "German (de)", "de-ch": "Swiss Standard German (de-CH, write 'ss' instead of the eszett character)",
    "fr": "French", "fr-ch": "Swiss French (fr-CH)", "it": "Italian", "es": "Spanish", "pt": "Portuguese",
    "nl": "Dutch", "pl": "Polish", "sk": "Slovak", "cs": "Czech", "sv": "Swedish", "da": "Danish",
    "no": "Norwegian", "fi": "Finnish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "en": "English"}


def _lang_name(code):
    c = (code or "").strip().lower().replace("_", "-")
    return _LANG_NAMES.get(c) or _LANG_NAMES.get(c.split("-")[0]) or code


def _lang(html):
    """Detect the site's language for caption writing: <html lang=...> first, then og:locale."""
    m = re.search(r"<html[^>]*\blang=[\"']([a-zA-Z\-_]{2,5})", html, re.I)
    if m:
        return m.group(1).replace("_", "-")
    loc = _meta(html, "og:locale")
    if loc:
        return loc.replace("_", "-")
    return ""


ADJ = ["bold","playful","fun","minimal","clean","modern","luxury","premium","elegant","warm",
       "friendly","cheeky","irreverent","calm","energetic","retro","vintage","sophisticated",
       "quirky","punchy","bright","soft","natural","organic","youthful","wholesome","fresh","real"]

JUNK_HEX = {"#007bff", "#0d6efd", "#0a58ca", "#6610f2", "#0dcaf0",   # Bootstrap 4/5 defaults
            "#428bca", "#5cb85c", "#5bc0de", "#f0ad4e", "#d9534f",   # Bootstrap 3 defaults
            "#dff0d8", "#fcf8e3", "#d9edf7", "#f2dede",              # Bootstrap 3 alert backgrounds
            "#357ebd", "#3071a9", "#2a6496", "#c09853", "#b94a48", "#468847", "#3a87ad",  # Bootstrap 3 borders/text
            "#abb8c3", "#f78da7", "#cf2e2e", "#ff6900", "#fcb900", "#7bdcb5",  # WordPress editor defaults
            "#9b51e0", "#0693e3", "#8ed1fc", "#00d084", "#eb144c", "#f47e60",  # more WP editor defaults
            "#5bbad5", "#da532c", "#2b5797", "#00aba9"}  # Safari mask-icon + msapplication tile defaults


def _colors(html):
    hexes = re.findall(r"#([0-9a-fA-F]{6})\b", html)
    def vivid(h):
        r, g, b = int(h[:2], 16), int(h[2:4], 16), int(h[4:], 16)
        if max(r, g, b) - min(r, g, b) < 22:  # greyscale
            return False
        if ("#" + h.lower()) in JUNK_HEX:      # framework defaults, not brand
            return False
        if all(c in (0, 255) for c in (r, g, b)):  # pure CSS channel colors are usually artifacts
            return False
        return True
    cnt = Counter("#" + h.lower() for h in hexes if vivid(h))
    cols = [c for c, _ in cnt.most_common(6)]
    tc = (_meta(html, "theme-color") or "").lower()
    # theme-color joins only if it is a real brand colour (vivid, not a framework default)
    if re.match(r"^#[0-9a-f]{6}$", tc) and tc not in cols and vivid(tc[1:]):
        cols.insert(0, tc)
    # always carry a dark anchor for text
    if cols and not any(int(c[1:3],16)+int(c[3:5],16)+int(c[5:7],16) < 180 for c in cols):
        cols.append("#161214")
    return cols[:6]


def _fonts(html):
    cands = []
    m = re.search(r'fontFamily"\s*:\s*\{[^}]*?\[\s*"([^"]+)"', html)
    if m: cands.append(m.group(1))
    m = re.search(r'font_family"\s*:\s*"([^"]+)"', html)
    if m: cands.append(m.group(1))
    cands += re.findall(r"@font-face[^}]*?font-family:\s*[\"']?([^;\"'}]+)", html, re.I)
    cands += re.findall(r"font-family:\s*([^;\"}\n]+)", html, re.I)
    generic = ("sans-serif","serif","monospace","inherit","var(","arial","helvetica","system","apple",
               "icon","glyph","fontello","awesome","icomoon","dashicons","material symbols","emoji")
    for c in cands:
        name = c.strip().strip("\"'").split(",")[0].strip()
        if name and not any(g in name.lower() for g in generic) and len(name) > 1:
            return _pretty_font(name)
    return "Inter"


def _images(html, origin):
    """Lifestyle / mood shots for the imagery board - deliberately NOT product packshots (those
    have their own panel). The og:image is skipped when it is just the logo (common on CPG sites,
    where it made the brand mark show up as the first 'mood' tile)."""
    imagery = []
    og = _meta(html, "og:image")
    if og:
        u = urllib.parse.urljoin(origin, og)
        if "logo" not in u.lower() and "favicon" not in u.lower():
            imagery.append(u)
    for u in _all_images(html, origin):
        if any(k in u.lower() for k in ("hero", "condensation", "lifestyle", "banner", "slider", "project", "prj", "gallery")) and u not in imagery:
            imagery.append(u)
    # Fallback: brands without hero-keyword filenames still have lifestyle/recipe photography,
    # which is almost always JPEG (isolated packshots are PNG). Fill a sparse board from non-junk
    # JPGs before giving up - a mood board of dish photos, still never a product packshot.
    if len(imagery) < 3:
        for u in _all_images(html, origin):
            low = u.lower()
            if u in imagery or not re.search(r'\.jpe?g(?:$|\?|#|$)', low):
                continue
            if "/themes/" in low or any(k in low for k in _PRODUCT_JUNK) or any(k in low for k in _DECOR_JUNK):
                continue
            imagery.append(u)
            if len(imagery) >= 8:
                break
    # Collapse WP thumbnail variants (same photo at -300x200, -1024x656, ...) to one tile each.
    return _dedupe_by_base(imagery)[:4]


def _shopify_products(origin):
    try:
        data = json.loads(_fetch(origin.rstrip("/") + "/products.json?limit=12", timeout=25))
    except Exception:
        return []
    out = []
    for p in data.get("products", [])[:8]:
        imgs = p.get("images") or []
        if imgs and imgs[0].get("src"):
            out.append({"name": (p.get("title") or "").strip()[:40], "url": imgs[0]["src"]})
    return out


def _linked_css(html, origin):
    """Fetch the page's linked stylesheets (up to 3) so colors/fonts defined in external
    CSS files are visible to _colors/_fonts."""
    hrefs = re.findall(r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']', html, re.I)
    hrefs += re.findall(r'<link[^>]+href=["\']([^"\']+\.css[^"\']*)["\'][^>]*rel=["\']stylesheet["\']', html, re.I)
    out, seen = [], set()
    for h in hrefs:
        u = urllib.parse.urljoin(origin + "/", h)
        if u in seen:
            continue
        seen.add(u)
        try:
            css = _fetch(u, timeout=12)
            out.append(css[:300_000])
        except Exception:
            continue
        if len(out) >= 3:
            break
    return "\n".join(out)


def _visible_text(html, limit=3000):
    """Rough visible-copy extraction for the enrichment call: strip scripts/styles/tags."""
    txt = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = unescape(re.sub(r"\s+", " ", txt)).strip()
    return txt[:limit]


# ---- Pass 1: deterministic scrape ---------------------------------------------

def scrape_static(url):
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    content, kind = _fetch_any(url)
    p = urlparse(url)
    origin = "%s://%s" % (p.scheme, p.netloc)

    if kind == "text":
        # Reader fallback: no DOM to parse. Minimal brand + the text for enrichment.
        name = p.netloc.replace("www.", "").split(".")[0].title()
        return {
            "name": name, "url": p.netloc.replace("www.", ""), "scrapedReal": True,
            "lang": "", "fonts": {"display": "Inter", "body": "Inter", "note": "site blocked direct reading - set manually"},
            "colors": [], "voice": "", "voiceTags": [], "taglines": [],
            "imagery": [], "productImages": [], "logo": "",
            "warnings": ["Site blocked direct reading. Brand details below were read through a text proxy. Add photos and colors manually if anything is missing."],
        }, content

    html = content
    name = _meta(html, "og:site_name").strip()
    if not name:
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        name = unescape(re.split(r"[|\-–]", (t.group(1) if t else ""))[0].strip())
    if not name:
        name = p.netloc.replace("www.", "").split(".")[0].title()
    if name and name.islower():
        name = name.title()
    desc = _meta(html, "og:description", "description")
    if len(desc.strip()) < 25:
        kw = _meta(html, "keywords")
        desc = (desc + " " + kw).strip() if kw else desc
    headlines = [unescape(re.sub(r"<[^>]+>", "", h)).strip() for h in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)]
    headlines = [h for h in headlines if 2 < len(h) < 60][:4]
    tags = [a for a in ADJ if re.search(r"\b" + a + r"\b", (desc + " " + html[:4000]).lower())][:5]
    # Real per-item bottle/jar photos almost never live on the homepage (that's lifestyle
    # imagery) - they live on the site's own shop/products listing page. Try that first;
    # only fall back to guessing from the homepage when the site has no such page.
    products = _shopify_products(origin)
    if not products:
        products = _shop_products(html, origin, name)
    if not products:
        products = _html_products(html, name, origin)
    # colors/fonts often live ONLY in linked stylesheets - parse HTML + external CSS together
    css = _linked_css(html, origin)
    html_css = html + "\n" + css
    font = _fonts(html_css)
    logo = _find_logo(html, origin)
    # Imagery is the lifestyle/mood board and stays SEPARATE from product packshots - padding it
    # with products (the old behaviour) is what duplicated bottles into the imagery panel and
    # mixed recipe shots and packaging together. Products live in productImages; if a site has
    # no real lifestyle imagery the board is simply sparse, and the UI's + button adds more.
    imagery = _images(html, origin)
    brand = {
        "name": name[:40], "url": p.netloc.replace("www.", ""), "scrapedReal": True,
        "lang": _lang(html),
        "fonts": {"display": font, "body": font, "note": "pulled from the site - edit if off"},
        "colors": _colors(html_css),
        "voice": (desc[:180].replace("—", ", ").replace("–", ", ") if len(desc.strip()) >= 12 else ""),
        "voiceTags": tags,
        "taglines": headlines,
        "imagery": imagery[:4],
        "productImages": products,
        "logo": logo,
        "warnings": [],
    }
    return brand, html


# ---- Pass 2: AI enrichment ------------------------------------------------------

_ENRICH_PROMPT = """You are a brand analyst. You are shown a brand's logo and/or key site image, plus text from their website, plus what an automatic scraper already extracted. Reply with ONLY a JSON object, no markdown, no preamble:
{
  "colors": ["#hex", ...],   // 4-6 hex colors of THE BRAND's visual identity, sampled from the image(s) shown, ordered most-dominant first. Real brand colors only, no greys unless the brand is genuinely monochrome.
  "voice": "...",            // 2-3 sentences describing the brand voice and personality, written IN THE BRAND'S OWN LANGUAGE (the language of the site text shown)
  "voiceTags": ["...", ...], // 3-5 single-word personality tags, same language
  "fontVibe": "serif|sans|slab|display|script"  // best guess of the brand's typographic style
}
Never use em dashes anywhere. JSON only."""


def enrich_brand(brand, html_text):
    """One small vision call to fill weak fields. Never raises - returns (brand, enriched_fields).

    Colors are ALWAYS re-verified against a real product photo when one is available, never
    gated on how many colors Pass 1 already found. Website CSS/theme colors (nav links, buy
    buttons) are frequently unrelated to the product's actual packaging - a site can have
    plenty of CSS hex codes and still be completely wrong about what the bottle looks like.
    The product photo IS the packaging, so it is the authoritative color source whenever it
    exists; CSS colors are kept only as secondary, non-overriding hints."""
    if not OPENROUTER_KEY:
        brand.setdefault("warnings", []).append("AI enrichment skipped: no OPENROUTER_API_KEY.")
        return brand, []
    product_url = _img_url((brand.get("productImages") or [{}])[0]) if brand.get("productImages") else ""
    verify_colors = bool(_vision_url(product_url) or _vision_url(brand.get("logo") or ""))
    weak_voice = len((brand.get("voice") or "").strip()) < 25
    weak_tags = not brand.get("voiceTags")
    if not (verify_colors or weak_voice or weak_tags):
        return brand, []  # nothing to verify against and text fields are already strong

    content = [{"type": "text", "text": _ENRICH_PROMPT}]
    shown = 0
    # real product photo first - it IS the packaging, the single best color source
    for u in ([product_url, brand.get("logo")] + (brand.get("imagery") or []))[:3]:
        v = _vision_url(u or "")
        if not v:
            continue
        data = v if v.startswith("data:") else (_to_data_uri(v) or v)
        content.append({"type": "text", "text": "BRAND IMAGE:"})
        content.append({"type": "image_url", "image_url": {"url": data}})
        shown += 1
        if shown >= 2:
            break
    txt = _visible_text(html_text) if html_text else ""
    if txt:
        content.append({"type": "text", "text": "SITE TEXT (excerpt):\n" + txt})
    content.append({"type": "text", "text": "SCRAPER ALREADY FOUND: " + json.dumps(
        {"name": brand.get("name"), "colors": brand.get("colors"),
         "voice": brand.get("voice"), "lang": brand.get("lang")})})
    if shown == 0 and not txt:
        brand.setdefault("warnings", []).append("AI enrichment skipped: nothing usable to show the model.")
        return brand, []

    try:
        raw = openrouter_chat(
            [{"role": "system", "content": "You reply with valid JSON only."},
             {"role": "user", "content": content}],
            model=IDEAS_MODEL, max_tokens=600, temperature=0.4,
            provider={"order": ["Anthropic"], "allow_fallbacks": True})
        txt2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        m = re.search(r"\{.*\}", txt2, re.S)
        data = json.loads(m.group(0) if m else txt2)
    except Exception as e:
        print("[enrich] failed: %s" % e, flush=True)
        brand.setdefault("warnings", []).append("AI enrichment failed (%s). Using scraped values only." % type(e).__name__)
        return brand, []

    enriched = []
    ai_colors = [c.lower() for c in (data.get("colors") or [])
                 if isinstance(c, str) and re.match(r"^#[0-9a-fA-F]{6}$", c)]
    if verify_colors and ai_colors:
        # AI colors (sampled from the real photo) lead and are never dropped; CSS colors
        # only pad out the tail of the palette, never override the packaging's own colors.
        keep = [c for c in (brand.get("colors") or []) if c not in ai_colors]
        brand["colors"] = (ai_colors + keep)[:6]
        enriched.append("colors")
    if weak_voice and (data.get("voice") or "").strip():
        brand["voice"] = data["voice"].strip()[:400].replace("—", ", ").replace("–", ", ")
        enriched.append("voice")
    if weak_tags and data.get("voiceTags"):
        brand["voiceTags"] = [str(t)[:20] for t in data["voiceTags"][:5]]
        enriched.append("voiceTags")
    if data.get("fontVibe") and brand.get("fonts", {}).get("display") == "Inter":
        brand["fonts"]["note"] = "style guess: %s - set the exact font manually" % data["fontVibe"]
    return brand, enriched


# ---- entry point -----------------------------------------------------------------

def scrape_brand(url):
    """Full two-pass scrape. Returns the brand dict with provenance under brand['sources']."""
    brand, html = scrape_static(url)
    sources = {k: "scraped" for k in ("colors", "voice", "voiceTags")}
    brand, enriched = enrich_brand(brand, html)
    for f in enriched:
        sources[f] = "ai"
    brand["sources"] = sources
    if not brand.get("colors"):
        brand["colors"] = ["#1c1a16", "#6f6a5c", "#d9d3c6", "#f2efe8", "#c19a4b"]
        sources["colors"] = "default"
        brand.setdefault("warnings", []).append("No brand colors found. Edit the palette manually.")
    return brand
