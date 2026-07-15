import json, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as f:
        return f.read()


class TestParsingHelpers(unittest.TestCase):
    HTML = fixture("relative-images.html")

    def test_relative_images_resolved(self):
        urls = scraper._all_images(self.HTML, "https://example.com")
        self.assertIn("https://example.com/images/product-1.jpg", urls)
        self.assertIn("https://example.com/assets/hero.webp", urls)      # data-src lazy
        self.assertIn("https://cdn.example.com/abs.png", urls)           # absolute kept

    def test_srcset_first_url(self):
        urls = scraper._all_images(self.HTML, "https://example.com")
        self.assertIn("https://example.com/srcset-small.jpg", urls)

    def test_logo_found_not_favicon(self):
        logo = scraper._find_logo(self.HTML, "https://example.com")
        self.assertEqual(logo, "https://example.com/img/brand-logo.png")

    def test_products_skip_junk(self):
        prods = scraper._html_products(self.HTML, "Example", "https://example.com")
        urls = [p["url"] for p in prods]
        self.assertIn("https://example.com/images/product-1.jpg", urls)
        self.assertNotIn("https://example.com/img/brand-logo.png", urls)
        self.assertNotIn("https://example.com/icons/facebook.png", urls)

    def test_products_skip_theme_decoration(self):
        prods = scraper._html_products(self.HTML, "Example", "https://example.com")
        urls = [p["url"] for p in prods]
        self.assertFalse(any("/themes/" in u for u in urls))

    def test_products_skip_hero_and_stock_photos(self):
        prods = scraper._html_products(self.HTML, "Example", "https://example.com")
        urls = [p["url"] for p in prods]
        self.assertFalse(any("hero" in u.lower() for u in urls))
        self.assertFalse(any("shutterstock" in u.lower() for u in urls))

    def test_shop_links_found(self):
        links = scraper._shop_links(self.HTML, "https://example.com")
        self.assertIn("https://example.com/products", links)

    def test_shop_links_ignore_homepage(self):
        html = '<a href="/">Home</a><a href="/product/">Shop</a>'
        links = scraper._shop_links(html, "https://example.com")
        self.assertEqual(links, ["https://example.com/product/"])

    def test_shop_links_dedupe_trailing_slash(self):
        html = '<a href="/products/">Shop</a><a href="/products">Shop</a>'
        links = scraper._shop_links(html, "https://example.com")
        self.assertEqual(len(links), 1)

    def test_category_links_found_on_hub(self):
        links = scraper._category_links(fixture("shop-hub.html"), "https://example.com",
                                         exclude_url="https://example.com/products")
        self.assertIn("https://example.com/categories-products/sauces", links)
        self.assertIn("https://example.com/categories-products/pastes", links)

    def test_category_links_exclude_locale_and_feed(self):
        links = scraper._category_links(fixture("shop-hub.html"), "https://example.com",
                                         exclude_url="https://example.com/products")
        self.assertFalse(any("/fr/" in l for l in links))
        self.assertFalse(any("/feed" in l for l in links))

    def test_category_links_exclude_self(self):
        links = scraper._category_links(fixture("shop-hub.html"), "https://example.com",
                                         exclude_url="https://example.com/categories-products/sauces")
        self.assertNotIn("https://example.com/categories-products/sauces", links)

    def test_dedupe_by_base_prefers_no_suffix(self):
        urls = ["https://x.com/sauce-korma-300x300.png", "https://x.com/sauce-korma-150x150.png",
                "https://x.com/sauce-korma.png"]
        self.assertEqual(scraper._dedupe_by_base(urls), ["https://x.com/sauce-korma.png"])

    def test_dedupe_by_base_picks_largest_when_no_original(self):
        urls = ["https://x.com/sauce-korma-150x150.png", "https://x.com/sauce-korma-300x300.png"]
        self.assertEqual(scraper._dedupe_by_base(urls), ["https://x.com/sauce-korma-300x300.png"])

    def test_shop_products_crawls_listing_page(self):
        shop_html = fixture("shop-listing.html")
        # /products is a bare hub URL - _fetch_shop_page tries the browser first for those;
        # simulate "no chrome" (same as CI/Railway) so the plain ladder (mocked _fetch) runs.
        with mock.patch.object(scraper, "_fetch", return_value=shop_html), \
             mock.patch.object(scraper, "_fetch_browser", side_effect=RuntimeError("no chrome in tests")):
            prods = scraper._shop_products(self.HTML, "https://example.com", "Example")
        urls = [p["url"] for p in prods]
        self.assertIn("https://example.com/wp-content/uploads/2022/05/sauce-korma.png", urls)
        self.assertIn("https://example.com/wp-content/uploads/2022/05/sauce-madras-300x300.png", urls)
        # the no-suffix original wins over its own -300x300/-150x150 thumbnail variants
        self.assertEqual(sum("sauce-korma" in u for u in urls), 1)
        self.assertFalse(any("/themes/" in u for u in urls))

    def test_shop_products_two_level_hub_crawl(self):
        """The real fix: a hub page (/products) that links to multiple category sub-pages
        (Cooking Sauces, Spice Pastes, ...) must contribute photos from EVERY category it
        links to, not just the first one - otherwise the render model only ever sees one
        product family and either repeats it or invents a product that isn't on the site."""
        hub_html = fixture("shop-hub.html")
        sauces_html = fixture("category-sauces.html")
        pastes_html = fixture("category-pastes.html")

        def fake_browser(url):
            if url.rstrip("/") == "https://example.com/products":
                return hub_html
            raise RuntimeError("unexpected browser fetch: %s" % url)

        def fake_fetch(url, timeout=30, ua=None):
            if "sauces" in url:
                return sauces_html
            if "pastes" in url:
                return pastes_html
            raise RuntimeError("unexpected fetch: %s" % url)

        homepage_html = '<a href="/products">Shop</a>'
        with mock.patch.object(scraper, "_fetch_browser", side_effect=fake_browser), \
             mock.patch.object(scraper, "_fetch", side_effect=fake_fetch):
            prods = scraper._shop_products(homepage_html, "https://example.com", "Example")

        urls = [p["url"] for p in prods]
        self.assertTrue(any("sauce" in u for u in urls), "missing sauces category photos")
        self.assertTrue(any("paste" in u for u in urls), "missing pastes category photos")
        # capped at 2 real photos per category - broad across categories, not deep in one
        self.assertLessEqual(sum("sauce-" in u for u in urls), 2)
        self.assertLessEqual(sum("paste-" in u for u in urls), 2)

    def test_images_exclude_logo_and_packshots_dedupe(self):
        """Imagery is the lifestyle/mood board: no logo, no product packshots (PNG), and WP
        thumbnail variants of one dish photo collapse to a single tile."""
        html = (
            '<meta property="og:image" content="/img/brand-logo.png">'          # logo og -> skip
            '<img src="/uploads/hero-butter-chicken.jpg">'                        # hero keyword -> in
            '<img src="/uploads/thai-green-curry.jpeg">'                          # dish jpg fallback
            '<img src="/uploads/thai-green-curry-300x200.jpeg">'                  # same dish, thumb
            '<img src="/uploads/thai-green-curry-1024x656.jpeg">'                 # same dish, thumb
            '<img src="/uploads/sauce-korma.png">'                               # packshot PNG -> out
        )
        imgs = scraper._images(html, "https://example.com")
        self.assertFalse(any("logo" in u.lower() for u in imgs))
        self.assertFalse(any(u.lower().endswith(".png") for u in imgs))         # no packshots
        self.assertEqual(sum("thai-green-curry" in u for u in imgs), 1)          # variants collapsed
        self.assertTrue(any("hero-butter-chicken" in u for u in imgs))

    def test_lang_from_html_tag(self):
        self.assertEqual(scraper._lang('<html lang="fr-CA"><head></head></html>'), "fr-CA")

    def test_lang_from_og_locale(self):
        html = '<html><meta property="og:locale" content="de_CH"></html>'
        self.assertEqual(scraper._lang(html), "de-CH")

    def test_colors_filter_junk(self):
        html = "body{color:#007bff;background:#e91e63;border:#e91e63;outline:#e91e63}"
        cols = scraper._colors(html)
        self.assertIn("#e91e63", cols)
        self.assertNotIn("#007bff", cols)  # Bootstrap default filtered

    def test_colors_empty_when_nothing(self):
        self.assertEqual(scraper._colors("body{color:#ffffff;background:#000000}"), [])

    def test_pretty_font(self):
        self.assertEqual(scraper._pretty_font("museo_slab"), "Museo Slab")
        self.assertEqual(scraper._pretty_font('"Open Sans", sans-serif'), "Open Sans")

    def test_visible_text_strips_tags(self):
        txt = scraper._visible_text("<script>var x=1;</script><p>Hello <b>world</b></p>")
        self.assertIn("Hello world", txt)
        self.assertNotIn("var x", txt)


