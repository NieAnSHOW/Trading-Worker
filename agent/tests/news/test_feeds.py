from __future__ import annotations

import builtins
import importlib
from pathlib import Path
import sys

import pytest

from src.news.catalog import SourceAssignment


FIXTURES = Path(__file__).with_name("fixtures")
ASSIGNMENT = SourceAssignment(
    id="ai-fixture",
    track_id="ai",
    name="Fixture News",
    url="https://feed.example/rss",
    filters=(),
)


def test_module_import_does_not_require_defusedxml(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules.pop("src.news.feeds", None)
    original_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "defusedxml" or name.startswith("defusedxml."):
            raise ImportError("unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    module = importlib.import_module("src.news.feeds")

    assert module.parse_feed


def test_rss_parses_only_valid_items_and_normalizes_text() -> None:
    from src.news.feeds import parse_feed

    items = parse_feed((FIXTURES / "rss.xml").read_bytes(), ASSIGNMENT)

    assert len(items) == 1
    assert items[0].title == "Market update"
    assert items[0].summary == "First & second"
    assert items[0].url == "https://news.example/articles/market"
    assert items[0].source == ASSIGNMENT
    assert items[0].published_at.isoformat() == "2026-07-20T10:30:00+00:00"


def test_atom_keeps_missing_optional_summary_and_time_as_none() -> None:
    from src.news.feeds import parse_feed

    items = parse_feed((FIXTURES / "atom.xml").read_bytes(), ASSIGNMENT)

    assert len(items) == 1
    assert items[0].title == "Atom entry"
    assert items[0].summary is None
    assert items[0].published_at is None


@pytest.mark.parametrize(
    ("payload", "case"),
    [
        (
            b"<rss><channel><item><link>https://news.example/a</link></item></channel></rss>",
            "missing title",
        ),
        (
            b"<rss><channel><item><title> \t </title><link>https://news.example/a</link></item></channel></rss>",
            "whitespace title",
        ),
        (
            b"<rss><channel><item><title>RSS entry</title></item></channel></rss>",
            "RSS missing link",
        ),
        (
            b"<feed xmlns=\"http://www.w3.org/2005/Atom\"><entry><title>Atom entry</title><link /></entry></feed>",
            "Atom missing href",
        ),
    ],
)
def test_parse_feed_drops_entries_missing_required_title_or_link(payload: bytes, case: str) -> None:
    from src.news.feeds import parse_feed

    assert parse_feed(payload, ASSIGNMENT) == (), case


def test_malicious_dtd_is_rejected_without_expansion() -> None:
    from src.news.feeds import FeedParseError, parse_feed

    with pytest.raises(FeedParseError, match="^invalid_xml$"):
        parse_feed((FIXTURES / "malicious_dtd.xml").read_bytes(), ASSIGNMENT)


def test_valid_cdata_that_mentions_dtd_is_not_treated_as_a_declaration() -> None:
    from src.news.feeds import parse_feed

    payload = b"""<rss><channel><item>
      <title>Security update</title>
      <link>https://news.example/security</link>
      <description><![CDATA[The string <!DOCTYPE rss> is not XML markup here.]]></description>
    </item></channel></rss>"""

    items = parse_feed(payload, ASSIGNMENT)

    assert len(items) == 1
    assert items[0].summary == "The string is not XML markup here."


def test_xslt_processing_instruction_is_rejected() -> None:
    from src.news.feeds import FeedParseError, parse_feed

    payload = b'''<?xml version="1.0"?>
    <?xml-stylesheet type="text/xsl" href="https://attacker.example/feed.xsl"?>
    <rss><channel><item><title>Title</title><link>https://news.example/a</link></item></channel></rss>'''

    with pytest.raises(FeedParseError, match="^invalid_xml$"):
        parse_feed(payload, ASSIGNMENT)


def test_parser_unavailable_has_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.news.feeds import FeedParseError, parse_feed

    original_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "defusedxml" or name.startswith("defusedxml."):
            raise ImportError("unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(FeedParseError, match="^parser_unavailable$"):
        parse_feed(b"<rss />", ASSIGNMENT)


def test_text_limits_and_non_http_links_are_enforced() -> None:
    from src.news.feeds import parse_feed

    oversized_title = "A" * 301
    oversized_summary = "B" * 1001
    feed = f"""<rss><channel>
      <item><title>{oversized_title}</title><link>https://news.example/long</link><description>{oversized_summary}</description></item>
      <item><title>File</title><link>file:///etc/passwd</link></item>
    </channel></rss>""".encode()

    items = parse_feed(feed, ASSIGNMENT)

    assert len(items) == 1
    assert len(items[0].title) == 300
    assert items[0].summary is not None
    assert len(items[0].summary) == 1000
