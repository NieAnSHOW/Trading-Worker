"""Immutable access to the vendored investment-news source catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping


FIXED_UPSTREAM_REPOSITORY = "https://github.com/simonlin1212/investment-news"
FIXED_UPSTREAM_COMMIT = "d98aa603228f4839fb48859812c63a58ca10cead"


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
    data = json.loads(files("src.news").joinpath("upstream_manifest.json").read_text(encoding="utf-8"))
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
