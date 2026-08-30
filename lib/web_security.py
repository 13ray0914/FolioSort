from __future__ import annotations

import json
from typing import Any, BinaryIO, Mapping
from urllib.parse import urlsplit


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def html_script_json(value: Any, *, ensure_ascii: bool = False) -> str:
    """Serialize JSON for use inside an HTML script element.

    HTML parses a literal ``</script`` even when it appears inside a JavaScript
    string. Escaping HTML-significant characters keeps untrusted metadata from
    terminating the surrounding script element while preserving valid JSON.
    """
    return (
        json.dumps(value, ensure_ascii=ensure_ascii)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def is_loopback_http_url(value: str | None, port: int) -> bool:
    """Return whether an Origin/Referer URL targets this loopback service."""
    if not value or value == "null":
        return False
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port or (80 if parsed.scheme == "http" else None)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in LOOPBACK_HOSTS
        and parsed_port == int(port)
        and parsed.username is None
        and parsed.password is None
    )


def browser_request_is_trusted(headers: Mapping[str, str], port: int) -> bool:
    """Reject browser cross-site writes while retaining local CLI access.

    Explicit Origin/Referer headers must name the loopback service. Requests
    without browser provenance headers are retained for local scripts and curl.
    """
    origin = headers.get("Origin")
    if origin is not None:
        return is_loopback_http_url(origin, port)

    referer = headers.get("Referer")
    if referer:
        return is_loopback_http_url(referer, port)

    fetch_site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    return fetch_site in {"", "none", "same-origin", "same-site"}


def read_json_object(
    headers: Mapping[str, str],
    stream: BinaryIO,
    *,
    max_bytes: int,
    empty_message: str = "A JSON request body is required",
) -> dict[str, Any]:
    """Read one size-limited application/json object from an HTTP request."""
    content_type = str(headers.get("Content-Type") or "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("application/json required")
    try:
        length = int(headers.get("Content-Length") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Content-Length") from exc
    if length <= 0:
        raise ValueError(empty_message)
    if length > int(max_bytes):
        raise ValueError(f"JSON request body exceeds {int(max_bytes)} bytes")
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError("Incomplete JSON request body")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value
