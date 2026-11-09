import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import main as scanner
import notifications as ntfy
from test_scanner import ROOM, CENTER


SETTINGS = ntfy.NtfySettings("https://ntfy.sh", "test-topic", "test-token")
MATCH = {"id": "flatfox:1", "source": "Flatfox", "address": "Bucheggstrasse 10, 8037 Zürich",
         "price_chf": 587, "available_from": "2026-12-16", "temporary": True, "title": "Möbliertes Zimmer",
         "url": "https://flatfox.ch/en/flat/1/", "is_new": False}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"NTFY_TOPIC": "", "NTFY_TOKEN": "", "NTFY_SERVER": ""})
        environment.start()
        self.addCleanup(environment.stop)
        sleep = patch.object(ntfy.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_environment_settings_are_optional_and_validate_without_exposing_secrets(self):
        self.assertIsNone(ntfy.settings_from_env())
        with patch.dict(os.environ, {"NTFY_TOPIC": "my-private-topic", "NTFY_TOKEN": "private-token"}):
            settings = ntfy.settings_from_env()
            self.assertEqual(settings.url, "https://ntfy.sh/my-private-topic")
            self.assertNotIn("my-private-topic", repr(settings))
            self.assertNotIn("private-token", repr(settings))
        for env in ({"NTFY_TOPIC": "invalid/topic"},
                    {"NTFY_TOPIC": "test", "NTFY_SERVER": "http://ntfy.example"},
                    {"NTFY_TOPIC": "test", "NTFY_SERVER": "https://ntfy.example?secret=value"},
                    {"NTFY_TOPIC": "test", "NTFY_SERVER": "https://[broken"},
                    {"NTFY_TOPIC": "test", "NTFY_TOKEN": "secret\r\ninjected"}):
            with patch.dict(os.environ, env), self.assertRaises(ntfy.NotificationError):
                ntfy.settings_from_env()

    def test_utf8_notification_uses_authentication_and_listing_click_link(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"event": "message", "topic": SETTINGS.topic, "id": "message-id"}).encode()
        with patch.object(ntfy, "build_opener", return_value=opener):
            ntfy.publish(SETTINGS, MATCH)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, SETTINGS.url)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(request.get_header("Click"), MATCH["url"])
        message = request.data.decode("utf-8")
        self.assertIn("Zürich", message)
        self.assertIn("CHF 587/month", message)
        self.assertIn("Temporary rental", message)

    def test_http_failure_does_not_expose_topic_or_token(self):
        error = HTTPError(SETTINGS.url, 401, "Unauthorized", {}, None)
        with patch.object(ntfy, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ntfy.NotificationError) as caught:
                ntfy.publish(SETTINGS, MATCH)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn(SETTINGS.topic, str(caught.exception))
        self.assertNotIn(SETTINGS.token, str(caught.exception))
        self.assertIsNone(ntfy.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_successful_delivery_is_persistent_and_independent_of_scan_newness(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ntfy, "publish") as publish:
            output = Path(directory)
            first = ntfy.notify_rooms(SETTINGS, [MATCH, MATCH], output)
            second = ntfy.notify_rooms(SETTINGS, [dict(MATCH, is_new=True)], output)
            self.assertEqual((first["sent_count"], second["sent_count"]), (1, 0))
            self.assertEqual(publish.call_count, 1)
            ntfy.notify_rooms(ntfy.NtfySettings("https://ntfy.sh", "other-topic"), [MATCH], output)
            self.assertEqual(publish.call_count, 2)

    def test_failed_delivery_retries_but_does_not_resend_successful_rooms(self):
        second = dict(MATCH, id="wgzimmer:2")
        with tempfile.TemporaryDirectory() as directory, patch.object(ntfy, "publish") as publish:
            publish.side_effect = [None, ntfy.NotificationError("ntfy unavailable")]
            first = ntfy.notify_rooms(SETTINGS, [MATCH, second], Path(directory))
            self.assertEqual((first["sent_count"], first["failed_count"]), (1, 1))
            publish.reset_mock()
            publish.side_effect = None
            retry = ntfy.notify_rooms(SETTINGS, [MATCH, second], Path(directory))
            self.assertEqual((retry["sent_count"], retry["failed_count"]), (1, 0))
            publish.assert_called_once_with(SETTINGS, second)

    def test_unconfirmed_http_response_is_not_marked_delivered(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ntfy, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'<html>Not ntfy</html>'
            failed = ntfy.notify_rooms(SETTINGS, [MATCH], Path(directory))
            self.assertEqual(failed["failed_count"], 1)
            response.read.return_value = b'{"event":"message","topic":"test-topic","id":"message-id"}'
            retry = ntfy.notify_rooms(SETTINGS, [MATCH], Path(directory))
            self.assertEqual(retry["sent_count"], 1)

    def test_disabled_notifications_do_not_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ntfy, "publish") as publish:
            stats = ntfy.notify_rooms(None, [MATCH], Path(directory))
            self.assertFalse(stats["enabled"])
            publish.assert_not_called()

    def test_scan_alerts_only_for_bucheggstrasse_rooms_within_filters(self):
        listings = [dict(ROOM, pk=i, rent_gross=700, public_address=address, description_title="Bucheggstrasse room")
                    for i, address in enumerate(("Bucheggstrasse 10, Zürich", "Bucheggstrasse, Zürich",
                                                  "Bucheggstrasse 14", "Other street 10", "Bucheggstrasseweg 10"))]
        listings += [dict(ROOM, pk=100, rent_gross=801, public_address="Bucheggstrasse 8"),
                     dict(ROOM, pk=101, rent_gross=700, public_address="Bucheggstrasse 6", longitude=8.8)]
        with tempfile.TemporaryDirectory() as directory:
            options = scanner.parse_args(["--sources", "flatfox", "--output-dir", directory])
            with patch.object(scanner, "settings_from_env", return_value=SETTINGS), \
                    patch.object(scanner, "fetch_pins", return_value=[{"pk": room["pk"]} for room in listings]), \
                    patch.object(scanner, "fetch_details", return_value=listings), \
                    patch.object(ntfy, "publish") as publish, \
                    patch("sys.stdout", new_callable=io.StringIO):
                first = scanner.scan(options, CENTER)
                second = scanner.scan(options, CENTER)
            self.assertEqual(first["notifications"]["sent_count"], 3)
            self.assertEqual(second["notifications"]["sent_count"], 0)
            self.assertEqual(publish.call_count, 3)
            self.assertTrue(first["complete"])
            saved = json.loads((Path(directory) / "results.json").read_text())
            self.assertEqual(saved["notifications"]["sent_count"], 0)
            self.assertNotIn(SETTINGS.topic, json.dumps(saved))

    def test_notification_failure_has_distinct_exit_code_and_keeps_results(self):
        with tempfile.TemporaryDirectory() as directory:
            listings = [dict(ROOM, rent_gross=700, public_address="Bucheggstrasse 8")]
            with patch.object(scanner, "settings_from_env", return_value=SETTINGS), \
                    patch.object(scanner, "fetch_pins", return_value=[{"pk": 123}]), \
                    patch.object(scanner, "fetch_details", return_value=listings), \
                    patch.object(ntfy, "publish", side_effect=ntfy.NotificationError("ntfy unavailable")), \
                    patch("sys.stdout", new_callable=io.StringIO):
                code = scanner.main(["scan", "--center", "Zürich HB", "--sources", "flatfox", "--output-dir", directory])
            self.assertEqual(code, 3)
            saved = json.loads((Path(directory) / "results.json").read_text())
            self.assertTrue(saved["complete"])
            self.assertEqual(saved["notifications"]["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
