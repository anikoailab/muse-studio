import os, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import services as S


class TestVisionUrl(unittest.TestCase):
    def test_https_passes(self):
        self.assertEqual(S._vision_url("https://x.com/a.png"), "https://x.com/a.png")

    def test_http_upgraded(self):
        self.assertEqual(S._vision_url("http://x.com/a.png"), "https://x.com/a.png")

    def test_protocol_relative(self):
        self.assertEqual(S._vision_url("//x.com/a.png"), "https://x.com/a.png")

    def test_svg_rejected(self):
        self.assertEqual(S._vision_url("https://x.com/logo.svg"), "")
        self.assertEqual(S._vision_url("data:image/svg+xml;base64,AAA"), "")

    def test_svg_with_query_rejected(self):
        self.assertEqual(S._vision_url("https://x.com/logo.svg?v=2"), "")

    def test_data_uri_passes(self):
        self.assertEqual(S._vision_url("data:image/png;base64,AAA"), "data:image/png;base64,AAA")

    def test_empty(self):
        self.assertEqual(S._vision_url(""), "")
        self.assertEqual(S._vision_url(None), "")


class TestImgUrl(unittest.TestCase):
    def test_string(self):
        self.assertEqual(S._img_url("https://a/b.png"), "https://a/b.png")

    def test_dict_url(self):
        self.assertEqual(S._img_url({"url": "u"}), "u")

    def test_dict_src(self):
        self.assertEqual(S._img_url({"src": "s"}), "s")

    def test_other(self):
        self.assertEqual(S._img_url(7), "")


class TestParseIdeas(unittest.TestCase):
    def test_plain_json(self):
        out = S.parse_ideas('[{"tone":"warm","title":"t","idea":"i","caption":"c","slides":[]}]')
        self.assertEqual(len(out), 1)

    def test_fenced_json(self):
        out = S.parse_ideas('```json\n[{"tone":"warm","slides":[]}]\n```')
        self.assertEqual(out[0]["tone"], "warm")

    def test_preamble_json(self):
        out = S.parse_ideas('Here you go: [{"tone":"x","slides":[]}] hope that helps')
        self.assertEqual(out[0]["tone"], "x")

    def test_ideas_object(self):
        out = S.parse_ideas('{"ideas":[{"tone":"y","slides":[]}]}')
        self.assertEqual(out[0]["tone"], "y")

    def test_garbage_returns_none(self):
        self.assertIsNone(S.parse_ideas("total garbage"))

    def test_em_dashes_stripped_everywhere(self):
        raw = ('[{"tone":"warm — cozy","title":"a – b","idea":"x — y",'
               '"caption":"hook — line","slides":[{"role":"HOOK","headline":"BIG — BOLD","badge":"new — in"}]}]')
        out = S.parse_ideas(raw)
        blob = str(out)
        self.assertNotIn("—", blob)  # em dash
        self.assertNotIn("–", blob)  # en dash
        self.assertIn("warm, cozy", out[0]["tone"])
        self.assertIn("BIG, BOLD", out[0]["slides"][0]["headline"])


class TestIdeasPrompt(unittest.TestCase):
    BRAND = {"name": "Patak's", "colors": ["#0b4da2", "#ffd200"], "voice": "bold, authentic",
             "voiceTags": ["bold"], "lang": "en"}

    def test_contains_brand_and_language(self):
        p = S.ideas_prompt(self.BRAND, "summer curry nights", 3, nprod=2)
        self.assertIn("Patak's", p)
        self.assertIn("summer curry nights", p)
        self.assertIn("LANGUAGE:", p)
        self.assertIn("productRef", p)
        self.assertIn("Never use em dashes", p)

    def test_no_lang_falls_back(self):
        b = dict(self.BRAND, lang="")
        p = S.ideas_prompt(b, "", 2)
        self.assertIn("Do NOT translate to English", p)

    def test_prompt_itself_has_no_em_dash(self):
        p = S.ideas_prompt(self.BRAND, "x", 1)
        self.assertNotIn("—", p)


class TestBranding(unittest.TestCase):
    def test_app_name(self):
        self.assertEqual(S.APP_NAME, "Muse Studio")


class TestBlotatoUploadFile(unittest.TestCase):
    def test_uploads_bytes_and_returns_public_url(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"fake-png-bytes")
            path = f.name
        try:
            fake_resp = mock.MagicMock()
            fake_resp.read.return_value = b""
            fake_resp.__enter__.return_value = fake_resp
            with mock.patch.object(S, "blotato_req",
                    return_value=(201, {"presignedUrl": "https://upload.example/x", "publicUrl": "https://cdn.example/x.png"})) as req, \
                 mock.patch("urllib.request.urlopen", return_value=fake_resp) as put:
                url = S.blotato_upload_file(path)
            self.assertEqual(url, "https://cdn.example/x.png")
            req.assert_called_once()
            self.assertEqual(req.call_args[0][0], "/v2/media/uploads")
            self.assertEqual(req.call_args[0][1]["filename"], os.path.basename(path))
            put.assert_called_once()
            self.assertEqual(put.call_args[0][0].full_url, "https://upload.example/x")
            self.assertEqual(put.call_args[0][0].data, b"fake-png-bytes")
        finally:
            os.unlink(path)

    def test_raises_on_missing_urls_in_response(self):
        with mock.patch.object(S, "blotato_req", return_value=(201, {})):
            with self.assertRaises(RuntimeError):
                S.blotato_upload_file("/tmp/whatever.png")


if __name__ == "__main__":
    unittest.main()
