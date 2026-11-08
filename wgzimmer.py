"""Public wgzimmer search/detail HTML adapter (Python standard library only)."""

from datetime import datetime
from html.parser import HTMLParser
import math
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse


BASE = "https://www.wgzimmer.ch"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


class WGZimmerError(Exception):
    pass


class Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        if self.tag in ("script", "style"):
            return ""
        return " ".join(" ".join(child.text() if isinstance(child, Node) else child
                                 for child in self.children).split())

    def nodes(self, tag=None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.nodes(tag)


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def safe_url(value):
    url = urljoin(BASE, value)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in ("wgzimmer.ch", "www.wgzimmer.ch"):
        raise WGZimmerError("Unexpected wgzimmer link or pagination host.")
    if not parsed.path.startswith(("/en/wgzimmer/", "/wgzimmer/", "/wglink/")):
        raise WGZimmerError("Unexpected wgzimmer link path.")
    return urlunparse(parsed._replace(netloc="www.wgzimmer.ch", fragment=""))


def parse_search(content):
    root = Document(content).root
    text = root.text()
    total = re.search(r"\bTotal\s*:?\s*(\d+)\b", text, re.I)
    if total is None:
        raise WGZimmerError("wgzimmer did not return search results (search form, CAPTCHA, or changed HTML).")
    entries = []
    for row in root.nodes("li"):
        if "search-mate-entry" not in row.attrs.get("class", "").split():
            continue
        for anchor in row.nodes("a"):
            href = anchor.attrs.get("href", "")
            identifier = UUID.search(urlparse(href).path)
            if identifier and ("/wglink/" in href or "/wgzimmer/" in href):
                entries.append({"pk": identifier.group().lower(), "url": safe_url(href)})
                break
    count = int(total.group(1))
    if (count > 0 and not entries) or (count == 0 and entries):
        raise WGZimmerError("wgzimmer result count and room entries disagree; its HTML may have changed.")
    next_url = None
    for anchor in root.nodes("a"):
        if "next" in anchor.attrs.get("class", "").split() or anchor.text().strip().casefold() in ("next", "next >", "weiter"):
            if anchor.attrs.get("href"):
                next_url = safe_url(anchor.attrs["href"])
                break
    pages = re.search(r"\bPage\s*(\d+)\s*/\s*(\d+)", text, re.I)
    if pages and int(pages.group(1)) < int(pages.group(2)) and not next_url:
        raise WGZimmerError("wgzimmer search is missing its next-page link.")
    return entries, next_url, count


def label_key(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", "", "".join(c for c in value if not unicodedata.combining(c))).split())


def labeled_values(root):
    fields = {}
    for label in root.nodes("strong"):
        if label.parent is None:
            continue
        siblings = label.parent.children
        values = []
        for child in siblings[siblings.index(label) + 1:]:
            if isinstance(child, Node) and child.tag == "strong":
                break
            values.append(child.text() if isinstance(child, Node) else child)
        value = " ".join(" ".join(values).split())
        if value:
            fields[label_key(label.text())] = value
    return fields


def money(value):
    # Swiss thousands separators and the common CHF 950.- / CHF 1'250.50 forms.
    match = re.fullmatch(r"\s*(?:CHF\s*)?([\d][\d\s'’]*)(?:[.,](\d{1,2}|[-–—]))?\s*(?:CHF)?\s*", value, re.I)
    if not match:
        return None
    whole = re.sub(r"[\s'’]", "", match.group(1))
    decimal = match.group(2)
    amount = float(whole + ("." + decimal if decimal and decimal.isdigit() else ""))
    return amount if amount > 0 else None


def iso_date(value):
    match = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", value)
    if match:
        try:
            return datetime(int(match[3]), int(match[2]), int(match[1])).date().isoformat()
        except ValueError:
            pass
    return None


def coordinates(root):
    # Prefer structured advertised coordinates. Otherwise geocode the address below.
    meta = {}
    for node in root.nodes("meta"):
        key = node.attrs.get("itemprop") or node.attrs.get("property")
        meta[key] = node.attrs.get("content")
    pairs = [(meta.get("latitude") or meta.get("place:location:latitude"),
              meta.get("longitude") or meta.get("place:location:longitude"))]
    for anchor in root.nodes("a"):
        parsed = urlparse(anchor.attrs.get("href", ""))
        if parsed.hostname and (parsed.hostname == "maps.google.com" or parsed.hostname.startswith("www.google.")):
            for key in ("q", "query", "ll"):
                values = parse_qs(parsed.query).get(key, [])
                for value in values:
                    match = re.fullmatch(r"\s*([+-]?[\d.]+)\s*,\s*([+-]?[\d.]+)\s*", value)
                    if match:
                        pairs.append(match.groups())
    for lat, lon in pairs:
        try:
            lat, lon = float(lat), float(lon)
            if math.isfinite(lat) and math.isfinite(lon) and 45 <= lat <= 48.5 and 5 <= lon <= 11:
                return lat, lon
        except (TypeError, ValueError):
            pass
    return None


def parse_detail(content, entry):
    root = Document(content).root
    fields = labeled_values(root)

    def field(*names):
        return next((fields[label_key(name)] for name in names if label_key(name) in fields), "")

    rent_text = field("Monthly rent", "Miete pro Monat", "Mietzins", "Monatliche Miete")
    city = field("City", "Ort", "Stadt")
    street = field("Address", "Adresse")
    if not rent_text and not city and not street:
        text = root.text().casefold()
        if any(message in text for message in ("no longer available", "no longer exists", "nicht mehr vorhanden", "nicht mehr verfügbar")):
            return None
        raise WGZimmerError("wgzimmer listing fields are missing (removed listing, challenge, or changed HTML).")
    title = None
    for node in root.nodes("meta"):
        if node.attrs.get("property") == "og:title":
            title = node.attrs.get("content")
            break
    start = field("Starting from", "Ab dem", "Ab")
    until = field("Until", "Bis")
    permanent = any(word in until.casefold() for word in ("no time restrictions", "unbefristet", "unbeschränkt"))
    furnished_text = field("Furnished", "Möbliert")
    furnished = {"yes": True, "ja": True, "no": False, "nein": False}.get(furnished_text.casefold())
    point = coordinates(root)
    return {
        "pk": entry["pk"], "source": "wgzimmer", "url": entry["url"],
        "object_category": "SHARED", "offer_type": "RENT", "status": "act", "country": "CH",
        "price_unit": "monthly", "advertised_monthly_rent": money(rent_text),
        "description_title": title or "WG room in {}".format(city or "Zürich"),
        "public_address": ", ".join(part for part in (street, city) if part),
        "street": street, "city": city,
        "latitude": point[0] if point else None, "longitude": point[1] if point else None,
        "location_accuracy": "advertised" if point else None,
        "moving_date": iso_date(start), "moving_date_type": start or None,
        "is_temporary": False if permanent else (True if iso_date(until) else None),
        "is_furnished": furnished,
    }


def address_tokens(value):
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    # GeoAdmin can spell umlauts either as ü or ue.
    for first, second in (("ue", "u"), ("ae", "a"), ("oe", "o")):
        text = text.replace(first, second)
    return set(re.findall(r"[a-z0-9]+", text))


def geocode_address(client, listing, cache):
    address = listing["public_address"]
    # A city centroid would make a small-radius search misleading.
    if not listing["street"] or not listing["city"]:
        return None
    if address in cache:
        point = cache[address]
        if (isinstance(point, list) and len(point) == 2 and
                all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in point)
                and 45 <= point[0] <= 48.5 and 5 <= point[1] <= 11):
            return tuple(point)
    response = client.get_json("https://api3.geo.admin.ch/rest/services/ech/SearchServer?" + urlencode({
        "searchText": address, "type": "locations", "origins": "address", "sr": 4326, "limit": 5,
    }))
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise WGZimmerError("Unexpected GeoAdmin address response.")
    for candidate in response["results"]:
        attrs = candidate.get("attrs", {})
        label = Document(attrs.get("label", "")).root.text()
        if not address_tokens(address).issubset(address_tokens(label)):
            continue
        try:
            lat, lon = float(attrs["lat"]), float(attrs["lon"])
            if math.isfinite(lat) and math.isfinite(lon) and 45 <= lat <= 48.5 and 5 <= lon <= 11:
                cache[address] = [lat, lon]
                return lat, lon
        except (KeyError, ValueError, TypeError):
            continue
    return None


def fetch_wgzimmer(client, options, cache):
    entries = {}
    for region in options.wgzimmer_regions:
        url = BASE + "/en/wgzimmer/search/mate.html?" + urlencode({
            "startSearch": "true", "priceMin": 0, "priceMax": math.ceil(options.max_price),
            "wgState": region, "permanent": "all", "student": "none", "typeofwg": "all",
            "wc_language": "en",
        })
        visited, region_ids, total = set(), set(), 0
        while url:
            if url in visited or len(visited) >= 100:
                raise WGZimmerError("wgzimmer pagination repeated or exceeded 100 pages.")
            visited.add(url)
            page, next_url, count = parse_search(client.get_text(url))
            total = max(total, count)
            for entry in page:
                entries[entry["pk"]] = entry
                region_ids.add(entry["pk"])
            url = next_url
        if len(region_ids) < total:
            raise WGZimmerError("wgzimmer returned only {} of {} rooms in {}. Search may be incomplete."
                                .format(len(region_ids), total, region))
    listings, removed, unknown_location = [], 0, 0
    for entry in entries.values():
        listing = parse_detail(client.get_text(entry["url"]), entry)
        if listing is None:
            removed += 1
            continue
        # Avoid geocoding rooms already excluded by their rent or furnished status.
        if listing["advertised_monthly_rent"] is not None and listing["advertised_monthly_rent"] <= options.max_price and (
                not options.furnished or listing["is_furnished"] is True):
            if listing["latitude"] is None:
                point = geocode_address(client, listing, cache)
                if point:
                    listing["latitude"], listing["longitude"] = point
                    listing["location_accuracy"] = "geocoded_address"
                else:
                    unknown_location += 1
        listings.append(listing)
    return listings, {"candidate_count": len(entries), "details_count": len(listings),
                      "unavailable_details_count": removed, "unknown_location_count": unknown_location}
