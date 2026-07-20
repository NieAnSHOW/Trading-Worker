"""Safe, bounded normalization of RSS and Atom feed payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
from unicodedata import normalize
from urllib.parse import urlsplit

from .catalog import SourceAssignment


MAX_TITLE_LENGTH = 300
MAX_SUMMARY_LENGTH = 1000
_FORBIDDEN_XML_MARKERS = (
    b"<!doctype",
    b"<!entity",
    b"<?xml-stylesheet",
    b"http://www.w3.org/1999/xsl/transform",
    b"<xsl:",
)
_CDATA_OR_COMMENT = re.compile(rb"<!\[CDATA\[.*?\]\]>|<!--.*?-->", re.DOTALL)


class FeedParseError(ValueError):
    """A stable, non-sensitive parse failure for one feed endpoint."""


@dataclass(frozen=True)
class RawFeedItem:
    title: str
    summary: str | None
    published_at: datetime | None
    url: str
    source: SourceAssignment


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def parse_feed(payload: bytes, source: SourceAssignment) -> tuple[RawFeedItem, ...]:
    """Parse an untrusted RSS or Atom response without fetching linked content."""
    if _contains_forbidden_xml_markup(payload):
        raise FeedParseError("invalid_xml")

    try:
        from defusedxml import ElementTree as safe_element_tree
    except ImportError as exc:
        raise FeedParseError("parser_unavailable") from exc

    try:
        root = safe_element_tree.fromstring(payload)
    except Exception as exc:
        raise FeedParseError("invalid_xml") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        candidates = root.findall(".//item")
        return tuple(item for candidate in candidates if (item := _rss_item(candidate, source)) is not None)
    if root_name == "feed":
        candidates = tuple(element for element in root.iter() if _local_name(element.tag) == "entry")
        return tuple(item for candidate in candidates if (item := _atom_item(candidate, source)) is not None)
    raise FeedParseError("invalid_xml")


def _contains_forbidden_xml_markup(payload: bytes) -> bool:
    """Ignore declaration-like text in CDATA or comments before prefiltering."""
    markup = _CDATA_OR_COMMENT.sub(b"", payload).lower()
    return any(marker in markup for marker in _FORBIDDEN_XML_MARKERS)


def _rss_item(element: Any, source: SourceAssignment) -> RawFeedItem | None:
    return _make_item(
        title=_element_text(_child(element, "title")),
        summary=_element_text(_child(element, "description")) or _element_text(_child(element, "summary")),
        published_at=_parse_time(_element_text(_child(element, "pubDate"))),
        url=_element_text(_child(element, "link")),
        source=source,
    )


def _atom_item(element: Any, source: SourceAssignment) -> RawFeedItem | None:
    links = tuple(child for child in element if _local_name(child.tag) == "link")
    alternate = next((link.get("href") for link in links if link.get("rel", "alternate") == "alternate"), None)
    url = alternate or next((link.get("href") for link in links), None)
    return _make_item(
        title=_element_text(_child(element, "title")),
        summary=_element_text(_child(element, "summary")) or _element_text(_child(element, "content")),
        published_at=_parse_time(_element_text(_child(element, "published")) or _element_text(_child(element, "updated"))),
        url=url,
        source=source,
    )


def _make_item(
    title: str | None, summary: str | None, published_at: datetime | None, url: str | None, source: SourceAssignment
) -> RawFeedItem | None:
    plain_title = _plain_text(title)
    plain_url = _plain_text(url)
    if not plain_title or not _is_http_url(plain_url):
        return None
    plain_summary = _plain_text(summary)
    return RawFeedItem(
        title=plain_title[:MAX_TITLE_LENGTH],
        summary=plain_summary[:MAX_SUMMARY_LENGTH] or None,
        published_at=published_at,
        url=plain_url,
        source=source,
    )


def _child(element: Any, name: str) -> Any | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _element_text(element: Any | None) -> str | None:
    if element is None:
        return None
    return "".join(element.itertext())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _plain_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(unescape(value))
    parser.close()
    return re.sub(r"\s+", " ", normalize("NFKC", " ".join(parser.parts))).strip()


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
