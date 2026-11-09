import copy
import io
from pathlib import Path
import re
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import main as scanner
import wgzimmer as wg
from test_scanner import CENTER, ROOM


ID = "11111111-2222-3333-4444-555555555555"
ID2 = "66666666-2222-3333-4444-555555555555"
ENTRY = {"pk": ID, "url": wg.BASE + "/wglink/en/" + ID}

# Legacy fixtures kept to verify compatibility with older listing pages.
SEARCH = '''<html><body><span>Total 1</span><span>Page 1/1</span><ul>
<li class="search-result-entry search-mate-entry"><a href="/wglink/en/{id}">Room</a>
<div class="cost">CHF 999.-</div></li></ul></body></html>'''.format(id=ID)
DETAIL = '''<html><head><meta property="og:title" content="WG-Zimmer Zürich"></head><body>
<div class="date-cost"><p><strong>Monthly rent:</strong> <span>CHF 950.–</span></p>
<p><strong>Starting from:</strong> 1.12.2026</p><p><strong>Until:</strong> No time restrictions</p></div>
<div class="adress-region"><p><strong>Address:</strong> Langstrasse 120</p>
<p><strong>City:</strong> 8004 Zürich</p></div><p><strong>Furnished:</strong> Yes</p>
<a href="https://maps.google.com/?q=47.378178,8.540218">Google Maps</a></body></html>'''
FIXTURES = Path(__file__).parent / "fixtures"
CURRENT_SEARCH = (FIXTURES / "wgzimmer-search.html").read_text(encoding="utf-8")
CURRENT_DETAIL = (FIXTURES / "wgzimmer-detail.html").read_text(encoding="utf-8")


class Client:
    def __init__(self, pages=(), geo=None):
        self.pages = iter(pages)
        self.urls = []
        self.geo = geo
        self.geo_calls = 0

    def get_text(self, url):
        self.urls.append(url)
        return next(self.pages)

    def get_json(self, url):
        self.geo_calls += 1
        return self.geo


