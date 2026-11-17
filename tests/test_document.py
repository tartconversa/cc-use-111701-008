import unittest

from app.htmlnav import analyze, parse_meta_refresh
from app.registry import Registry
from app.viewport import MOBILE_VIEWPORT, validate_viewport


def signals(body, url="https://m.example.test/start", content_type="text/html; charset=utf-8"):
    return analyze(body.encode("utf-8"), content_type, url).signals


class MetaRefreshTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_meta_refresh("0; url=/x"), (0.0, "/x"))
        self.assertEqual(parse_meta_refresh("5"), (5.0, None))
        self.assertEqual(parse_meta_refresh("0;URL='/y'"), (0.0, "/y"))
        self.assertEqual(parse_meta_refresh('0; url="/z"'), (0.0, "/z"))
        self.assertEqual(parse_meta_refresh("abc"), (None, None))
        self.assertEqual(parse_meta_refresh(""), (None, None))

    def test_case_and_spacing(self):
        found = signals('<META HTTP-EQUIV="Refresh" CONTENT="1; URL = /Up">')
        self.assertEqual(found[0].target, "https://m.example.test/Up")
        self.assertEqual(found[0].delay_seconds, 1.0)

    def test_self_refresh_is_a_signal_to_current_url(self):
        found = signals("<meta http-equiv='refresh' content='0'>")
        self.assertEqual(found[0].target, "https://m.example.test/start")

    def test_relative_target_uses_base(self):
        found = signals('<base href="https://m.example.test/dir/"><meta http-equiv="refresh" content="0;url=next">')
        self.assertEqual(found[0].target, "https://m.example.test/dir/next")

    def test_comments_and_noscript_are_ignored(self):
        body = (
            "<!-- <meta http-equiv='refresh' content='0;url=/fake'> -->"
            "<noscript><meta http-equiv='refresh' content='0;url=/fake2'></noscript>"
            "<template><meta http-equiv='refresh' content='0;url=/fake3'></template>"
        )
        self.assertEqual(signals(body), [])


class ScriptNavigationTest(unittest.TestCase):
    def test_assignment_replace_and_assign(self):
        body = (
            "<script>location.href='/a'; window.location.replace('/b');"
            " document.location.assign('/c');</script>"
        )
        self.assertEqual(
            [signal.target for signal in signals(body)],
            ["https://m.example.test/a", "https://m.example.test/b", "https://m.example.test/c"],
        )

    def test_declared_delay_recorded(self):
        found = signals("<script>setTimeout(function(){location.href='/slow'}, 2500)</script>")
        self.assertEqual(found[0].delay_seconds, 2500.0)

    def test_dynamic_target_is_flagged(self):
        found = signals("<script>var u='/x'; location.href = u + q;</script>")
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].target)
        self.assertTrue(found[0].dynamic)

    def test_string_and_comment_handling(self):
        body = "<script>var u='https://m.example.test/keep'; // location.href='/fake'\nlocation.href='/real';</script>"
        self.assertEqual([s.target for s in signals(body)], ["https://m.example.test/real"])

    def test_reload_targets_current_document(self):
        found = signals("<script>location.reload()</script>")
        self.assertEqual(found[0].target, "https://m.example.test/start")

    def test_dom_click_uses_registered_element(self):
        body = (
            "<a id='go' href='/enter'>x</a>"
            "<script>document.getElementById('go').click()</script>"
        )
        found = signals(body)
        self.assertEqual([s.target for s in found], ["https://m.example.test/enter"])

    def test_unknown_element_click_is_dynamic(self):
        found = signals("<script>document.getElementById('missing').click()</script>")
        self.assertTrue(found[0].dynamic)

    def test_data_attribute_auto_navigation(self):
        found = signals("<a href='/ad' data-auto-navigate>ad</a>")
        self.assertEqual(found[0].target, "https://m.example.test/ad")

    def test_window_open_and_form_submit(self):
        found = signals("<script>window.open('/new'); document.forms[0].submit();</script>")
        self.assertEqual(found[0].target, "https://m.example.test/new")
        self.assertTrue(found[1].dynamic)

    def test_inline_event_handler_is_analysed(self):
        found = signals("<body onload=\"location.href='/landing'\">x</body>")
        self.assertEqual(found[0].target, "https://m.example.test/landing")
        self.assertIn("onload", found[0].source)

    def test_external_script_is_recorded(self):
        analysis = analyze(b'<script src="/app.js"></script>', "text/html", "https://m.example.test/x")
        self.assertEqual(analysis.external_scripts[0]["src"], "https://m.example.test/app.js")

    def test_non_script_types_are_not_executed(self):
        body = (
            '<script type="application/json">{"url": "location.href=/x"}</script>'
            '<script src="/external.js"></script>'
        )
        self.assertEqual(signals(body), [])


class ContentIdentityTest(unittest.TestCase):
    def test_explicit_identity_sources(self):
        analysis = analyze(b"<body data-content-id='c-9'>x</body>", "text/html", "https://m.example.test/x")
        self.assertEqual(analysis.explicit_content_id, "c-9")
        self.assertEqual(analysis.content_id_source, "body[data-content-id]")

    def test_title_is_metadata_only(self):
        analysis = analyze(b"<title>loading</title><script>location.href='/next'</script>", "text/html", "https://m.example.test/x")
        self.assertEqual(analysis.title, "loading")
        self.assertEqual(analysis.signals[0].target, "https://m.example.test/next")


class ViewportTest(unittest.TestCase):
    def test_defaults_are_frozen_mobile(self):
        viewport = validate_viewport(None)
        self.assertEqual(viewport.width, MOBILE_VIEWPORT["width"])
        self.assertIn("Mobile", viewport.request_headers["User-Agent"])
        with self.assertRaises(Exception):
            viewport.width = 1

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            validate_viewport({"width": 0})
        with self.assertRaises(ValueError):
            validate_viewport({"unknown": 1})
        with self.assertRaises(ValueError):
            validate_viewport({"user_agent": "x\r\ny"})


class RegistryValidationTest(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "qr_id": "qr-1",
            "store_id": "s-1",
            "printed_url": "https://m.example.test/q/1",
            "allowed_domains": ["example.test"],
        }
        row.update(overrides)
        return {"registrations": [row]}

    def test_printed_url_must_be_authorized(self):
        with self.assertRaises(ValueError):
            Registry.from_data(self._row(printed_url="https://partner.test/q/1"))

    def test_empty_domains_rejected(self):
        with self.assertRaises(ValueError):
            Registry.from_data(self._row(allowed_domains=[]))

    def test_duplicate_identity_rejected(self):
        data = self._row()
        data["registrations"].append(dict(data["registrations"][0]))
        with self.assertRaises(ValueError):
            Registry.from_data(data)

    def test_single_viewport_applies_to_all(self):
        data = self._row()
        data["viewports"] = [{"width": 412, "height": 915}]
        registry = Registry.from_data(data)
        self.assertEqual(registry.claim("s-1", "qr-1").viewport.width, 412)


if __name__ == "__main__":
    unittest.main()
