#!/usr/bin/env python3
"""Vendor the pinned investment-news catalog from Git objects only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


FIXED_UPSTREAM_REPOSITORY = "https://github.com/simonlin1212/investment-news"
FIXED_UPSTREAM_COMMIT = "d98aa603228f4839fb48859812c63a58ca10cead"
SCHEMA_VERSION = 1
GIT_ENV = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}


def _git_show(repository: str, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", repository, "show", f"{commit}:{path}"],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )
    return result.stdout


def _verify_commit(repository: str, commit: str) -> None:
    subprocess.run(
        ["git", "-C", repository, "cat-file", "-e", f"{commit}^{{commit}}"], check=True, env=GIT_ENV
    )


def _assignment_id(track_id: str, url: str) -> str:
    return hashlib.sha256(f"{track_id}\0{url}".encode("utf-8")).hexdigest()[:16]


def _manifest_from_sources(sources: dict[str, Any]) -> dict[str, Any]:
    tracks = [
        {"id": item["key"], "name": item["name"], "accent": item["accent"]}
        for item in sources["industries"]
    ]
    assignments = [
        {
            "id": _assignment_id(item["hint"], item["url"]),
            "track_id": item["hint"],
            "name": item["name"],
            "url": item["url"],
            "filters": item.get("filters", []),
        }
        for item in sources["sources"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": FIXED_UPSTREAM_REPOSITORY,
        "commit": FIXED_UPSTREAM_COMMIT,
        "tracks": tracks,
        "sources": assignments,
        "filters": sources.get("redline_keywords", []),
    }


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8", newline="\n")


def import_upstream(repository: str, commit: str, output_dir: Path) -> None:
    if commit != FIXED_UPSTREAM_COMMIT:
        raise ValueError(f"commit must be the fixed pin {FIXED_UPSTREAM_COMMIT}")
    _verify_commit(repository, commit)
    sources = json.loads(_git_show(repository, commit, "sources.json"))
    license_text = _git_show(repository, commit, "LICENSE").decode("utf-8")
    manifest = _manifest_from_sources(sources)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    notice = (
        "# investment-news\n\n"
        f"Source: {FIXED_UPSTREAM_REPOSITORY} at commit {FIXED_UPSTREAM_COMMIT}\n\n"
        "The following notice is included under the upstream MIT License:\n\n"
        f"{license_text.rstrip()}\n"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_text(output_dir / "upstream_manifest.json", manifest_text)
    _write_text(output_dir / "upstream_manifest.sha256", hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() + "\n")
    _write_text(output_dir / "THIRD_PARTY_NOTICES.md", notice)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        import_upstream(args.repository, args.commit, args.output_dir)
    except (OSError, subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
