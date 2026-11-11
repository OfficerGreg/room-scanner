"""Public wgzimmer search/detail HTML adapter (Python standard library only)."""

from datetime import datetime
from html.parser import HTMLParser
import math
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse


BASE = "https://www.wgzimmer.ch"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
# Preserve existing configs while using the current city/canton identifiers.
REGION_ALIASES = {"zurich-stadt": "zuerich", "zurich": "kanton-zuerich"}


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
    if not parsed.path.startswith(("/en/wgzimmer/", "/wgzimmer/", "/wglink/", "/en/wg-zimmer/", "/wg-zimmer/")):
        raise WGZimmerError("Unexpected wgzimmer link path.")
    return urlunparse(parsed._replace(netloc="www.wgzimmer.ch", fragment=""))


def parse_search(content):
    root = Document(content).root
    text = root.text()
    result_list = next((node for node in root.nodes("ol")
                        if "result-list" in node.attrs.get("class", "").split()), None)
    count_node = next((node for node in root.nodes()
                       if "search__count" in node.attrs.get("class", "").split()), None)
    if result_list is not None and result_list.attrs.get("data-total", "").isdigit():
        count = int(result_list.attrs["data-total"])
    elif count_node is not None and re.fullmatch(r"[\d\s'’]+ ads?", count_node.text()):
        count = int(re.sub(r"\D", "", count_node.text()))
    else:
        total = re.search(r"\bTotal\s*:?\s*(\d+)\b", text, re.I)
        count = int(total[1]) if total else None
    if count is None:
        raise WGZimmerError("wgzimmer did not return search results (search form, CAPTCHA, or changed HTML).")
    entries = []
    for row in root.nodes("li"):
        classes = row.attrs.get("class", "").split()
        if "result-row" in classes:
            identifier = UUID.fullmatch(row.attrs.get("data-ad-id", ""))
            anchor = next((a for a in row.nodes("a")
                           if "result-row__link" in a.attrs.get("class", "").split()), None)
            if identifier is None or anchor is None:
                raise WGZimmerError("wgzimmer room entry is missing its ID or link.")
            url = safe_url(anchor.attrs.get("href", ""))
            if not urlparse(url).path.startswith(("/en/wg-zimmer/", "/wg-zimmer/")):
                raise WGZimmerError("wgzimmer returned a non-room search result.")
            entries.append({"pk": identifier.group().lower(), "url": url})
            continue
        if "search-mate-entry" not in classes:
            continue
        for anchor in row.nodes("a"):
            href = anchor.attrs.get("href", "")
            identifier = UUID.search(urlparse(href).path)
            if identifier and ("/wglink/" in href or "/wgzimmer/" in href):
                entries.append({"pk": identifier.group().lower(), "url": safe_url(href)})
                break
    if (count > 0 and not entries) or (count == 0 and entries):
        raise WGZimmerError("wgzimmer result count and room entries disagree; its HTML may have changed.")
    next_url = safe_url(result_list.attrs["data-next-page"]) if (
        result_list is not None and result_list.attrs.get("data-next-page")) else None
    for anchor in root.nodes("a"):
        if ("next" in anchor.attrs.get("rel", "").split() or
                "next" in anchor.attrs.get("class", "").split() or
                anchor.text().strip().casefold() in ("next", "next >", "next ›", "weiter")):
            if anchor.attrs.get("href"):
                next_url = safe_url(anchor.attrs["href"])
                break
    pages = re.search(r"\bPage\s*(\d+)\s*/\s*(\d+)", text, re.I)
    if pages and int(pages.group(1)) < int(pages.group(2)) and not next_url:
        raise WGZimmerError("wgzimmer search is missing its next-page link.")
    return entries, next_url, count


