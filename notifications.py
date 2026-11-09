"""Optional ntfy publishing with persistent delivery history (standard library only)."""

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOG = logging.getLogger("room_scanner.ntfy")


class NotificationError(Exception):
    """A configuration or delivery error safe to display without exposing secrets."""


@dataclass(frozen=True)
class NtfySettings:
    server: str
    topic: str = field(repr=False)
    token: str = field(default="", repr=False)

    @property
    def url(self):
        return self.server + "/" + self.topic


def settings_from_env():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return None
    if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic):
        raise NotificationError("NTFY_TOPIC must be a topic name of 1–64 letters, numbers, underscores or dashes.")
    server = os.environ.get("NTFY_SERVER", "").strip().rstrip("/") or "https://ntfy.sh"
    try:
        parsed = urlparse(server)
        parsed.port
    except ValueError:
        raise NotificationError("NTFY_SERVER must be a valid HTTPS server URL.") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or re.search(r"\s", server)):
        raise NotificationError("NTFY_SERVER must be an HTTPS server URL without credentials, query or fragment.")
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token and not re.fullmatch(r"[\x21-\x7e]+", token):
        raise NotificationError("NTFY_TOKEN must be a single ASCII token without whitespace.")
    return NtfySettings(server, topic, token)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a publishing token or the message to another endpoint.
        return None


def publish(settings, room):
    rental_type = {True: "Temporary rental", False: "Ongoing rental"}.get(room.get("temporary"), "Rental period unspecified")
    message = "{}\nCHF {:g}/month | {}\nAvailable: {}\n{}\n{}\n{}".format(
        room["address"], room["price_chf"], room["source"], room.get("available_from") or "See listing",
        rental_type, room["url"], room["title"])
    # Leave room for multi-byte characters within ntfy's normal message size.
    body = message.encode("utf-8")[:4000].decode("utf-8", errors="ignore").encode("utf-8")
    headers = {"Content-Type": "text/plain; charset=utf-8", "Tags": "house",
               "Title": "Bucheggstrasse room - CHF {:g}/month".format(room["price_chf"]), "Click": room["url"]}
    if settings.token:
        headers["Authorization"] = "Bearer " + settings.token
    request = Request(settings.url, data=body, headers=headers, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            acknowledgement = json.loads(response.read(65536).decode("utf-8"))
        if (not isinstance(acknowledgement, dict) or acknowledgement.get("event") != "message"
                or acknowledgement.get("topic") != settings.topic or not acknowledgement.get("id")):
            raise NotificationError("ntfy returned an unexpected publishing acknowledgement.")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise NotificationError("ntfy returned HTTP {}. Delivery will be retried on a later scan.".format(code)) from None
    except (URLError, OSError, ValueError, UnicodeError):
        raise NotificationError("Cannot confirm ntfy delivery. It will be retried on a later scan.") from None


def notify_rooms(settings, rooms, output_dir):
    if settings is None:
        return {"enabled": False, "sent_count": 0, "failed_count": 0}
    stats = {"enabled": True, "sent_count": 0, "failed_count": 0}
    destination = hashlib.sha256(settings.url.encode("utf-8")).hexdigest()
    with closing(sqlite3.connect(str(output_dir / "scanner.sqlite3"))) as connection:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS ntfy_sent (destination TEXT, listing_id TEXT, "
                               "sent_at TEXT, PRIMARY KEY (destination, listing_id))")
        delivered = {row[0] for row in connection.execute(
            "SELECT listing_id FROM ntfy_sent WHERE destination = ?", (destination,))}
        # Listing IDs include the provider; a cross-post can produce two alerts.
        pending = {room["id"]: room for room in rooms if room["id"] not in delivered}
        for index, room in enumerate(pending.values()):
            if index:
                time.sleep(1)
            try:
                publish(settings, room)
            except NotificationError as exc:
                stats.update(failed_count=len(pending) - stats["sent_count"], error=str(exc))
                LOG.error("%s", exc)
                break
            with connection:
                connection.execute("INSERT INTO ntfy_sent VALUES (?, ?, ?)",
                                   (destination, room["id"], datetime.now(timezone.utc).isoformat()))
            stats["sent_count"] += 1
    LOG.info("ntfy: %s sent; %s awaiting delivery", stats["sent_count"], stats["failed_count"])
    return stats
