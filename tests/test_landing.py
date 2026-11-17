import json
import tempfile
import unittest
from pathlib import Path

from app.landing import LandingCollector
from app.ledger import empty, load, record_scan, save
from app.registry import Registry
from app.report import chain_report, store_report
from app.service import collect_chain, run_batch
from app.transport import MappingTransport, TransportError
from app.urls import authorize_url, domain_matches, normalize_url, resolve_url


def registration(qr_id="qr-1", url="https://m.example.test/q/1", domains=("example.test",)):
    return Registry.from_data(
        {
            "registrations": [
                {
                    "qr_id": qr_id,
                    "store_id": "s-1",
                    "printed_url": url,
                    "allowed_domains": list(domains),
                }
            ]
        }
    ).claim("s-1", qr_id)


def page(body, **headers):
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8", **headers},
        "body": body,
    }


def site(**routes):
    return {url.replace("__", "://").replace("_", "."): value for url, value in routes.items()}


class URLPolicyTest(unittest.TestCase):
    def test_domain_boundaries(self):
        self.assertTrue(domain_matches("m.example.test", "example.test"))
        self.assertFalse(domain_matches("badexample.test", "example.test"))
        self.assertTrue(domain_matches("a.b.example.test", "example.test"))
        self.assertFalse(domain_matches("example.test", "*.example.test"))
        self.assertTrue(domain_matches("a.example.test", "*.example.test"))

    def test_authorization_rejects_other_scheme_and_credentials(self):
        self.assertEqual(authorize_url("https://m.example.test/x", ["example.test"]), (True, "example.test"))
        self.assertEqual(authorize_url("ftp://m.example.test/x", ["example.test"]), (False, None))
        self.assertEqual(authorize_url("https://user:pw@m.example.test/x", ["example.test"]), (False, None))

    def test_normalization_keeps_query_and_drops_fragment(self):
        self.assertEqual(
            normalize_url("HTTPS://M.Example.Test:443/a?b=1#x"),
            "https://m.example.test/a?b=1",
        )

    def test_relative_resolution(self):
        self.assertEqual(resolve_url("https://m.example.test/a/b", "../c"), "https://m.example.test/c")