def validate_search_filters(content, region, max_price, furnished):
    """Do not silently accept an unfiltered page when the site ignores a query."""
    root = Document(content).root
    form = next((n for n in root.nodes("form")
                 if "search-filter__form" in n.attrs.get("class", "").split()), None)
    if form is None:
        if any("search__count" in n.attrs.get("class", "").split() or
               "result-list" in n.attrs.get("class", "").split() for n in root.nodes()):
            raise WGZimmerError("wgzimmer search filter form is missing; cannot verify query filters.")
        # Legacy HTML has no current filter form; parse_search checks its structure.
        return
    selected = {}
    for node in form.nodes("select"):
        choice = next((o for o in node.nodes("option") if "selected" in o.attrs), None)
        selected[node.attrs.get("name")] = choice.attrs.get("value", "") if choice else ""
    inputs = {n.attrs.get("name"): n for n in form.nodes("input")}
    if (selected.get("region") != region or "max" not in inputs or
            inputs["max"].attrs.get("value") != str(max_price) or
            (furnished and ("moebliert" not in inputs or "checked" not in inputs["moebliert"].attrs))):
        raise WGZimmerError("wgzimmer did not apply the requested region, rent, or furnished filters. "
                            "Check --wgzimmer-regions; its search form may have changed.")


def label_key(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", "", "".join(c for c in value if not unicodedata.combining(c))).split())


def labeled_values(root):
    fields = {}
    for label in root.nodes("dt"):
        siblings = label.parent.children
        for sibling in siblings[siblings.index(label) + 1:]:
            if isinstance(sibling, Node):
                if sibling.tag == "dd":
                    fields[label_key(label.text())] = sibling.text()
                break
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
    for node in root.nodes():
        if "ad-map" in node.attrs.get("class", "").split():
            pairs.append((node.attrs.get("data-lat"), node.attrs.get("data-lon")))
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
    rent_node = next((n for n in root.nodes()
                      if "ad-page__rent-value" in n.attrs.get("class", "").split()), None)
    if rent_node is not None:
        rent_text = rent_node.text()
    city = field("City", "Ort", "Stadt")
    street = field("Address", "Adresse")
    if not city and street:
        address = re.fullmatch(r"(.*?)\s*(\d{4}\s+.+)", street)
        if address:
            street, city = address[1].strip(), address[2].strip()
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
    start = field("Starting from", "From", "Ab dem", "Ab")
    until = field("Until", "Bis")
    permanent = any(word in until.casefold() for word in ("no time restrictions", "open-ended", "unlimited", "unbefristet", "unbeschränkt"))
    furnished_text = field("Furnished", "Möbliert")
    furnished = {"yes": True, "ja": True, "no": False, "nein": False}.get(furnished_text.casefold())
    point = coordinates(root)
    approximate = any("ad-map" in n.attrs.get("class", "").split() and "data-approximate" in n.attrs
                      for n in root.nodes())
    flat_type = field("Type of flat share")
    return {
        "pk": entry["pk"], "source": "wgzimmer", "url": entry["url"],
        "object_category": "APARTMENT" if flat_type.casefold() == "studio" else "SHARED",
        "offer_type": "RENT", "status": "act", "country": "CH",
        "price_unit": "monthly", "advertised_monthly_rent": money(rent_text),
        "description_title": title or "WG room in {}".format(city or "Zürich"),
        "public_address": ", ".join(part for part in (street, city) if part),
        "street": street, "city": city,
        "latitude": point[0] if point else None, "longitude": point[1] if point else None,
        "location_accuracy": ("advertised_approximate" if approximate else "advertised") if point else None,
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
    # GeoAdmin rejects searchText longer than ten words with HTTP 400.
    # Listings sometimes give directions instead of an address. Do not truncate
    # those into a different location or let one discard the entire source.
    if len(address.split()) > 10:
        return None
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
        region = REGION_ALIASES.get(region, region)
        max_price = math.ceil(options.max_price)
        params = {"region": region, "min": 0, "max": max_price}
        if options.furnished:
            params["moebliert"] = "ja"
        url = BASE + "/en/wgzimmer/search/mate.html?" + urlencode(params)
        visited, region_ids, total = set(), set(), 0
        while url:
            if url in visited or len(visited) >= 100:
                raise WGZimmerError("wgzimmer pagination repeated or exceeded 100 pages.")
            visited.add(url)
            if urlparse(url).path != "/en/wgzimmer/search/mate.html":
                raise WGZimmerError("Unexpected wgzimmer search pagination path.")
            content = client.get_text(url)
            validate_search_filters(content, region, max_price, options.furnished)
            page, next_url, count = parse_search(content)
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
        if (listing["object_category"] == "SHARED" and listing["advertised_monthly_rent"] is not None
                and listing["advertised_monthly_rent"] <= options.max_price) and (
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
