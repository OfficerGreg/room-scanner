"""Search Flatfox and wgzimmer.ch for WG rooms. Run `python main.py --help`."""

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import ssl
import sqlite3
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from wgzimmer import WGZimmerError, fetch_wgzimmer


LOG = logging.getLogger("room_scanner")
FLATFOX = "https://flatfox.ch"
PIN_LIMIT = 1000
EARTH_KM = 6371.0088
DEFAULTS = {
    "max_price": 1200.0,
    "center": "Zürich HB",
    "radius_km": 5.0,
    "min_distance_km": 0.0,
    "interval_minutes": 30.0,
    "furnished": False,
    "sources": ["flatfox", "wgzimmer"],
    "wgzimmer_regions": ["zurich-stadt", "zurich"],
}


class ScannerError(Exception):
    """A user-facing configuration, network, or source error."""


class HttpClient:
    """Pace requests and retry transient failures without bypassing access checks."""

    def __init__(self, delay=1.0, timeout=30.0, max_requests=200, cookies=False):
        self.delay = delay
        self.timeout = timeout
        self.max_requests = max_requests
        self.requests = 0
        self.last_request = None
        self.open = build_opener(HTTPCookieProcessor(CookieJar())).open if cookies else urlopen

    def get_json(self, url):
        try:
            return json.loads(self._read(url, "application/json"))
        except (ValueError, UnicodeError) as exc:
            raise ScannerError("Invalid JSON from {}; the source may have changed."
                               .format(urlparse(url).hostname)) from exc

    def get_text(self, url):
        return self._read(url, "text/html,application/xhtml+xml")

    def _read(self, url, accept):
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise ScannerError("Request limit reached; narrow the radius and try again.")
            if self.last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.requests += 1
            request = Request(url, headers={
                "User-Agent": "ZurichRoomScanner/1.0 (personal housing search)",
                "Accept": accept,
            })
            try:
                with self.open(request, timeout=self.timeout) as response:
                    encoding = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(encoding)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ScannerError("HTTP {} from {}. Try again later; access restrictions are not bypassed."
                                       .format(exc.code, urlparse(url).hostname)) from exc
                retry_after = exc.headers.get("Retry-After", "")
                if retry_after.isdigit() and int(retry_after) > 60:
                    raise ScannerError("Source requests a longer pause. Try again in {} seconds."
                                       .format(retry_after)) from exc
                wait = max(2 ** (attempt + 1), int(retry_after) if retry_after.isdigit() else 0)
                LOG.warning("HTTP %s; retrying in %s seconds", exc.code, wait)
                time.sleep(wait)
            except (URLError, TimeoutError, OSError) as exc:
                if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                    raise ScannerError("TLS certificate validation failed for {}: {}. Certificate verification remains enabled."
                                       .format(urlparse(url).hostname, exc)) from exc
                if attempt == 2:
                    raise ScannerError("Cannot reach {}: {}".format(urlparse(url).hostname, exc)) from exc
                time.sleep(2 ** (attempt + 1))
            except (ValueError, UnicodeError) as exc:
                raise ScannerError("Invalid text from {}; the source may have changed."
                                   .format(urlparse(url).hostname)) from exc


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def normalized(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                            if not unicodedata.combining(c)).split())


def plain_text(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def resolve_center(value, client):
    """Accept lat,lon or a Swiss address/place, returning an explicit resolved label."""
    coordinates = re.fullmatch(r"\s*([+-]?[\d.]+)\s*,\s*([+-]?[\d.]+)\s*", value)
    if coordinates:
        lat, lon = (number(part) for part in coordinates.groups())
        if lat is None or lon is None or not -85 <= lat <= 85 or not -180 <= lon <= 180:
            raise ScannerError("Center coordinates must be latitude (-85..85), longitude (-180..180).")
        return lat, lon, "{:.6f}, {:.6f}".format(lat, lon)
    if normalized(value) in ("zurich hb", "zurich hauptbahnhof"):
        return 47.378178, 8.540218, "Zürich HB"
    url = "https://api3.geo.admin.ch/rest/services/ech/SearchServer?" + urlencode({
        "searchText": value, "type": "locations", "sr": 4326, "limit": 20,
    })
    response = client.get_json(url)
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise ScannerError("Unexpected geocoder response.")
    candidates = []
    for result in response["results"]:
        attrs = result.get("attrs", {})
        lat, lon = number(attrs.get("lat")), number(attrs.get("lon"))
        if lat is None or lon is None:
            continue
        label = attrs.get("label", value)
        bold = " ".join(re.findall(r"<b>(.*?)</b>", label))
        exact = normalized(plain_text(bold)) == normalized(value)
        candidates.append((not exact, lat, lon, plain_text(label)))
    if not candidates:
        raise ScannerError("No location found for {!r}. Use a full Swiss address or lat,lon.".format(value))
    # Prefer an exact place name over similarly named stops, retaining source rank otherwise.
    candidates.sort(key=lambda candidate: candidate[0])
    _, lat, lon, label = candidates[0]
    return lat, lon, label


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    hav = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1.0, max(0.0, hav))))