class ChainTest(unittest.TestCase):
    def test_interleaved_http_meta_and_script_hops(self):
        routes = {
            "https://m.example.test/q/1": {"status": 302, "headers": {"Location": "/gate"}},
            "https://m.example.test/gate": page("<meta http-equiv='refresh' content='0;url=/mid'>"),
            "https://m.example.test/mid": page("<script>setTimeout(function(){location.href='/final'},2000)</script>"),
            "https://m.example.test/final": page("<body data-content-id='c-1'>final</body>"),
        }
        result = collect_chain(
            Registry.from_data(
                {
                    "registrations": [
                        {
                            "qr_id": "qr-1",
                            "store_id": "s-1",
                            "printed_url": "https://m.example.test/q/1",
                            "allowed_domains": ["example.test"],
                        }
                    ]
                }
            ),
            "s-1",
            "qr-1",
            MappingTransport(routes),
        )
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(
            [hop["transition"]["kind"] for hop in result["hops"][:-1]],
            ["http_redirect", "meta_refresh", "script_navigation"],
        )
        self.assertEqual(result["final_content"]["value"], "c-1")
        self.assertEqual(
            result["path"],
            [
                "https://m.example.test/q/1",
                "https://m.example.test/gate",
                "https://m.example.test/mid",
                "https://m.example.test/final",
            ],
        )
        # 只访问了链路中的页面，没有访问范围外地址。
        self.assertEqual(len(result["hops"]), 4)

    def test_declared_delay_is_recorded_not_waited(self):
        routes = {"https://m.example.test/q/1": page("<meta http-equiv='refresh' content='30;url=/next'>"),
                  "https://m.example.test/next": page("ok")}
        transport = MappingTransport(routes)
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(
            result["hops"][0]["transition"]["signal"]["declared_delay_seconds"], 30.0
        )

    def test_offsite_redirect_stops_and_never_fetches_target(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": {"status": 302, "headers": {"Location": "https://partner.test/x"}}}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["stop_reason"], "out_of_scope")
        self.assertEqual(result["failure"]["step"], 0)
        self.assertEqual(result["failure"]["url"], "https://partner.test/x")
        self.assertEqual(result["hops"][0]["transition"]["authorized"], False)
        self.assertEqual(
            [item["url"] for item in transport.requests], ["https://m.example.test/q/1"]
        )

    def test_offsite_meta_refresh_is_reported(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": page("<meta http-equiv='refresh' content='0;url=https://partner.test/x'>")}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure"]["signal"]["kind"], "meta_refresh")

    def test_offsite_script_navigation_is_reported(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": page("<script>location.assign('https://partner.test/x')</script>")}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["failure"]["signal"]["target"], "https://partner.test/x")

    def test_redirect_loop_stops_at_repeated_url(self):
        transport = MappingTransport(
            {
                "https://m.example.test/q/1": {"status": 301, "headers": {"Location": "/q/2"}},
                "https://m.example.test/q/2": {"status": 301, "headers": {"Location": "/q/1"}},
            }
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "stopped")
        self.assertEqual(result["stop_reason"], "redirect_loop")
        self.assertEqual(result["failure"]["url"], "https://m.example.test/q/1")
        self.assertEqual(len(transport.requests), 2)

    def test_meta_refresh_to_self_is_a_loop(self):
        transport = MappingTransport({"https://m.example.test/q/1": page("<meta http-equiv='refresh' content='5'>")})
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["stop_reason"], "navigation_loop")

    def test_script_reload_is_a_loop(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": page("<script>setTimeout(function(){location.reload()},1000)</script>")}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["stop_reason"], "navigation_loop")

    def test_competing_navigations_are_not_treated_as_loaded(self):
        body = (
            "<script>setTimeout(function(){location.href='/a'},0);"
            "setTimeout(function(){location.href='/b'},0);</script>"
        )
        transport = MappingTransport({"https://m.example.test/q/1": page(body)})
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "unfinished")
        self.assertEqual(result["stop_reason"], "ambiguous_navigation")
        self.assertIsNone(result["final_content"])
        self.assertEqual(len(transport.requests), 1)

    def test_dynamic_script_target_is_unfinished(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": page("<script>var base='/x'; location.href = base + window.q;</script>")}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["stop_reason"], "ambiguous_navigation")
        self.assertTrue(result["failure"]["dynamic"])

    def test_refresh_header_is_a_navigation(self):
        routes = {
            "https://m.example.test/q/1": {
                "status": 200,
                "headers": {"Content-Type": "text/html", "Refresh": "0; url=/final"},
                "body": "<p>wait</p>",
            },
            "https://m.example.test/final": page("<body data-content-id='c'>x</body>"),
        }
        result = LandingCollector(MappingTransport(routes)).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["hops"][0]["transition"]["kind"], "meta_refresh")

    def test_fragment_does_not_create_extra_hop(self):
        routes = {
            "https://m.example.test/q/1": page("<script>location.href='/final#top'</script>"),
            "https://m.example.test/final": page("<body data-content-id='c'>x</body>"),
        }
        transport = MappingTransport(routes)
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(result["path"][-1], "https://m.example.test/final")

    def test_location_header_without_redirect_status_is_recorded(self):
        routes = {
            "https://m.example.test/q/1": {
                "status": 200,
                "headers": {"Content-Type": "text/html", "Location": "/elsewhere"},
                "body": "<body data-content-id='c'>x</body>",
            }
        }
        result = LandingCollector(MappingTransport(routes)).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["hops"][0]["ignored_location"], "/elsewhere")

    def test_external_script_page_is_unfinished_not_final(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": page('<script src="/app.js"></script>')}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "unfinished")
        self.assertEqual(result["stop_reason"], "external_script")
        self.assertIsNone(result["final_content"])

    def test_page_title_does_not_complete_the_chain(self):
        # 只有一个无导航意图的页面才是最终页；标题本身不参与判定。
        transport = MappingTransport({"https://m.example.test/q/1": page("<title>活动</title>")})
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["final_content"]["kind"], "digest")
        self.assertEqual(result["hops"][0]["title"], "活动")

    def test_hop_budget_stops_with_position(self):
        routes = {f"https://m.example.test/q/{i}": {"status": 302, "headers": {"Location": f"/q/{i + 1}"}} for i in range(10)}
        transport = MappingTransport(routes)
        result = LandingCollector(transport, max_hops=3).collect(registration())
        self.assertEqual(result["outcome"], "unfinished")
        self.assertEqual(result["stop_reason"], "hop_budget")
        self.assertEqual(result["failure"]["step"], 3)
        self.assertEqual(len(transport.requests), 3)

    def test_transport_error_is_recorded(self):
        transport = MappingTransport(
            {"https://m.example.test/q/1": {"error": {"kind": "timeout", "detail": "请求超时"}}}
        )
        result = LandingCollector(transport).collect(registration())
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["failure"]["kind"], "timeout")
        self.assertEqual(result["verified_hops"], 0)

    def test_non_html_entity_uses_digest_identity(self):
        routes = {
            "https://m.example.test/q/1": {
                "status": 200,
                "headers": {"Content-Type": "application/pdf"},
                "body": "%PDF-1.4",
            }
        }
        result = LandingCollector(MappingTransport(routes)).collect(registration())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["final_content"]["kind"], "digest")

    def test_printed_url_must_match_registry(self):
        registry = Registry.from_data(
            {
                "registrations": [
                    {
                        "qr_id": "qr-1",
                        "store_id": "s-1",
                        "printed_url": "https://m.example.test/q/1",
                        "allowed_domains": ["example.test"],
                    }
                ]
            }
        )
        with self.assertRaises(ValueError):
            registry.claim("s-1", "qr-1", scanned_url="https://m.example.test/q/2")
        registry.claim("s-1", "qr-1", scanned_url="https://m.example.test/q/1")

    def test_unregistered_claim_is_rejected(self):
        registry = Registry.from_data(
            {
                "registrations": [
                    {
                        "qr_id": "qr-1",
                        "store_id": "s-1",
                        "printed_url": "https://m.example.test/q/1",
                        "allowed_domains": ["example.test"],
                    }
                ]
            }
        )
        with self.assertRaises(KeyError):
            registry.claim("s-2", "qr-1")
        with self.assertRaises(KeyError):
            registry.claim("s-1", "qr-9")


