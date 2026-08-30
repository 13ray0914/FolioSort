from __future__ import annotations

import io
import json
import unittest

from lib.web_security import (
    browser_request_is_trusted,
    html_script_json,
    is_loopback_http_url,
    read_json_object,
)


class HtmlScriptJsonTests(unittest.TestCase):
    def test_script_end_tag_and_html_characters_are_escaped(self) -> None:
        payload = {"title": "</script><script>alert(1)</script>&\u2028\u2029"}

        encoded = html_script_json(payload)

        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertNotIn("\u2028", encoded)
        self.assertNotIn("\u2029", encoded)
        self.assertEqual(json.loads(encoded), payload)


class LoopbackRequestTests(unittest.TestCase):
    def test_loopback_origins_for_service_port_are_accepted(self) -> None:
        self.assertTrue(is_loopback_http_url("http://127.0.0.1:8765", 8765))
        self.assertTrue(is_loopback_http_url("http://localhost:8765/path", 8765))
        self.assertTrue(is_loopback_http_url("http://[::1]:8765", 8765))

    def test_untrusted_or_opaque_origins_are_rejected(self) -> None:
        self.assertFalse(is_loopback_http_url("null", 8765))
        self.assertFalse(is_loopback_http_url("https://attacker.example", 8765))
        self.assertFalse(is_loopback_http_url("http://127.0.0.1:9999", 8765))
        self.assertFalse(is_loopback_http_url("http://user@127.0.0.1:8765", 8765))

    def test_browser_provenance_is_enforced(self) -> None:
        self.assertTrue(browser_request_is_trusted({"Origin": "http://localhost:8765"}, 8765))
        self.assertFalse(browser_request_is_trusted({"Origin": "null"}, 8765))
        self.assertFalse(browser_request_is_trusted({"Origin": "https://attacker.example"}, 8765))
        self.assertFalse(browser_request_is_trusted({"Referer": "https://attacker.example/page"}, 8765))
        self.assertFalse(browser_request_is_trusted({"Sec-Fetch-Site": "cross-site"}, 8765))

    def test_headerless_local_clients_remain_supported(self) -> None:
        self.assertTrue(browser_request_is_trusted({}, 8765))


class JsonRequestTests(unittest.TestCase):
    def test_valid_json_object_is_read(self) -> None:
        raw = b'{"paper_id":"P0001"}'
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(raw)),
        }

        self.assertEqual(
            read_json_object(headers, io.BytesIO(raw), max_bytes=1024),
            {"paper_id": "P0001"},
        )

    def test_simple_cross_site_content_type_is_rejected(self) -> None:
        raw = b'{"paper_id":"P0001"}'
        headers = {"Content-Type": "text/plain", "Content-Length": str(len(raw))}

        with self.assertRaisesRegex(ValueError, "application/json required"):
            read_json_object(headers, io.BytesIO(raw), max_bytes=1024)

    def test_oversized_body_is_rejected_before_reading(self) -> None:
        stream = io.BytesIO(b"{}")
        headers = {"Content-Type": "application/json", "Content-Length": "2048"}

        with self.assertRaisesRegex(ValueError, "exceeds 1024 bytes"):
            read_json_object(headers, stream, max_bytes=1024)
        self.assertEqual(stream.tell(), 0)

    def test_json_arrays_are_rejected(self) -> None:
        raw = b"[]"
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}

        with self.assertRaisesRegex(ValueError, "JSON object"):
            read_json_object(headers, io.BytesIO(raw), max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