class TestBlockDetection(unittest.TestCase):
    """Bot-wall challenge pages must never be parsed as the brand's site. Regression for
    superpatch.com (2026-07-14): Cloudflare's 403 interstitial came back through the
    headless-Chrome rung and was saved as a brand literally named 'Attention Required!'."""
    CF_403 = ('<html><head><title>Attention Required! | Cloudflare</title></head>'
              '<body>Sorry, you have been blocked</body></html>')
    CF_JS = ('<html><head><title>Just a moment...</title>'
             '<script src="/cdn-cgi/challenge-platform/h/b.js"></script></head></html>')
    AKAMAI = ('<html><head><title>Access Denied</title></head>'
              '<body>Reference errors.edgesuite.net</body></html>')
    READER_BLOCKED = "Title: Attention Required! | Cloudflare\n\nSorry, you have been blocked"
    NORMAL = ('<html><head><title>Everyday Wellness</title></head><body><h1>Welcome</h1>'
              '<p>Our post about Cloudflare outages and access denied errors.</p></body></html>')

    def test_challenge_pages_detected(self):
        for page in (self.CF_403, self.CF_JS, self.AKAMAI, self.READER_BLOCKED):
            self.assertTrue(scraper._looks_blocked(page))

    def test_normal_page_mentioning_block_words_passes(self):
        self.assertFalse(scraper._looks_blocked(self.NORMAL))

    def test_browser_rung_rejects_challenge_dom(self):
        # --dump-dom returns the DOM regardless of HTTP status - the exact leak that
        # let the Cloudflare page through. Pad past the 500-char emptiness check.
        dom = self.CF_403 + "<!-- " + "x" * 600 + " -->"
        fake = mock.Mock(stdout=dom.encode())
        with mock.patch.object(scraper, "_chrome_exe", return_value="/fake/chrome"), \
             mock.patch.object(scraper.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError):
                scraper._fetch_browser("https://walled.example")

    def test_ladder_escalates_past_blocked_plain_fetch(self):
        # Plain fetch returns a 200-status challenge page; the ladder must not accept it
        # and must keep climbing until a rung returns real content (here: the reader).
        with mock.patch.object(scraper, "_fetch",
                               side_effect=lambda url, timeout=30, ua=None: self.CF_403), \
             mock.patch.object(scraper, "_fetch_browser", side_effect=RuntimeError("bot wall")), \
             mock.patch.object(scraper, "_fetch_jina", return_value="Title: Super Patch\n\nReal content here"):
            content, kind = scraper._fetch_any("https://walled.example")
        self.assertEqual(kind, "text")
        self.assertIn("Real content", content)

    def test_all_rungs_blocked_raises_clear_error(self):
        with mock.patch.object(scraper, "_fetch",
                               side_effect=lambda url, timeout=30, ua=None: self.CF_403), \
             mock.patch.object(scraper, "_fetch_browser", side_effect=RuntimeError("bot wall")), \
             mock.patch.object(scraper, "_fetch_jina", return_value=self.READER_BLOCKED):
            with self.assertRaises(RuntimeError) as ctx:
                scraper._fetch_any("https://walled.example")
        self.assertIn("bot protection", str(ctx.exception))