def bounding_box(lat, lon, radius):
    angle = radius / EARTH_KM
    lat_delta = math.degrees(angle)
    lon_delta = math.degrees(math.asin(min(1, math.sin(angle) / math.cos(math.radians(lat)))))
    if lon - lon_delta < -180 or lon + lon_delta > 180:
        raise ScannerError("Search areas crossing the date line are unsupported.")
    return {"north": lat + lat_delta, "south": lat - lat_delta,
            "east": lon + lon_delta, "west": lon - lon_delta}


def fetch_pins(client, bounds, depth=0):
    # Do not prefilter on map prices: only listing details determine the rent.
    url = FLATFOX + "/api/v1/pin/?" + urlencode(dict(
        bounds, offer_type="RENT", object_category="SHARED", max_count=PIN_LIMIT,
        ordering="-created"))
    pins = client.get_json(url)
    if not isinstance(pins, list) or any(not isinstance(pin, dict) or "pk" not in pin for pin in pins):
        raise ScannerError("Unexpected Flatfox map response; the source may have changed.")
    if len(pins) < PIN_LIMIT:
        return pins
    if depth >= 5:
        raise ScannerError("Map results are still capped after subdivision. Use a smaller radius.")
    mid_lat = (bounds["north"] + bounds["south"]) / 2
    mid_lon = (bounds["east"] + bounds["west"]) / 2
    result = []
    for south, north in ((bounds["south"], mid_lat), (mid_lat, bounds["north"])):
        for west, east in ((bounds["west"], mid_lon), (mid_lon, bounds["east"])):
            result.extend(fetch_pins(client, dict(south=south, north=north, west=west, east=east), depth + 1))
    return result


def fetch_details(client, ids):
    """The public feed supports repeated pk parameters, unlike location filters."""
    listings = {}
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        params = [("pk", str(pk)) for pk in batch] + [("limit", "50")]
        url = FLATFOX + "/api/v1/public-listing/?" + urlencode(params)
        visited = set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "flatfox.ch" or parsed.path != "/api/v1/public-listing/":
                raise ScannerError("Unexpected Flatfox pagination URL.")
            if url in visited:
                raise ScannerError("Flatfox pagination repeated a page.")
            visited.add(url)
            response = client.get_json(url)
            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                raise ScannerError("Unexpected Flatfox listing response.")
            for listing in response["results"]:
                if not isinstance(listing, dict) or listing.get("pk") not in batch:
                    raise ScannerError("Flatfox ignored the listing ID filter; stopping to avoid incorrect results.")
                listings[listing["pk"]] = listing
            next_page = response.get("next")
            url = urljoin(FLATFOX, next_page) if next_page else None
    return list(listings.values())


def monthly_rent(listing):
    """Flatfox gross rent or wgzimmer's explicitly labeled advertised monthly rent."""
    if listing.get("price_unit") != "monthly":
        return None
    if listing.get("source") == "wgzimmer":
        price = number(listing.get("advertised_monthly_rent"))
        return price if price is not None and price > 0 else None
    gross = number(listing.get("rent_gross"))
    net = number(listing.get("rent_net"))
    charges = number(listing.get("rent_charges"))
    if gross is not None and gross > 0:
        return gross
    if net is not None and net > 0 and charges is not None and charges >= 0:
        return net + charges
    return None