class LedgerTest(unittest.TestCase):
    def _result(self, qr_id="qr-1", identity="c-1", scan_id=None, at="2026-11-18T00:00:00+00:00"):
        return {
            "scan_id": scan_id or f"{qr_id}-{identity}-{at}",
            "collected_at": at,
            "store_id": "s-1",
            "qr_id": qr_id,
            "outcome": "complete",
            "stop_reason": None,
            "failure": None,
            "hops": [],
            "path": ["https://m.example.test/q/1", "https://m.example.test/final"],
            "verified_hops": 2,
            "pending_hop": None,
            "final_content": {"kind": "declared", "value": identity, "source": "body", "sha256": "0" * 64},
            "viewport": {},
            "allowed_domains": ["example.test"],
            "printed_url": "https://m.example.test/q/1",
        }

    def test_shared_final_page_keeps_separate_paths(self):
        db = empty()
        record_scan(db, {**self._result("qr-1"), "path": ["https://m.example.test/q/1", "https://m.example.test/a"]})
        record_scan(db, {**self._result("qr-2"), "path": ["https://m.example.test/q/2", "https://m.example.test/b"]})
        first = chain_report(db["chains"]["s-1/qr-1"])
        second = chain_report(db["chains"]["s-1/qr-2"])
        self.assertNotEqual(first["paths"][0]["path"], second["paths"][0]["path"])
        self.assertEqual(first["versions"][0]["identity"], second["versions"][0]["identity"])
        self.assertEqual(
            first["versions"][0]["shared_by"], ["s-1/qr-1", "s-1/qr-2"]
        )

    def test_version_change_marks_old_chain_invalid(self):
        db = empty()
        record_scan(db, self._result(identity="old", at="2026-11-18T00:00:00+00:00"))
        record_scan(db, self._result(identity="new", at="2026-11-19T00:00:00+00:00"))
        versions = chain_report(db["chains"]["s-1/qr-1"])["versions"]
        self.assertEqual([item["identity"] for item in versions], ["old", "new"])
        self.assertEqual(versions[0]["valid_until"], "2026-11-19T00:00:00+00:00")
        self.assertEqual(versions[0]["superseded_by"], 2)
        self.assertEqual(versions[1]["supersedes"], 1)
        self.assertEqual(versions[1]["valid_until"], None)

    def test_pending_scan_does_not_replace_version(self):
        db = empty()
        record_scan(db, self._result(identity="c-1"))
        pending = {
            **self._result(identity="c-1"),
            "scan_id": "pending-1",
            "outcome": "unfinished",
            "stop_reason": "ambiguous_navigation",
            "failure": {"kind": "ambiguous_navigation"},
            "verified_hops": 1,
            "pending_hop": {"from_step": 0, "to_url": "https://m.example.test/x"},
            "final_content": None,
        }
        record_scan(db, pending)
        report = chain_report(db["chains"]["s-1/qr-1"])
        self.assertEqual(len(report["versions"]), 1)
        self.assertEqual(len(report["pending"]), 1)
        self.assertEqual(report["pending"][0]["verified_hops"], 1)
        self.assertEqual(report["pending"][0]["pending_hop"]["to_url"], "https://m.example.test/x")
        self.assertEqual(report["pending"][0]["attempt"], 2)
        self.assertEqual(report["pending"][0]["retry_of"], report["completed"][0]["scan_id"])

    def test_duplicate_scan_id_is_rejected(self):
        db = empty()
        record_scan(db, self._result(scan_id="same"))
        with self.assertRaises(ValueError):
            record_scan(db, self._result(scan_id="same"))

    def test_save_and_load_roundtrip(self):
        db = empty()
        record_scan(db, self._result())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            save(path, db)
            loaded = load(path)
            self.assertEqual(loaded["chains"], db["chains"])
            self.assertEqual(load(Path(folder) / "missing.json"), empty())