class WGZimmerTests(unittest.TestCase):
    def setUp(self):
        self.ntfy = patch.object(scanner, "settings_from_env", return_value=None)
        self.ntfy.start()
        self.addCleanup(self.ntfy.stop)

    def options(self, argv):
        return scanner.parse_args(["--max-price", "1200", "--radius-km", "5"] + argv)

    def test_search_and_detail_parse_expected_labels(self):
        entries, next_url, count = wg.parse_search(SEARCH)
        self.assertEqual((len(entries), next_url, count), (1, None, 1))
        listing = wg.parse_detail(DETAIL, entries[0])
        self.assertEqual(listing["advertised_monthly_rent"], 950)
        self.assertEqual(listing["public_address"], "Langstrasse 120, 8004 Zürich")
        self.assertEqual(listing["moving_date"], "2026-12-01")
        self.assertIs(listing["is_furnished"], True)
        self.assertIs(listing["is_temporary"], False)
        matches, _ = scanner.filter_listings([listing], self.options([]), *CENTER[:2])
        self.assertEqual(matches[0]["id"], "wgzimmer:" + ID)
        self.assertEqual(matches[0]["price_basis"], "advertised_monthly")

    def test_swiss_prices_and_unknown_prices(self):
        for text, expected in (("CHF 1'250.50", 1250.5), ("950.-", 950), ("CHF 1’050.–", 1050),
                               ("CHF 900", 900), ("900 CHF", 900), ("on request", None),
                               ("950 + utilities", None), ("50 per night", None)):
            with self.subTest(text=text):
                self.assertEqual(wg.money(text), expected)

    def test_captcha_and_changed_html_are_errors_not_zero_rooms(self):
        for content in ("<form>CAPTCHA required</form>", "<span>Total 5</span><p>New layout</p>"):
            with self.assertRaises(wg.WGZimmerError):
                wg.parse_search(content)
        self.assertEqual(wg.parse_search("<span>Total 0</span>")[0], [])

    def test_search_only_accepts_offered_rooms(self):
        wanted = SEARCH.replace("search-mate-entry", "search-room-entry")
        with self.assertRaises(wg.WGZimmerError):
            wg.parse_search(wanted)

    def test_search_paginates_and_deduplicates_by_uuid_across_regions(self):
        first = SEARCH.replace("Total 1", "Total 2").replace("Page 1/1", "Page 1/2")
        first += '<a class="next" href="/en/wgzimmer/search/mate.html?page=2">Next</a>'
        second = SEARCH.replace(ID, ID2).replace("Total 1", "Total 2").replace("Page 1/1", "Page 2/2")
        client = Client([first, second, SEARCH, DETAIL, DETAIL])
        options = self.options([])
        listings, stats = wg.fetch_wgzimmer(client, options, {})
        self.assertEqual(len(listings), 2)
        self.assertEqual(stats["candidate_count"], 2)
        self.assertEqual(len(client.urls), 5)
        self.assertEqual(parse_qs(urlparse(client.urls[0]).query)["region"], ["zuerich"])
        self.assertEqual(parse_qs(urlparse(client.urls[2]).query)["region"], ["kanton-zuerich"])

    def test_current_queries_use_supported_filters_and_region_aliases(self):
        for region, expected in (("zurich-stadt", "zuerich"), ("zurich", "kanton-zuerich"),
                                 ("zuerich", "zuerich"), ("winterthur", "winterthur")):
            client = Client(["<span>Total 0</span>"])
            options = self.options(["--wgzimmer-regions", region, "--max-price", "800.5", "--furnished"])
            wg.fetch_wgzimmer(client, options, {})
            self.assertEqual(parse_qs(urlparse(client.urls[0]).query),
                             {"region": [expected], "min": ["0"], "max": ["801"], "moebliert": ["ja"]})

    def test_current_search_reads_full_id_short_link_and_next_page(self):
        entries, next_url, count = wg.parse_search(CURRENT_SEARCH)
        self.assertEqual(count, 110)
        self.assertEqual(entries, [{"pk": "2feafb66-c53d-4bcc-bdd9-8f7871ea0452",
                                   "url": wg.BASE + "/en/wg-zimmer/zuerich/8050-zuerich-2feafb66.html"}])
        self.assertEqual(parse_qs(urlparse(next_url).query),
                         {"region": ["zuerich"], "max": ["800"], "seite": ["2"]})
        without_rel_next = CURRENT_SEARCH.replace('rel="next"', '').replace('Next ›', 'More')
        self.assertEqual(wg.parse_search(without_rel_next)[1], next_url)
        self.assertEqual(wg.parse_search('<p class="search__count">0 ads</p>'), ([], None, 0))

    def test_current_search_rejects_wanted_ads_missing_ids_and_external_links(self):
        for page in (CURRENT_SEARCH.replace('/en/wg-zimmer/', '/en/wg-suche/'),
                     CURRENT_SEARCH.replace('data-ad-id="2feafb66-c53d-4bcc-bdd9-8f7871ea0452"', ''),
                     CURRENT_SEARCH.replace('href="/en/wg-zimmer/', 'href="https://example.com/en/wg-zimmer/')):
            with self.assertRaises(wg.WGZimmerError):
                wg.parse_search(page)

    def test_current_detail_reads_facts_rent_and_approximate_map(self):
        entry = wg.parse_search(CURRENT_SEARCH)[0][0]
        listing = wg.parse_detail(CURRENT_DETAIL, entry)
        self.assertEqual(listing["advertised_monthly_rent"], 659)
        self.assertEqual(listing["public_address"], "Hagenholzstrasse, 8050 Zürich")
        self.assertEqual(listing["moving_date"], "2027-01-01")
        self.assertIs(listing["is_temporary"], False)
        self.assertIs(listing["is_furnished"], False)
        self.assertEqual((listing["latitude"], listing["longitude"]), (47.4147, 8.5605))
        self.assertEqual(listing["location_accuracy"], "advertised_approximate")
        studio = wg.parse_detail(CURRENT_DETAIL.replace('<dd>Flat share</dd>', '<dd>Studio</dd>'), entry)
        self.assertEqual(scanner.filter_listings([studio], self.options([]), 47.4147, 8.5605)[0], [])

    def test_ignored_query_filters_fail_instead_of_searching_all_switzerland(self):
        wg.validate_search_filters(CURRENT_SEARCH, "zuerich", 800, False)
        checked = CURRENT_SEARCH.replace('name="moebliert" value="ja"', 'name="moebliert" value="ja" checked')
        wg.validate_search_filters(checked, "zuerich", 800, True)
        no_form = re.sub(r"<form.*?</form>", "", CURRENT_SEARCH, flags=re.S)
        with self.assertRaises(wg.WGZimmerError):
            wg.validate_search_filters(no_form, "zuerich", 800, False)
        for region, price, furnished in (("kanton-zuerich", 800, False), ("zuerich", 900, False),
                                          ("zuerich", 800, True)):
            with self.assertRaises(wg.WGZimmerError):
                wg.validate_search_filters(CURRENT_SEARCH, region, price, furnished)

    def test_current_pages_keep_filters_and_fetch_each_detail_once(self):
        first = CURRENT_SEARCH.replace('110', '2')
        second = first.replace('2feafb66-c53d-4bcc-bdd9-8f7871ea0452', ID2).replace('2feafb66.html', '66666666.html')
        second = second.replace('rel="next"', '').replace('Next ›', 'Previous')
        second = second.replace('data-next-page="/en/wgzimmer/search/mate.html?region=zuerich&amp;max=800&amp;seite=2"', '')
        client = Client([first, second, CURRENT_DETAIL, CURRENT_DETAIL])
        options = self.options(["--wgzimmer-regions", "zurich-stadt", "--max-price", "800"])
        listings, stats = wg.fetch_wgzimmer(client, options, {})
        self.assertEqual(len(listings), 2)
        self.assertEqual(stats["unknown_location_count"], 0)
        self.assertEqual(client.geo_calls, 0)
        self.assertEqual(parse_qs(urlparse(client.urls[1]).query)["seite"], ["2"])
        stripped_filters = second.replace('value="zuerich" selected', 'value="zuerich"')
        with self.assertRaises(wg.WGZimmerError):
            wg.fetch_wgzimmer(Client([first, stripped_filters]), options, {})

    def test_missing_next_page_and_repeated_pagination_fail(self):
        with self.assertRaises(wg.WGZimmerError):
            wg.parse_search(SEARCH.replace("Page 1/1", "Page 1/2"))
        repeated = SEARCH + '<a class="next" href="/en/wgzimmer/search/mate.html?page=2">Next</a>'
        options = self.options(["--wgzimmer-regions", "zurich-stadt"])
        with self.assertRaises(wg.WGZimmerError):
            wg.fetch_wgzimmer(Client([repeated, repeated]), options, {})

    def test_links_to_other_hosts_and_paths_are_rejected(self):
        for value in ("https://example.com/wglink/en/" + ID, "http://www.wgzimmer.ch/wglink/en/" + ID,
                      "/en/accounts/login"):
            with self.assertRaises(wg.WGZimmerError):
                wg.safe_url(value)

    def test_removed_or_changed_detail_does_not_make_up_a_room(self):
        self.assertIsNone(wg.parse_detail("<p>This ad no longer exists</p>", ENTRY))
        with self.assertRaises(wg.WGZimmerError):
            wg.parse_detail("<p>Unexpected page</p>", ENTRY)

    def test_address_geocoding_is_cached_and_rejects_fuzzy_matches(self):
        listing = wg.parse_detail(DETAIL, ENTRY)
        response = {"results": [{"attrs": {"label": "Langstrasse 120 <b>8004 Zürich</b>",
                                         "lat": CENTER[0], "lon": CENTER[1]}}]}
        client, cache = Client(geo=response), {}
        self.assertEqual(wg.geocode_address(client, listing, cache), CENTER[:2])
        self.assertEqual(wg.geocode_address(client, listing, cache), CENTER[:2])
        self.assertEqual(client.geo_calls, 1)
        response["results"][0]["attrs"]["label"] = "Langstrasse 121 8004 Zürich"
        self.assertIsNone(wg.geocode_address(Client(geo=response), listing, {}))
        self.assertIsNone(wg.geocode_address(client, dict(listing, street=""), {}))

    def test_no_city_centroid_fallback_for_radius_filter(self):
        detail = DETAIL.replace('q=47.378178,8.540218', 'q=Langstrasse')
        client = Client([SEARCH, detail], geo={"results": []})
        options = self.options(["--wgzimmer-regions", "zurich-stadt"])
        listings, stats = wg.fetch_wgzimmer(client, options, {})
        self.assertEqual(stats["unknown_location_count"], 1)
        self.assertEqual(scanner.filter_listings(listings, options, *CENTER[:2])[0], [])

    def test_tls_errors_fail_immediately_without_disabling_verification(self):
        error = URLError(ssl.SSLCertVerificationError(1, "certificate has expired"))
        with patch.object(scanner, "urlopen", side_effect=error) as request:
            with self.assertRaises(scanner.ScannerError):
                scanner.HttpClient().get_text(wg.BASE)
            self.assertEqual(request.call_count, 1)

    def test_source_selection_config_and_invalid_sources(self):
        self.assertEqual(self.options(["--sources", "wgzimmer"]).sources, ["wgzimmer"])
        self.assertEqual(self.options(["--sources", "wgzimmer,flatfox,wgzimmer"]).sources,
                         ["wgzimmer", "flatfox"])
        for value in ("unknown", "", "wgzimmer,,flatfox"):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.options(["--sources", value])

    def test_one_failed_source_exports_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            options = self.options(["--output-dir", directory])
            with patch.object(scanner, "fetch_pins", return_value=[{"pk": 123}]):
                with patch.object(scanner, "fetch_details", return_value=[ROOM]):
                    with patch.object(scanner, "fetch_wgzimmer", side_effect=wg.WGZimmerError("challenge")):
                        with patch("sys.stdout", new_callable=io.StringIO):
                            report = scanner.scan(options, CENTER)
            self.assertFalse(report["complete"])
            self.assertEqual(len(report["matches"]), 1)
            self.assertFalse(report["sources"]["wgzimmer"]["ok"])
            self.assertEqual(report["sources"]["wgzimmer"]["error"], "challenge")
            self.assertTrue((Path(directory) / "results.json").exists())

    def test_wgzimmer_works_when_flatfox_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            options = self.options(["--output-dir", directory])
            listings = [wg.parse_detail(DETAIL, ENTRY)]
            stats = {"candidate_count": 1, "details_count": 1, "unavailable_details_count": 0,
                     "unknown_location_count": 0}
            with patch.object(scanner, "fetch_pins", side_effect=scanner.ScannerError("offline")):
                with patch.object(scanner, "fetch_wgzimmer", return_value=(listings, stats)):
                    with patch("sys.stdout", new_callable=io.StringIO):
                        report = scanner.scan(options, CENTER)
            self.assertEqual(report["matches"][0]["source"], "wgzimmer.ch")
            self.assertFalse(report["complete"])

    def test_old_flatfox_seen_history_is_preserved_when_enabling_wgzimmer(self):
        with tempfile.TemporaryDirectory() as directory:
            flatfox = self.options(["--sources", "flatfox", "--output-dir", directory])
            both = self.options(["--output-dir", directory])
            matches, _ = scanner.filter_listings([ROOM], flatfox, *CENTER[:2])
            metadata = {"scanned_at": "2026-11-09T09:00:00+00:00", "complete": True}
            self.assertEqual(scanner.save_results(flatfox, CENTER, copy.deepcopy(matches), metadata)["new_count"], 1)
            wg_matches, _ = scanner.filter_listings([wg.parse_detail(DETAIL, ENTRY)], both, *CENTER[:2])
            report = scanner.save_results(both, CENTER, copy.deepcopy(matches) + wg_matches, metadata)
            self.assertEqual(report["new_count"], 1)
            self.assertEqual([item["source"] for item in report["matches"] if item["is_new"]], ["wgzimmer.ch"])

    def test_partial_one_shot_scan_returns_distinct_exit_code(self):
        with patch.object(scanner, "scan", return_value={"complete": False}):
            self.assertEqual(scanner.main(["scan", "--center", "Zürich HB"]), 2)


if __name__ == "__main__":
    unittest.main()