def filter_listings(listings, options, lat, lon):
    matches = []
    unknown_price = 0
    for listing in listings:
        if (listing.get("object_category") != "SHARED" or listing.get("offer_type") != "RENT"
                or listing.get("status") != "act" or listing.get("country") != "CH"):
            continue
        if options.furnished and listing.get("is_furnished") is not True:
            continue
        price = monthly_rent(listing)
        if price is None:
            unknown_price += 1
            continue
        if price > options.max_price:
            continue
        room_lat, room_lon = number(listing.get("latitude")), number(listing.get("longitude"))
        if room_lat is None or room_lon is None or not -90 <= room_lat <= 90 or not -180 <= room_lon <= 180:
            continue
        distance = distance_km(lat, lon, room_lat, room_lon)
        if not options.min_distance_km <= distance <= options.radius_km:
            continue
        source = listing.get("source", "flatfox")
        base, label = {"flatfox": (FLATFOX, "Flatfox"),
                       "wgzimmer": ("https://www.wgzimmer.ch", "wgzimmer.ch")}[source]
        listing_url = urljoin(base, listing.get("url") or "/{}/".format(listing["pk"]))
        if urlparse(listing_url).scheme != "https" or urlparse(listing_url).netloc != urlparse(base).netloc:
            raise ScannerError("Unexpected listing link from {}.".format(label))
        matches.append({
            "id": "{}:{}".format(source, listing["pk"]), "source": label,
            "title": listing.get("description_title") or listing.get("short_title") or "WG room",
            "price_chf": price, "distance_km": round(distance, 3),
            "price_basis": "advertised_monthly" if source == "wgzimmer" else "gross_monthly",
            "address": listing.get("public_address") or listing.get("city") or "",
            "latitude": room_lat, "longitude": room_lon,
            "location_accuracy": listing.get("location_accuracy") or "advertised",
            "available_from": listing.get("moving_date"),
            "availability_type": listing.get("moving_date_type"),
            "furnished": listing.get("is_furnished"), "temporary": listing.get("is_temporary"),
            "published": listing.get("published"), "url": listing_url,
        })
    matches.sort(key=lambda item: (item["price_chf"], item["distance_km"], item["id"]))
    return matches, unknown_price


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=str(path.parent),
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_results(options, center, matches, metadata):
    """Newness is per search criteria, so changing filters does not hide new matches."""
    output = options.output_dir
    output.mkdir(parents=True, exist_ok=True)
    criteria = {"center": list(center[:2]), "radius_km": options.radius_km,
                "min_distance_km": options.min_distance_km, "max_price": options.max_price,
                "furnished": options.furnished, "version": 1}
    # Keep the old Flatfox fingerprint so enabling wgzimmer preserves existing seen state.
    search_keys = {}
    for source in options.sources:
        source_criteria = dict(criteria, source=source)
        if source == "wgzimmer":
            source_criteria["regions"] = sorted(options.wgzimmer_regions)
        search_keys[source] = hashlib.sha256(json.dumps(source_criteria, sort_keys=True).encode()).hexdigest()
    with sqlite3.connect(str(output / "scanner.sqlite3")) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS seen (search_key TEXT, listing_id TEXT, "
                           "first_seen TEXT, last_seen TEXT, PRIMARY KEY (search_key, listing_id))")
        seen = {}
        for search_key in search_keys.values():
            seen.update(connection.execute("SELECT listing_id, first_seen FROM seen WHERE search_key = ?", (search_key,)))
        for match in matches:
            match["is_new"] = match["id"] not in seen
            match["first_seen"] = seen.get(match["id"], metadata["scanned_at"])
        report = dict(metadata, source=", ".join(options.sources), center={"latitude": center[0], "longitude": center[1],
                      "label": center[2]}, filters=dict(criteria, sources=options.sources,
                      wgzimmer_regions=options.wgzimmer_regions), new_count=sum(item["is_new"] for item in matches),
                      matches=matches)
        atomic_write(output / "results.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        fields = ["id", "title", "price_chf", "distance_km", "address", "url", "available_from",
                  "availability_type", "furnished", "temporary", "published", "latitude", "longitude",
                  "source", "price_basis", "location_accuracy", "is_new", "first_seen"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        for match in matches:
            # Listing text is untrusted; prevent formulas when opened in Excel.
            writer.writerow({key: "'" + str(value) if isinstance(value, str) and
                             value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
                             for key, value in match.items()})
        atomic_write(output / "results.csv", buffer.getvalue())
        connection.executemany("INSERT INTO seen VALUES (?, ?, ?, ?) ON CONFLICT(search_key, listing_id) "
                               "DO UPDATE SET last_seen=excluded.last_seen",
                               [(search_keys[item["id"].split(":", 1)[0]], item["id"], item["first_seen"],
                                 metadata["scanned_at"]) for item in matches])
    return report


def scan(options, center):
    matches, sources = [], {}
    for source in options.sources:
        try:
            if source == "flatfox":
                client = HttpClient()
                pins = fetch_pins(client, bounding_box(center[0], center[1], options.radius_km))
                ids = sorted({pin["pk"] for pin in pins})
                LOG.info("Flatfox: found %s candidate rooms; fetching details", len(ids))
                listings = fetch_details(client, ids)
                stats = {"candidate_count": len(ids), "details_count": len(listings),
                         "unavailable_details_count": len(ids) - len(listings), "unknown_location_count": 0}
            else:
                client = HttpClient(max_requests=600, cookies=True)
                cache_path = options.output_dir / "wgzimmer-geocodes.json"
                cache = {}
                if cache_path.exists():
                    try:
                        cached = json.loads(cache_path.read_text(encoding="utf-8"))
                        if isinstance(cached, dict):
                            cache = cached
                    except (ValueError, OSError):
                        LOG.warning("Ignoring unreadable wgzimmer geocoding cache")
                LOG.info("wgzimmer: scanning regions %s", ", ".join(options.wgzimmer_regions))
                try:
                    listings, stats = fetch_wgzimmer(client, options, cache)
                finally:
                    if cache:
                        atomic_write(cache_path, json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
            source_matches, unknown_price = filter_listings(listings, options, *center[:2])
            matches.extend(source_matches)
            sources[source] = dict(stats, ok=True, match_count=len(source_matches),
                                   unknown_monthly_rent_count=unknown_price,
                                   complete=stats["unavailable_details_count"] == 0 and stats["unknown_location_count"] == 0)
            LOG.info("%s: %s matches; %s without known monthly rent; %s without usable locations", source,
                     len(source_matches), unknown_price, stats["unknown_location_count"])
        except (ScannerError, WGZimmerError, OSError) as exc:
            sources[source] = {"ok": False, "complete": False, "error": str(exc)}
            LOG.error("%s failed: %s", source, exc)
    if not any(status["ok"] for status in sources.values()):
        raise ScannerError("All requested sources failed. Previous result files were preserved.")
    matches.sort(key=lambda item: (item["price_chf"], item["distance_km"], item["id"]))
    complete = all(status["complete"] for status in sources.values())
    report = save_results(options, center, matches, {
        "scanned_at": datetime.now(timezone.utc).isoformat(), "complete": complete, "sources": sources,
        **{key: sum(status.get(key, 0) for status in sources.values()) for key in (
            "candidate_count", "details_count", "unavailable_details_count", "unknown_monthly_rent_count", "unknown_location_count")},
    })
    print("\n{}{} rooms match | {} new | max CHF {:g} | {:.1f} km around {}".format(
        "[PARTIAL] " if not complete else "", len(matches), report["new_count"], options.max_price, options.radius_km, center[2]))
    visible = [item for item in matches if item["is_new"] or not options.new_only]
    for item in visible:
        print("{}[{}] CHF {:g}/month | {:.2f} km | {}".format(
            "[NEW] " if item["is_new"] else "", item["source"], item["price_chf"], item["distance_km"], item["title"]))
        print("  {}\n  {}".format(item["address"], item["url"]))
    print("Saved {} and {}\n".format(options.output_dir / "results.json", options.output_dir / "results.csv"), flush=True)
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Find WG rooms on Flatfox and wgzimmer.ch by monthly rent and straight-line distance.")
    parser.add_argument("command", nargs="?", choices=("scan", "watch"), default="scan")
    parser.add_argument("--config", type=Path, help="JSON configuration file; CLI values override it")
    parser.add_argument("--max-price", type=float, help="Maximum monthly rent in CHF (Flatfox gross; wgzimmer advertised; default: 1200)")
    parser.add_argument("--center", help='Swiss address/place or "latitude,longitude" (default: Zürich HB)')
    parser.add_argument("--radius-km", "--radius", "--max-distance-km", dest="radius_km", type=float,
                        help="Maximum straight-line distance in km (default: 5)")
    parser.add_argument("--min-distance-km", type=float, help="Optional inner radius in km (default: 0)")
    parser.add_argument("--interval-minutes", "--interval", dest="interval_minutes", type=float,
                        help="In watch mode: minutes between scans, at least 5 (default: 30)")
    parser.add_argument("--furnished", action="store_true", default=None, help="Only furnished rooms")
    parser.add_argument("--sources", help="Comma-separated sources: flatfox,wgzimmer (default: both)")
    parser.add_argument("--wgzimmer-regions", help="Comma-separated wgzimmer regions (default: zurich-stadt,zurich)")
    parser.add_argument("--new-only", action="store_true", help="Print only unseen matches; exports still include all matches")
    parser.add_argument("--output-dir", type=Path, help="Results and persistent state directory (default: data)")
    options = parser.parse_args(argv)
    config = dict(DEFAULTS)
    if options.config:
        try:
            loaded = json.loads(options.config.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            parser.error("Cannot read config: {}".format(exc))
        if not isinstance(loaded, dict):
            parser.error("Config must be a JSON object")
        unknown = set(loaded) - set(DEFAULTS) - {"output_dir"}
        if unknown:
            parser.error("Unknown config keys: {}".format(", ".join(sorted(unknown))))
        config.update(loaded)
    for key, default in config.items():
        if getattr(options, key, None) is None:
            setattr(options, key, default)
    for key in ("max_price", "radius_km", "min_distance_km", "interval_minutes"):
        value = number(getattr(options, key))
        if value is None:
            parser.error("{} must be a finite number".format(key))
        setattr(options, key, value)
    if options.max_price <= 0:
        parser.error("--max-price must be positive")
    if not 0 < options.radius_km <= 100:
        parser.error("--radius-km must be greater than 0 and at most 100")
    if not 0 <= options.min_distance_km < options.radius_km:
        parser.error("--min-distance-km must be >= 0 and smaller than the radius")
    if options.interval_minutes < 5:
        parser.error("--interval-minutes must be at least 5")
    if not isinstance(options.furnished, bool):
        parser.error("furnished must be a JSON boolean")
    for key in ("sources", "wgzimmer_regions"):
        value = getattr(options, key)
        if isinstance(value, str):
            value = [part.strip().lower() for part in value.split(",")]
        if not isinstance(value, list) or not value or any(not isinstance(part, str) or
                not re.fullmatch(r"[a-z][a-z0-9-]*", part) for part in value):
            parser.error("{} must be a nonempty list or comma-separated string of names".format(key))
        setattr(options, key, list(dict.fromkeys(value)))
    if set(options.sources) - {"flatfox", "wgzimmer"}:
        parser.error("--sources supports only flatfox and wgzimmer")
    if not isinstance(options.center, str) or not options.center.strip():
        parser.error("center must be a nonempty address, place, or lat,lon string")
    try:
        options.output_dir = Path(options.output_dir if options.output_dir is not None else "data").resolve()
    except (TypeError, ValueError, OSError) as exc:
        parser.error("Invalid output_dir: {}".format(exc))
    return options


def main(argv=None):
    options = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        center = resolve_center(options.center, HttpClient())
        LOG.info("Center: %s (%.6f, %.6f)", center[2], center[0], center[1])
        while True:
            try:
                report = scan(options, center)
                if options.command == "scan":
                    return 0 if report["complete"] else 2
            except (ScannerError, OSError, sqlite3.Error) as exc:
                LOG.error("Scan failed: %s", exc)
                if options.command == "scan":
                    return 1
            if options.command == "scan":
                return 0
            LOG.info("Next scan in %g minutes. Ctrl+C stops the scanner.", options.interval_minutes)
            time.sleep(options.interval_minutes * 60)
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        return 0
    except (ScannerError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
