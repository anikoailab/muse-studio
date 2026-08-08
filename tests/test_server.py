import base64, json, os, sys, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
import services as S


def _start(handler=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler or server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _get(url, headers=None, expect_error=False):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code, e.read()
        raise


def _post(url, payload, expect_error=False):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        if expect_error:
            return e.code, json.loads(e.read() or b"{}")
        raise


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = _start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_health_booleans_only(self):
        st, body = _get(self.base + "/api/health")
        j = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(j["app"], "Muse Studio")
        for k in ("falKey", "openrouterKey", "blotatoKey", "defaultAccountId"):
            self.assertIsInstance(j[k], bool)   # never actual key material
        blob = body.decode()
        for key in (S.FAL_KEY, S.OPENROUTER_KEY, S.BLOTATO_KEY):
            if key:
                self.assertNotIn(key, blob)

    def test_index_served(self):
        st, body = _get(self.base + "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Muse", body)

    def test_path_traversal_blocked(self):
        st, body = _get(self.base + "/..%2f..%2fetc%2fpasswd", expect_error=True)
        self.assertEqual(st, 404)
        st, body = _get(self.base + "/../env.local", expect_error=True)
        self.assertIn(st, (400, 404))

    def test_env_local_not_served(self):
        st, _ = _get(self.base + "/env.local", expect_error=True)
        self.assertEqual(st, 404)
        st, _ = _get(self.base + "/env.example", expect_error=True)
        self.assertEqual(st, 404)
        st, _ = _get(self.base + "/server.py", expect_error=True)
        self.assertEqual(st, 404)
        st, _ = _get(self.base + "/.gitignore", expect_error=True)
        self.assertEqual(st, 404)

    def test_unknown_post_404(self):
        st, j = _post(self.base + "/api/nope", {}, expect_error=True)
        self.assertEqual(st, 404)

    def test_scrape_requires_url(self):
        st, j = _post(self.base + "/api/scrape", {}, expect_error=True)
        self.assertEqual(st, 400)

    def test_generate_requires_prompt(self):
        st, j = _post(self.base + "/api/generate", {"prompt": ""}, expect_error=True)
        self.assertEqual(st, 400)

    def test_schedule_requires_account_or_default(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "BLOTATO_DEFAULT_ACCOUNT", ""):
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"]}, expect_error=True)
        self.assertEqual(st, 400)
        self.assertIn("account", j["error"].lower())

    def test_schedule_unsupported_platform_rejected(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"):
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "snapchat"}, expect_error=True)
        self.assertEqual(st, 400)

    def test_schedule_facebook_requires_page_id(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"):
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "facebook"}, expect_error=True)
        self.assertEqual(st, 400)
        self.assertIn("page", j["error"].lower())

    def test_schedule_local_media_path_traversal_blocked(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"):
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["../../../../etc/passwd"],
                           "accountId": "1", "platform": "instagram"}, expect_error=True)
        self.assertEqual(st, 502)

    def test_schedule_uploads_local_render_file(self):
        """The fix for 'render the carousel first' firing on a post that was clearly
        rendered: a local renders/<brand>/<file> path (what the UI shows via slideURL()) must
        be uploadable straight to Blotato, not just a live https url."""
        brand_dir = os.path.join(server.ROOT, "renders", "test-brand-xyz")
        os.makedirs(brand_dir, exist_ok=True)
        fpath = os.path.join(brand_dir, "post1_slide1_hook.png")
        with open(fpath, "wb") as f:
            f.write(b"fake")
        try:
            with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
                 mock.patch.object(S, "blotato_upload_file", return_value="https://cdn.example/uploaded.png") as up, \
                 mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1"})) as req:
                st, j = _post(self.base + "/api/schedule",
                              {"caption": "x", "imageUrls": ["renders/test-brand-xyz/post1_slide1_hook.png"],
                               "accountId": "1", "platform": "instagram"})
            self.assertEqual(st, 200)
            self.assertTrue(j["ok"])
            up.assert_called_once_with(fpath)
            req.assert_called_once()
            self.assertEqual(req.call_args[0][0], "/v2/posts")
            self.assertEqual(req.call_args[0][1]["post"]["content"]["mediaUrls"], ["https://cdn.example/uploaded.png"])
        finally:
            os.unlink(fpath)
            os.rmdir(brand_dir)

    def test_schedule_tiktok_builds_target_fields(self):
        # blotato_req is called twice here (/v2/media to resolve the http url, then /v2/posts) -
        # return a body with "url" so the /v2/media leg resolves, "id" so /v2/posts looks real
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "tiktok"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["targetType"], "tiktok")
        self.assertIn("privacyLevel", target)

    def test_schedule_instagram_story_sets_media_type(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "instagram", "mediaType": "story"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["mediaType"], "story")

    def test_schedule_facebook_story_sets_media_type(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"], "accountId": "1",
                           "platform": "facebook", "pageId": "p1", "mediaType": "story"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["pageId"], "p1")
        self.assertEqual(target["mediaType"], "story")

    def test_schedule_facebook_no_media_type_when_not_story(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"], "accountId": "1",
                           "platform": "facebook", "pageId": "p1"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["pageId"], "p1")
        self.assertNotIn("mediaType", target)

    def test_generate_story_uses_1920_height(self):
        original_urlopen = urllib.request.urlopen
        bodies = []

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"images": [{"url": "https://cdn.example/story.png"}]}).encode()

        def fake_urlopen(req, *args, **kwargs):
            if isinstance(req, urllib.request.Request) and req.full_url.startswith(self.base):
                return original_urlopen(req, *args, **kwargs)
            bodies.append(json.loads(req.data.decode()))
            return FakeResponse()

        with mock.patch.object(S, "FAL_KEY", "test-key"), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            st, j = _post(self.base + "/api/generate", {"prompt": "make it", "postType": "story"})
            self.assertEqual(st, 200)
            self.assertEqual(bodies[-1]["image_size"]["height"], 1920)
            st, j = _post(self.base + "/api/generate", {"prompt": "make it"})
            self.assertEqual(st, 200)
            self.assertEqual(bodies[-1]["image_size"]["height"], 1350)

    def test_schedule_pinterest_requires_board_id(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"):
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "pinterest"}, expect_error=True)
        self.assertEqual(st, 400)
        self.assertIn("board", j["error"].lower())

    def test_schedule_pinterest_builds_target_fields(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "pinterest", "boardId": "b1"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["targetType"], "pinterest")
        self.assertEqual(target["boardId"], "b1")

    def test_schedule_twitter_builds_target_fields(self):
        with mock.patch.object(S, "BLOTATO_KEY", "test-key"), \
             mock.patch.object(S, "blotato_req", return_value=(200, {"id": "p1", "url": "https://cdn.example/resolved.png"})) as req:
            st, j = _post(self.base + "/api/schedule",
                          {"caption": "x", "imageUrls": ["https://a/b.png"],
                           "accountId": "1", "platform": "twitter"})
        self.assertEqual(st, 200)
        target = req.call_args_list[-1][0][1]["post"]["target"]
        self.assertEqual(target["targetType"], "twitter")


class TestAuthGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw_patch = mock.patch.object(S, "STUDIO_PASS", "sesame")
        cls.user_patch = mock.patch.object(S, "STUDIO_USER", "muse")
        cls.pw_patch.start(); cls.user_patch.start()
        cls.srv, cls.base = _start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.pw_patch.stop(); cls.user_patch.stop()

    def test_unauthenticated_401(self):
        req = urllib.request.Request(self.base + "/api/health")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("Muse Studio", ctx.exception.headers.get("WWW-Authenticate", ""))

    def test_wrong_password_401(self):
        creds = base64.b64encode(b"muse:wrong").decode()
        req = urllib.request.Request(self.base + "/api/health",
                                     headers={"Authorization": "Basic " + creds})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_correct_login_passes(self):
        creds = base64.b64encode(b"muse:sesame").decode()
        st, body = _get(self.base + "/api/health", headers={"Authorization": "Basic " + creds})
        self.assertEqual(st, 200)

    def test_landing_page_public(self):
        st, body = _get(self.base + "/")
        self.assertEqual(st, 200)

    def test_studio_still_gated(self):
        st, _ = _get(self.base + "/studio", expect_error=True)
        self.assertEqual(st, 401)

    def test_sample_request_post_public(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, mock.patch.object(server, "RENDERS_DIR", td):
            st, j = _post(self.base + "/api/sample-request",
                          {"email": "lead@example.com", "website": "example.com"})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])

    def test_sample_requests_list_still_gated(self):
        st, _ = _get(self.base + "/api/sample-requests", expect_error=True)
        self.assertEqual(st, 401)


class TestIdeasRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.base = _start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_ideas_end_to_end_mocked(self):
        fake = json.dumps([{"tone": "warm", "title": "T — bold", "idea": "I", "caption": "C — hook",
                            "slides": [{"role": "HOOK", "headline": "H", "badge": "", "productRef": None}]}])
        with mock.patch.object(S, "OPENROUTER_KEY", "test"), \
             mock.patch.object(S, "openrouter_chat", return_value=fake):
            st, j = _post(self.base + "/api/ideas",
                          {"brand": {"name": "X", "colors": ["#111111"]}, "direction": "d", "count": 1})
        self.assertEqual(st, 200)
        self.assertEqual(len(j["ideas"]), 1)
        self.assertNotIn("—", json.dumps(j))  # server-side no-em-dash guarantee

    def test_ideas_unparseable_502(self):
        with mock.patch.object(S, "OPENROUTER_KEY", "test"), \
             mock.patch.object(S, "openrouter_chat", return_value="not json at all"):
            st, j = _post(self.base + "/api/ideas",
                          {"brand": {"name": "X"}, "count": 1}, expect_error=True)
        self.assertEqual(st, 502)


if __name__ == "__main__":
    unittest.main()
