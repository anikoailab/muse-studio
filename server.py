#!/usr/bin/env python3
"""
Muse Studio - local server.
  - serves the static app (index.html)
  - GET  /api/health         -> key presence booleans (never values)
  - GET  /api/brand          -> the last saved brand (persisted on disk)
  - GET  /api/accounts       -> the Blotato accounts list (for the UI account picker)
  - POST /api/scrape {url}   -> two-pass brand scrape, saved to disk
  - POST /api/save  {brand}  -> persist the current brand
  - POST /api/ideas          -> carousel ideas + captions (OpenRouter vision)
  - POST /api/generate       -> ONE slide render via GPT Image 2 on Fal (always one at a time)
  - POST /api/schedule       -> schedule to Instagram via Blotato (accountId from the request)
  - POST /api/store-image    -> save a rendered slide to renders/<brand>/

Run:   python server.py        (then open http://localhost:5057)
Keys:  read from (1) env var, (2) ./env.local, (3) ./.env, (4) ~/.config/muse-studio/.env
"""
import os, re, json, base64, hmac, mimetypes, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import services as S
from scraper import scrape_brand

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT, "data", "brand-state.json")


def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_state(brand):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(brand, f, indent=2)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        try:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up (reload / abort) - nothing to send it, nothing to log

    # Only these may ever leave over HTTP. Everything else in ROOT (env.local, *.py,
    # data/brand-state.json, scripts) stays server-side - the old app served ANY file
    # under its folder, which would have handed out the keys file on a direct request.
    _SERVABLE = (".html", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".css", ".js")

    def _file(self, path):
        rel = path.split("?", 1)[0].lstrip("/")
        # landing page fronts the app: / -> landing.html, /studio -> the studio itself
        if rel == "":
            rel = "landing.html" if os.path.isfile(os.path.join(ROOT, "landing.html")) else "index.html"
        elif rel in ("studio", "studio/"):
            rel = "index.html"
        full = os.path.normpath(os.path.join(ROOT, rel))
        base = os.path.basename(full).lower()
        if (not full.startswith(ROOT + os.sep) or not os.path.isfile(full)
                or base.startswith(".") or base.startswith("env")
                or not base.endswith(self._SERVABLE)):
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # The app is one index.html that changes on every bugfix; Safari heuristically
            # caches responses with no cache headers, so users kept running a STALE app after
            # fixes shipped (and re-hitting bugs that were already dead). Images may cache;
            # the app shell must not.
            if base.endswith((".html", ".js", ".css")):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _authed(self):
        """One shared login gates the whole app + API. Constant-time compare.
        Gate is OFF until STUDIO_PASSWORD is set (local dev needs no login)."""
        if not S.STUDIO_PASS:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8", "ignore").partition(":")
                if hmac.compare_digest(user, S.STUDIO_USER) and hmac.compare_digest(pw, S.STUDIO_PASS):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="%s"' % S.APP_NAME)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authed():
            return
        if self.path.startswith("/api/health"):
            return self._json(200, {"ok": True, "app": S.APP_NAME,
                                    "falKey": bool(S.FAL_KEY),
                                    "openrouterKey": bool(S.OPENROUTER_KEY),
                                    "blotatoKey": bool(S.BLOTATO_KEY),
                                    "defaultAccountId": bool(S.BLOTATO_DEFAULT_ACCOUNT),
                                    "engine": "GPT Image 2 (Fal)"})
        if self.path.startswith("/api/brand"):
            s = _read_state()
            return self._json(200, {"brand": s}) if s else self._json(404, {"brand": None})
        if self.path.startswith("/api/library"):
            return self._library()
        if self.path.startswith("/api/subaccounts"):
            return self._subaccounts()
        if self.path.startswith("/api/accounts"):
            return self._accounts()
        return self._file(self.path)

    def do_POST(self):
        if not self._authed():
            return
        try:
            if self.path.startswith("/api/scrape"):
                url = (self._body().get("url") or "").strip()
                if not url:
                    return self._json(400, {"error": "no url"})
                try:
                    brand = scrape_brand(url)
                except Exception as e:
                    return self._json(502, {"error": "scrape failed: %s" % e})
                _write_state(brand)
                return self._json(200, {"brand": brand})

            if self.path.startswith("/api/store-image"):
                return self._store_image(self._body())

            if self.path.startswith("/api/save"):
                brand = self._body().get("brand") or self._body()
                _write_state(brand)
                return self._json(200, {"ok": True})

            if self.path.startswith("/api/generate"):
                return self._generate(self._body())

            if self.path.startswith("/api/schedule"):
                return self._schedule(self._body())

            if self.path.startswith("/api/ideas"):
                return self._ideas(self._body())

            return self._json(404, {"error": "unknown route"})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def _accounts(self):
        """Blotato accounts for the UI picker - so no account ID is ever hardcoded here."""
        if not S.BLOTATO_KEY:
            return self._json(400, {"error": "no BLOTATO_API_KEY in env.local"})
        try:
            st, body = S.blotato_req("/v2/users/me/accounts", None, timeout=30)
            items = body.get("items") or body.get("accounts") or body
            return self._json(200, {"accounts": items, "default": S.BLOTATO_DEFAULT_ACCOUNT or None})
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": "blotato accounts: %s" % e.read().decode("utf-8", "ignore")[:300]})
        except Exception as e:
            return self._json(502, {"error": "blotato accounts error: %s" % e})

    def _subaccounts(self):
        """Facebook Pages (or LinkedIn company pages / YouTube playlists) for one account -
        Blotato only exposes these via a per-account lookup, not on the main accounts list."""
        if not S.BLOTATO_KEY:
            return self._json(400, {"error": "no BLOTATO_API_KEY in env.local"})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        account = (qs.get("accountId") or [""])[0].strip()
        if not account:
            return self._json(400, {"error": "accountId required"})
        try:
            st, body = S.blotato_req("/v2/users/me/accounts/%s/subaccounts" % urllib.parse.quote(account), None, timeout=30)
            return self._json(200, {"subaccounts": body.get("items") or []})
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": "blotato subaccounts: %s" % e.read().decode("utf-8", "ignore")[:300]})
        except Exception as e:
            return self._json(502, {"error": "blotato subaccounts error: %s" % e})

    def _generate(self, req):
        prompt = (req.get("prompt") or "").strip()
        post_type = str(req.get("postType") or "carousel").strip().lower()
        # reference photos (product + mood + optional real logo). Fetched HERE and sent to Fal as
        # base64 data URIs, so brand CDNs that block Fal's datacenter IP can't break the render.
        raw_refs = [v for v in (S._vision_url(u) for u in (req.get("imageUrls") or []))
                    if v.startswith("https://") or v.startswith("data:")][:3]
        refs = [v if v.startswith("data:") else (S._to_data_uri(v) or v) for v in raw_refs]
        if not prompt:
            return self._json(400, {"error": "empty prompt"})
        if not S.FAL_KEY:
            return self._json(400, {"error": "No FAL_KEY found. Add it to env.local"})
        # with reference photos -> image-to-image (edit) so the REAL product appears; else text-to-image
        endpoint = S.FAL_EDIT_ENDPOINT if refs else S.FAL_ENDPOINT
        def fire(image_size):
            # num_images is ALWAYS 1 - one render at a time, the client loops if it needs more
            body = {"prompt": prompt, "image_size": image_size, "quality": "high",
                    "num_images": 1, "output_format": "png"}
            if refs:
                body["image_urls"] = refs
            r = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Key %s" % S.FAL_KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=360) as resp:
                out = json.loads(resp.read())
            return [img.get("url") for img in out.get("images", []) if img.get("url")]
        # edit endpoint wants multiple-of-16 dims; carousel accepts the portrait_4_3 preset on retry
        if post_type == "story":
            first_size = {"width": 1088, "height": 1920} if refs else {"width": 1080, "height": 1920}
        else:
            first_size = {"width": 1088, "height": 1360} if refs else {"width": 1080, "height": 1350}
        try:
            return self._json(200, {"images": fire(first_size), "mode": "edit" if refs else "text"})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            print("[generate] Fal %s (%s, %d refs): %s" % (e.code, "edit" if refs else "text", len(refs), detail), flush=True)
            if post_type != "story" and e.code == 422 and "image_size" in detail:
                try:
                    return self._json(200, {"images": fire("portrait_4_3"), "mode": "edit" if refs else "text"})
                except Exception as e2:
                    print("[generate] portrait_4_3 retry failed: %s" % e2, flush=True)
                    return self._json(502, {"error": "Fal retry failed: %s" % e2})
            return self._json(e.code, {"error": "Fal error: %s" % detail})
        except Exception as e:
            print("[generate] error: %s" % e, flush=True)
            return self._json(502, {"error": "generate error: %s" % e})

    def _resolve_media(self, u):
        """One image reference -> a Blotato-usable URL. Accepts either a real public URL (goes
        through /v2/media so Blotato mirrors it to a stable link) or a LOCAL renders/<brand>/...
        path (the same value the UI already displays via slideURL()) - those get uploaded
        straight from disk via the presigned-upload flow. This is the fix for scheduling failing
        even though a slide was clearly rendered and visible on screen: the browser's saved state
        can lose the original Fal URL (it expires, or a bug wiped it), but the local backup file
        in renders/ never does, so as long as the UI can show the slide, the server can now
        schedule it too."""
        if u.startswith(("http://", "https://")):
            st, body = S.blotato_req("/v2/media", {"url": u})
            if not body.get("url"):
                raise RuntimeError("blotato /v2/media returned no url")
            return body["url"]
        rel = u.lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT + os.sep) or not os.path.isfile(full):
            raise RuntimeError("local media file not found: %s" % u)
        return S.blotato_upload_file(full)

    # Per-platform required fields Blotato needs beyond accountId/platform/text/mediaUrls.
    # TikTok values match the defaults already used across Aniko's existing scheduled posts.
    _TIKTOK_DEFAULTS = {"isYourBrand": False, "autoAddMusic": False, "disabledDuet": False,
        "privacyLevel": "PUBLIC_TO_EVERYONE", "isAiGenerated": True, "disabledStitch": False,
        "disabledComments": False, "isBrandedContent": False}

    def _schedule(self, req):
        if not S.BLOTATO_KEY:
            return self._json(400, {"error": "scheduling not configured: set BLOTATO_API_KEY in env.local"})
        account = str(req.get("accountId") or "").strip() or S.BLOTATO_DEFAULT_ACCOUNT
        if not account:
            return self._json(400, {"error": "no account selected: pick an account in the Schedule panel (or set BLOTATO_IG_ACCOUNT_ID in env.local as a default)"})
        platform = str(req.get("platform") or "instagram").strip().lower()
        media_type = str(req.get("mediaType") or "").strip().lower()
        if platform not in ("instagram", "tiktok", "facebook", "pinterest", "twitter"):
            return self._json(400, {"error": "unsupported platform: %s" % platform})
        page_id = str(req.get("pageId") or "").strip()
        board_id = str(req.get("boardId") or "").strip()
        if platform == "facebook" and not page_id:
            return self._json(400, {"error": "Facebook needs a Page selected"})
        if platform == "pinterest" and not board_id:
            return self._json(400, {"error": "Pinterest needs a board selected"})
        caption = (req.get("caption") or "").strip()
        urls = req.get("imageUrls") or []
        sched = req.get("scheduledTime")
        if not urls:
            return self._json(400, {"error": "no imageUrls"})
        media = []
        for u in urls:
            try:
                media.append(self._resolve_media(u))
            except urllib.error.HTTPError as e:
                return self._json(502, {"error": "media upload failed: %s" % e.read().decode("utf-8", "ignore")[:300]})
            except Exception as e:
                return self._json(502, {"error": "media upload error (%s): %s" % (u, e)})
        if not media:
            return self._json(502, {"error": "no media uploaded"})
        if platform == "instagram":
            target = {"targetType": "instagram", "mediaType": "story"} if media_type == "story" else {"targetType": "instagram"}
        elif platform == "facebook":
            target = {"targetType": "facebook", "pageId": page_id}
            if media_type == "story":
                target["mediaType"] = "story"
        elif platform == "pinterest":
            target = {"targetType": "pinterest", "boardId": board_id}
        elif platform == "twitter":
            target = {"targetType": "twitter"}
        else:
            target = dict(self._TIKTOK_DEFAULTS, targetType="tiktok")
        post = {"accountId": account,
                "content": {"text": caption, "mediaUrls": media, "platform": platform},
                "target": target}
        payload = {"post": post}
        if sched:
            payload["scheduledTime"] = sched
        try:
            st, body = S.blotato_req("/v2/posts", payload)
            return self._json(200, {"ok": True, "httpStatus": st, "account": account,
                                    "platform": platform, "mediaCount": len(media), "blotato": body})
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": "post failed: %s" % e.read().decode("utf-8", "ignore")[:500]})
        except Exception as e:
            return self._json(500, {"error": "post error: %s" % e})

    def _ideas(self, req):
        if not S.OPENROUTER_KEY:
            return self._json(400, {"error": "no OPENROUTER_API_KEY in env.local"})
        brand = req.get("brand") or {}
        direction = req.get("direction") or ""
        n = max(1, min(8, int(req.get("count", 3))))
        # collect the real product + imagery photos so the vision model can SEE and match them.
        # keep each product's ORIGINAL index so productRef lines up with the UI's brand.productImages.
        prods = (brand.get("productImages") or [])[:6]
        usable = [(idx, p, S._vision_url(S._img_url(p))) for idx, p in enumerate(prods)]
        usable = [(idx, p, v) for idx, p, v in usable if v]
        moods = [v for v in (S._vision_url(S._img_url(m)) for m in (brand.get("imagery") or [])[:3]) if v]
        content = [{"type": "text", "text": S.ideas_prompt(brand, direction, n, len(usable))}]
        for idx, p, v in usable:
            nm = p.get("name", "") if isinstance(p, dict) else ""
            content.append({"type": "text", "text": "PRODUCT[%d]%s:" % (idx, (" - " + nm) if nm else "")})
            content.append({"type": "image_url", "image_url": {"url": v}})
        for v in moods:
            content.append({"type": "text", "text": "IMAGERY (overall mood / vibe reference):"})
            content.append({"type": "image_url", "image_url": {"url": v}})
        msgs = [
            {"role": "system", "content": "You are a world-class DTC creative director who writes scroll-stopping, on-brand Instagram carousels. You always reply with valid JSON only - no markdown fences, no preamble."},
            {"role": "user", "content": content},
        ]
        # prefer the Anthropic provider (handles https URLs + base64 reliably) for the vision call
        try:
            raw = S.openrouter_chat(msgs, model=S.IDEAS_MODEL, max_tokens=2600,
                                    provider={"order": ["Anthropic"], "allow_fallbacks": True})
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": "openrouter: %s" % e.read().decode("utf-8", "ignore")[:400]})
        except Exception as e:
            return self._json(502, {"error": "openrouter error: %s" % e})
        ideas = S.parse_ideas(raw)
        if not ideas:
            return self._json(502, {"error": "could not parse ideas JSON", "raw": raw[:400]})
        return self._json(200, {"ideas": ideas[:n], "model": S.IDEAS_MODEL, "products": len(prods)})

    def _library(self):
        """Every render saved under renders/, grouped by brand folder, newest first.
        Paths are ROOT-relative so the static file server can serve them directly."""
        base = os.path.join(ROOT, "renders")
        brands = []
        try:
            for slug in os.listdir(base):
                folder = os.path.join(base, slug)
                if not os.path.isdir(folder):
                    continue
                files = []
                for fn in os.listdir(folder):
                    if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        continue
                    files.append({"path": "renders/%s/%s" % (slug, fn),
                                  "mtime": int(os.path.getmtime(os.path.join(folder, fn)))})
                if files:
                    files.sort(key=lambda f: -f["mtime"])
                    brands.append({"brand": slug.replace("-", " ").title(), "files": files})
        except FileNotFoundError:
            pass
        brands.sort(key=lambda b: -b["files"][0]["mtime"])
        return self._json(200, {"brands": brands})

    def _store_image(self, req):
        """Download a rendered slide (Fal URL) to disk so it survives URL expiry + reloads.
        Saves to renders/<brand-slug>/post<id>_slide<n>_<role>.png (override dir with RENDER_OUT)."""
        url = (req.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return self._json(400, {"error": "need an http(s) image url"})
        slug = re.sub(r"[^a-z0-9]+", "-", (req.get("brand") or "brand").lower()).strip("-") or "brand"
        pid = re.sub(r"[^0-9A-Za-z]+", "", str(req.get("postId", "0"))) or "0"
        sn = re.sub(r"[^0-9]+", "", str(req.get("slide", "0"))) or "0"
        role = re.sub(r"[^a-z0-9]+", "", str(req.get("role", "")).lower())
        base = os.environ.get("RENDER_OUT") or os.path.join(ROOT, "renders")
        folder = os.path.join(base, slug)
        os.makedirs(folder, exist_ok=True)
        fname = "post%s_slide%s%s.png" % (pid, sn, ("_" + role) if role else "")
        dest = os.path.join(folder, fname)
        try:
            r = urllib.request.Request(url, headers={"User-Agent": S.UA})
            with urllib.request.urlopen(r, timeout=120) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
        except Exception as e:
            return self._json(502, {"error": "download failed: %s" % e})
        rel = "renders/%s/%s" % (slug, fname) if base == os.path.join(ROOT, "renders") else dest
        return self._json(200, {"ok": True, "path": rel, "bytes": len(data)})

    def log_message(self, *a):
        pass


def main():
    # Local dev binds to localhost (private). On a host (Railway sets PORT) bind 0.0.0.0 so it can route in.
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = int(os.environ.get("PORT", "5057"))
    print("%s -> http://%s:%d" % (S.APP_NAME, host, port))
    print("  FAL_KEY: %s   OPENROUTER: %s   BLOTATO: %s" % tuple(
        "loaded" if k else "missing" for k in (S.FAL_KEY, S.OPENROUTER_KEY, S.BLOTATO_KEY)))
    print("  access gate: %s" % ("ON (login required)" if S.STUDIO_PASS else "OFF (no STUDIO_PASSWORD - open, local only)"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