class TestScrapeStatic(unittest.TestCase):
    def test_thin_site_parses(self):
        """bluedragon-thin.html mimics the site the old app failed on: colors only in an
        image-based identity, sparse metadata. Pass 1 must still return a well-formed brand."""
        html = fixture("bluedragon-thin.html")
        with mock.patch.object(scraper, "_fetch_any", return_value=(html, "html")), \
             mock.patch.object(scraper, "_linked_css", return_value=""), \
             mock.patch.object(scraper, "_shopify_products", return_value=[]):
            brand, raw = scraper.scrape_static("https://bluedragon.ca")
        self.assertEqual(brand["name"], "Blue Dragon")
        self.assertEqual(brand["url"], "bluedragon.ca")
        self.assertIsInstance(brand["colors"], list)
        self.assertIn("warnings", brand)

    def test_reader_fallback_minimal_brand(self):
        with mock.patch.object(scraper, "_fetch_any", return_value=("Some text about the brand", "text")):
            brand, raw = scraper.scrape_static("https://walled.example")
        self.assertEqual(brand["name"], "Walled")
        self.assertTrue(brand["warnings"])


class TestEnrichment(unittest.TestCase):
    def _thin_brand(self):
        # has a real product photo -> verify_colors triggers regardless of how many CSS
        # colors were already scraped (that's the whole point: CSS colors can be plentiful
        # and still wrong, e.g. website button colors that don't match the packaging)
        return {"name": "Blue Dragon", "colors": ["#e91e63", "#111111", "#222222"], "voice": "",
                "voiceTags": [], "imagery": [], "logo": "",
                "productImages": [{"url": "https://example.com/product.jpg"}],
                "fonts": {"display": "Inter"}, "warnings": []}

    def test_enrich_fills_weak_fields(self):
        ai = json.dumps({"colors": ["#0a3d8f", "#d42a2a", "#f5c518", "#ffffff"],
                         "voice": "Bold wok-fired flavour for home cooks.",
                         "voiceTags": ["bold", "fiery"], "fontVibe": "sans"})
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "_to_data_uri", return_value="data:image/jpeg;base64,AAA"), \
             mock.patch.object(scraper, "openrouter_chat", return_value=ai):
            brand, enriched = scraper.enrich_brand(self._thin_brand(), "<p>Wok-fired sauces</p>")
        self.assertGreaterEqual(len(brand["colors"]), 3)
        self.assertEqual(brand["colors"][0], "#0a3d8f")   # AI (real photo) dominant first
        self.assertIn("#e91e63", brand["colors"])          # scraped color kept as secondary hint
        self.assertTrue(brand["voice"])
        self.assertIn("colors", enriched)
        self.assertIn("voice", enriched)

    def test_enrich_overrides_even_with_plenty_of_css_colors(self):
        """The exact bug reported: a site can have 3+ CSS colors and still be completely
        wrong about the product's real color (CSS colors were nav/button chrome, not
        packaging). A real product photo must always win."""
        ai = json.dumps({"colors": ["#1a9b96", "#5a2d82"], "voice": "", "voiceTags": [], "fontVibe": "sans"})
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "_to_data_uri", return_value="data:image/jpeg;base64,AAA"), \
             mock.patch.object(scraper, "openrouter_chat", return_value=ai):
            brand, enriched = scraper.enrich_brand(self._thin_brand(), "text")
        self.assertEqual(brand["colors"][0], "#1a9b96")
        self.assertEqual(brand["colors"][1], "#5a2d82")
        self.assertIn("colors", enriched)

    def test_enrich_failure_never_raises(self):
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "_to_data_uri", return_value="data:image/jpeg;base64,AAA"), \
             mock.patch.object(scraper, "openrouter_chat", side_effect=RuntimeError("boom")):
            brand, enriched = scraper.enrich_brand(self._thin_brand(), "<p>text</p>")
        self.assertEqual(enriched, [])
        self.assertTrue(any("enrichment failed" in w.lower() for w in brand["warnings"]))

    def test_enrich_skipped_when_strong_and_no_photo(self):
        strong = {"name": "X", "colors": ["#111111", "#222222", "#333333"],
                  "voice": "A confident brand voice with plenty of substance here.",
                  "voiceTags": ["bold"], "imagery": [], "logo": "", "productImages": [],
                  "fonts": {}, "warnings": []}
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "openrouter_chat", side_effect=AssertionError("should not be called")):
            brand, enriched = scraper.enrich_brand(strong, "text")
        self.assertEqual(enriched, [])

    def test_enrich_still_runs_for_voice_even_with_no_photo(self):
        strong_colors_weak_voice = {"name": "X", "colors": ["#111111", "#222222", "#333333"],
                  "voice": "", "voiceTags": [], "imagery": [], "logo": "", "productImages": [],
                  "fonts": {}, "warnings": []}
        ai = json.dumps({"colors": ["#0a3d8f"], "voice": "A confident new voice.", "voiceTags": ["bold"]})
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "openrouter_chat", return_value=ai):
            brand, enriched = scraper.enrich_brand(strong_colors_weak_voice, "text")
        self.assertIn("voice", enriched)
        self.assertNotIn("colors", enriched)  # no photo to verify against - CSS colors untouched
        self.assertEqual(brand["colors"], ["#111111", "#222222", "#333333"])

    def test_enrich_strips_em_dashes_from_voice(self):
        ai = json.dumps({"colors": ["#0a3d8f", "#d42a2a", "#f5c518"],
                         "voice": "Bold — fiery — fun.", "voiceTags": [], "fontVibe": "sans"})
        with mock.patch.object(scraper, "OPENROUTER_KEY", "test"), \
             mock.patch.object(scraper, "_to_data_uri", return_value="data:image/jpeg;base64,AAA"), \
             mock.patch.object(scraper, "openrouter_chat", return_value=ai):
            brand, _ = scraper.enrich_brand(self._thin_brand(), "t")
        self.assertNotIn("—", brand["voice"])


class TestScrapeBrand(unittest.TestCase):
    def test_default_palette_when_empty(self):
        empty = {"name": "X", "colors": [], "voice": "long enough voice text to not be weak......",
                 "voiceTags": ["a"], "imagery": [], "logo": "", "fonts": {}, "warnings": []}
        with mock.patch.object(scraper, "scrape_static", return_value=(empty, "")), \
             mock.patch.object(scraper, "enrich_brand", side_effect=lambda b, h: (b, [])):
            brand = scraper.scrape_brand("https://x.example")
        self.assertTrue(brand["colors"])
        self.assertEqual(brand["sources"]["colors"], "default")
        self.assertTrue(any("manually" in w for w in brand["warnings"]))


if __name__ == "__main__":
    unittest.main()
