"""Immutable access to the vendored investment-news source catalog."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


FIXED_UPSTREAM_REPOSITORY = "https://github.com/simonlin1212/investment-news"
FIXED_UPSTREAM_COMMIT = "d98aa603228f4839fb48859812c63a58ca10cead"
GOLDEN_MANIFEST_SHA256 = "5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6"
EXPECTED_SCHEMA_VERSION = 1
EXPECTED_TRACK_IDS = frozenset(
    {"ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"}
)
EXPECTED_SOURCE_COUNT = 108
EXPECTED_ENDPOINT_COUNT = 106


@dataclass(frozen=True)
class SourceAssignment:
    id: str
    track_id: str
    name: str
    url: str
    filters: tuple[str, ...]


@dataclass(frozen=True)
class FeedEndpoint:
    id: str
    url: str
    assignments: tuple[SourceAssignment, ...]


@dataclass(frozen=True)
class NewsCatalog:
    schema_version: int
    repository: str
    commit: str
    tracks: tuple[Mapping[str, str], ...]
    assignments: tuple[SourceAssignment, ...]
    filters: tuple[str, ...]


def load_catalog() -> NewsCatalog:
    package_files = files("src.news")
    manifest_bytes = package_files.joinpath("upstream_manifest.json").read_bytes()
    actual_digest = hashlib.sha256(manifest_bytes).hexdigest()
    sidecar_digest = package_files.joinpath("upstream_manifest.sha256").read_text(encoding="utf-8").strip()
    if actual_digest != sidecar_digest or actual_digest != GOLDEN_MANIFEST_SHA256:
        raise ValueError("catalog manifest digest mismatch")

    data = json.loads(manifest_bytes)
    _validate_manifest(data)
    assignments = tuple(
        SourceAssignment(
            id=item["id"],
            track_id=item["track_id"],
            name=item["name"],
            url=item["url"],
            filters=tuple(item["filters"]),
        )
        for item in data["sources"]
    )
    return NewsCatalog(
        schema_version=data["schema_version"],
        repository=data["repository"],
        commit=data["commit"],
        tracks=tuple(MappingProxyType(dict(item)) for item in data["tracks"]),
        assignments=assignments,
        filters=tuple(data["filters"]),
    )


def _validate_manifest(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("catalog manifest must be an object")
    if (
        data.get("schema_version") != EXPECTED_SCHEMA_VERSION
        or data.get("repository") != FIXED_UPSTREAM_REPOSITORY
        or data.get("commit") != FIXED_UPSTREAM_COMMIT
    ):
        raise ValueError("catalog pin does not match the fixed upstream contract")

    tracks = data.get("tracks")
    sources = data.get("sources")
    filters = data.get("filters")
    if not isinstance(tracks, list) or len(tracks) != len(EXPECTED_TRACK_IDS):
        raise ValueError("catalog track count does not match the fixed upstream contract")
    if not isinstance(sources, list) or len(sources) != EXPECTED_SOURCE_COUNT:
        raise ValueError("catalog source count does not match the fixed upstream contract")
    if not isinstance(filters, list) or not all(isinstance(item, str) for item in filters):
        raise ValueError("catalog filters are invalid")

    track_ids: list[str] = []
    for track in tracks:
        if not isinstance(track, dict):
            raise ValueError("catalog track is invalid")
        track_id = track.get("id")
        if not isinstance(track_id, str) or not track_id:
            raise ValueError("catalog track id is invalid")
        if not isinstance(track.get("name"), str) or not track["name"]:
            raise ValueError("catalog track name is invalid")
        track_ids.append(track_id)
    if len(track_ids) != len(set(track_ids)) or set(track_ids) != EXPECTED_TRACK_IDS:
        raise ValueError("catalog track ids do not match the fixed upstream contract")

    assignment_ids: set[str] = set()
    track_urls: set[tuple[str, str]] = set()
    endpoint_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("catalog source is invalid")
        track_id = source.get("track_id")
        source_id = source.get("id")
        url = source.get("url")
        if not isinstance(track_id, str) or track_id not in EXPECTED_TRACK_IDS:
            raise ValueError("catalog source track is invalid")
        if not isinstance(url, str) or not _is_valid_feed_url(url):
            raise ValueError("catalog source URL is invalid")
        expected_id = hashlib.sha256(f"{track_id}\0{url}".encode("utf-8")).hexdigest()[:16]
        if source_id != expected_id or source_id in assignment_ids:
            raise ValueError("catalog source id is invalid")
        if (track_id, url) in track_urls:
            raise ValueError("catalog source assignment is duplicated")
        if not isinstance(source.get("name"), str) or not source["name"]:
            raise ValueError("catalog source name is invalid")
        source_filters = source.get("filters")
        if not isinstance(source_filters, list) or not all(isinstance(item, str) for item in source_filters):
            raise ValueError("catalog source filters are invalid")
        assignment_ids.add(source_id)
        track_urls.add((track_id, url))
        endpoint_urls.add(url)
    if len(endpoint_urls) != EXPECTED_ENDPOINT_COUNT:
        raise ValueError("catalog endpoint count does not match the fixed upstream contract")


def _is_valid_feed_url(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
    except (UnicodeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and _is_public_hostname(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _is_public_hostname(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if len(ascii_hostname) > 253 or ascii_hostname.startswith(".") or ascii_hostname.endswith("."):
            return False
        labels = ascii_hostname.split(".")
        return all(
            0 < len(label) <= 63
            and label[0] != "-"
            and label[-1] != "-"
            and all(character.isascii() and (character.isalnum() or character == "-") for character in label)
            for label in labels
        )
    return address.is_global and not address.is_multicast


def group_endpoints(catalog: NewsCatalog) -> tuple[FeedEndpoint, ...]:
    by_url: dict[str, list[SourceAssignment]] = {}
    for assignment in catalog.assignments:
        by_url.setdefault(assignment.url, []).append(assignment)
    return tuple(
        FeedEndpoint(
            id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            url=url,
            assignments=tuple(sorted(assignments, key=lambda item: item.id)),
        )
        for url, assignments in sorted(by_url.items())
    )