class BatchTest(unittest.TestCase):
    def test_batch_reports_each_qr_separately(self):
        registry = Registry.from_data(
            {
                "registrations": [
                    {
                        "qr_id": "qr-1",
                        "store_id": "s-1",
                        "printed_url": "https://m.example.test/q/1",
                        "allowed_domains": ["example.test"],
                    },
                    {
                        "qr_id": "qr-2",
                        "store_id": "s-1",
                        "printed_url": "https://m.example.test/q/2",
                        "allowed_domains": ["example.test"],
                    },
                ]
            }
        )
        site_map = {
            "https://m.example.test/q/1": {"status": 302, "headers": {"Location": "/final"}},
            "https://m.example.test/q/2": page("<script>location.href='/final'</script>"),
            "https://m.example.test/final": page("<body data-content-id='same'>x</body>"),
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            out = run_batch(
                registry,
                MappingTransport(site_map),
                store_id="s-1",
                qr_ids=None,
                ledger_path=path,
                collected_at="2026-11-18T00:00:00+00:00",
            )
            by_qr = {item["qr_id"]: item for item in out["results"]}
            self.assertEqual(
                [hop["transition"]["kind"] for hop in by_qr["qr-1"]["hops"][:-1]],
                ["http_redirect"],
            )
            self.assertEqual(
                [hop["transition"]["kind"] for hop in by_qr["qr-2"]["hops"][:-1]],
                ["script_navigation"],
            )
            self.assertEqual(by_qr["qr-1"]["final_content"]["value"], "same")
            self.assertEqual(by_qr["qr-2"]["final_content"]["value"], "same")
            report = store_report(load(path), "s-1")
            self.assertEqual(
                report["shared_identities"],
                [{"identity": "same", "qr_ids": ["qr-1", "qr-2"]}],
            )
            self.assertEqual(len(report["chains"]), 2)


if __name__ == "__main__":
    unittest.main()
