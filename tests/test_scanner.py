import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import main as scanner


ROOM = {
    "pk": 123, "object_category": "SHARED", "offer_type": "RENT", "status": "act",
    "country": "CH", "rent_gross": 950, "rent_net": None, "rent_charges": None,
    "price_unit": "monthly", "latitude": 47.378178, "longitude": 8.540218,
    "description_title": "WG-Zimmer Zürich", "public_address": "8001 Zürich",
    "url": "/en/flat/8001-zurich/123/", "is_furnished": True,
}
CENTER = (47.378178, 8.540218, "Zürich HB")


class StubClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get_json(self, url):
        self.urls.append(url)
        return next(self.responses)


class ScannerTests(unittest.TestCase):
    def options(self, *args):
        return scanner.parse_args(list(args))

    def test_distance_and_bounding_box_enclose_cardinal_points(self):
        self.assertEqual(scanner.distance_km(47, 8, 47, 8), 0)
        self.assertAlmostEqual(scanner.distance_km(0, 0, 1, 0), 111.195, places=3)
        box = scanner.bounding_box(*CENTER[:2], 5)
        self.assertAlmostEqual(scanner.distance_km(*CENTER[:2], box["north"], CENTER[1]), 5, places=6)
        self.assertLess(box["south"], CENTER[0])
        self.assertLess(box["west"], CENTER[1])
        self.assertGreater(box["east"], CENTER[1])

    def test_only_active_rooms_in_price_and_distance_range_pass(self):
        rows = [copy.deepcopy(ROOM)]
        for change in ({"rent_gross": 1300}, {"longitude": 8.8}, {"status": "ina"},
                       {"object_category": "APARTMENT"}, {"offer_type": "SALE"},
                       {"latitude": None}, {"price_unit": "daily"}, {"country": "DE"}):
            rows.append(dict(ROOM, **change))
        matches, _ = scanner.filter_listings(rows, self.options(), *CENTER[:2])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["price_chf"], 950)
        self.assertEqual(matches[0]["url"], "https://flatfox.ch/en/flat/8001-zurich/123/")

    def test_price_cap_uses_total_including_utilities(self):
        room = dict(ROOM, rent_gross=None, rent_net=1000, rent_charges=250, price_display=700)
        self.assertEqual(scanner.monthly_rent(room), 1250)
        self.assertEqual(scanner.filter_listings([room], self.options(), *CENTER[:2])[0], [])
        room["rent_charges"] = 0
        self.assertEqual(scanner.monthly_rent(room), 1000)

    def test_unknown_charges_or_nonfinite_price_never_count_as_free(self):
        for gross, net, charges in ((None, 900, None), ("nan", None, None), (0, None, None)):
            self.assertIsNone(scanner.monthly_rent(dict(ROOM, rent_gross=gross, rent_net=net, rent_charges=charges)))

    def test_inner_radius_and_furnished_filters(self):
        options = self.options("--min-distance-km", "1")
        self.assertEqual(scanner.filter_listings([ROOM], options, *CENTER[:2])[0], [])
        options = self.options("--furnished")
        self.assertEqual(scanner.filter_listings([dict(ROOM, is_furnished=False)], options, *CENTER[:2])[0], [])

    def test_exact_place_name_preferred_over_similarly_named_stop(self):
        client = StubClient([{"results": [
            {"attrs": {"lat": 47.37, "lon": 8.53, "label": "<b>Somewhere Süd</b>"}},
            {"attrs": {"lat": 47.38, "lon": 8.54, "label": "<i>Zug</i> <b>Somewhere</b>"}},
        ]}])
        center = scanner.resolve_center("Somewhere", client)
        self.assertEqual(center[:2], (47.38, 8.54))

    def test_exact_coordinates_and_default_need_no_network(self):
        client = StubClient([])
        self.assertEqual(scanner.resolve_center("47.378178,8.540218", client)[:2], CENTER[:2])
        self.assertEqual(scanner.resolve_center("Zürich HB", client), CENTER)
        with self.assertRaises(scanner.ScannerError):
            scanner.resolve_center("99,8", client)

    def test_capped_map_tiles_are_subdivided(self):
        client = StubClient([[{"pk": 1}, {"pk": 2}], [{"pk": 1}], [{"pk": 2}], [], []])
        with patch.object(scanner, "PIN_LIMIT", 2):
            pins = scanner.fetch_pins(client, scanner.bounding_box(*CENTER[:2], 5))
        self.assertEqual({p["pk"] for p in pins}, {1, 2})
        self.assertEqual(len(client.urls), 5)
        self.assertNotIn("max_price", client.urls[0])

    def test_detail_batches_use_repeated_ids_and_follow_pagination(self):
        client = StubClient([
            {"results": [ROOM], "next": "/api/v1/public-listing/?pk=123&pk=456&offset=1"},
            {"results": [dict(ROOM, pk=456)], "next": None},
        ])
        results = scanner.fetch_details(client, [123, 456])
        self.assertEqual(len(results), 2)
        self.assertEqual(parse_qs(urlparse(client.urls[0]).query)["pk"], ["123", "456"])

    def test_source_ignoring_id_filters_is_a_failure(self):
        client = StubClient([{"results": [dict(ROOM, pk=999)], "next": None}])
        with self.assertRaises(scanner.ScannerError):
            scanner.fetch_details(client, [123])

    def test_pagination_cannot_redirect_to_other_host(self):
        client = StubClient([{"results": [ROOM], "next": "https://example.com/private"}])
        with self.assertRaises(scanner.ScannerError):
            scanner.fetch_details(client, [123])
        self.assertEqual(len(client.urls), 1)

    def test_persistent_newness_and_empty_results(self):
        with tempfile.TemporaryDirectory() as directory:
            options = self.options("--output-dir", directory)
            metadata = {"scanned_at": "2026-11-09T09:00:00+00:00", "complete": True}
            matches, _ = scanner.filter_listings([ROOM], options, *CENTER[:2])
            self.assertEqual(scanner.save_results(options, CENTER, copy.deepcopy(matches), metadata)["new_count"], 1)
            scanner.save_results(options, CENTER, [], metadata)
            report = scanner.save_results(options, CENTER, copy.deepcopy(matches), metadata)
            self.assertEqual(report["new_count"], 0)
            self.assertEqual(json.loads((Path(directory) / "results.json").read_text(encoding="utf-8"))["matches"][0]["title"], ROOM["description_title"])
            options.max_price = 1100
            self.assertEqual(scanner.save_results(options, CENTER, copy.deepcopy(matches), metadata)["new_count"], 1)

    def test_config_override_and_invalid_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"max_price": 900, "radius_km": 8}), encoding="utf-8")
            options = self.options("--config", str(path), "--max-price", "1000")
            self.assertEqual((options.max_price, options.radius_km), (1000, 8))
        for args in (("--max-price", "nan"), ("--radius-km", "0"), ("--interval", "0"),
                     ("--min-distance-km", "6"), ("--max-price", "-1")):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.options(*args)

    def test_access_denied_is_not_retried(self):
        error = HTTPError("https://flatfox.ch/", 403, "Forbidden", {}, None)
        with patch.object(scanner, "urlopen", side_effect=error) as request:
            with self.assertRaises(scanner.ScannerError):
                scanner.HttpClient().get_json("https://flatfox.ch/")
            self.assertEqual(request.call_count, 1)

    def test_failed_scan_preserves_previous_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "results.json"
            result.write_text("previous results", encoding="utf-8")
            with patch.object(scanner, "fetch_pins", side_effect=scanner.ScannerError("offline")):
                self.assertEqual(scanner.main(["scan", "--sources", "flatfox", "--output-dir", directory]), 1)
            self.assertEqual(result.read_text(), "previous results")

    def test_watch_runs_again_after_failure_and_handles_ctrl_c(self):
        with patch.object(scanner, "scan", side_effect=[scanner.ScannerError("offline"), {}]) as scan:
            with patch.object(scanner.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(scanner.main(["watch", "--interval", "15"]), 0)
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(sleep.call_args_list[0].args, (900,))


if __name__ == "__main__":
    unittest.main()
