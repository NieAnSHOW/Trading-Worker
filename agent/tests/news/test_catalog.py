from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.news.catalog import FIXED_UPSTREAM_COMMIT, FIXED_UPSTREAM_REPOSITORY, group_endpoints, load_catalog


ROOT = Path(__file__).resolve().parents[3]
NEWS_DIR = ROOT / "agent" / "src" / "news"
IMPORTER = ROOT / "scripts" / "news" / "import_upstream.py"
GOLDEN_MANIFEST_SHA256 = "5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6"


def test_catalog_matches_the_pinned_upstream_contract() -> None:
    catalog = load_catalog()

    assert catalog.repository == FIXED_UPSTREAM_REPOSITORY
    assert catalog.commit == FIXED_UPSTREAM_COMMIT == "d98aa603228f4839fb48859812c63a58ca10cead"
    assert len(catalog.tracks) == 12
    assert len(catalog.assignments) == 108
    assert len({(item.track_id, item.url) for item in catalog.assignments}) == 108
    assert len({item.id for item in catalog.assignments}) == 108
    with pytest.raises(TypeError):
        catalog.tracks[0]["id"] = "changed"


def test_endpoint_grouping_is_deterministic_and_preserves_shared_feeds() -> None:
    endpoints = group_endpoints(load_catalog())

    assert len(endpoints) == 106
    assert tuple(item.url for item in endpoints) == tuple(sorted(item.url for item in endpoints))
    assert all(item.id == hashlib.sha256(item.url.encode("utf-8")).hexdigest()[:16] for item in endpoints)
    assert all(
        tuple(assignment.id for assignment in item.assignments)
        == tuple(sorted(assignment.id for assignment in item.assignments))
        for item in endpoints
    )
    by_url = {item.url: item for item in endpoints}
    assert len(by_url["https://sspai.com/feed"].assignments) == 2
    assert len(by_url["https://www.engadget.com/rss.xml"].assignments) == 2


def test_manifest_hash_and_mit_notice_are_pinned() -> None:
    manifest = NEWS_DIR / "upstream_manifest.json"
    digest = NEWS_DIR / "upstream_manifest.sha256"
    notice = NEWS_DIR / "THIRD_PARTY_NOTICES.md"

    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == GOLDEN_MANIFEST_SHA256
    assert digest.read_text(encoding="utf-8").strip() == GOLDEN_MANIFEST_SHA256
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["repository"] == FIXED_UPSTREAM_REPOSITORY
    assert payload["commit"] == FIXED_UPSTREAM_COMMIT
    assert payload["schema_version"] == 1
    assert "MIT License" in notice.read_text(encoding="utf-8")
    assert "Copyright (c) 2026 simonlin1212" in notice.read_text(encoding="utf-8")


def test_importer_reads_git_objects_not_dirty_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "upstream"
    subprocess.run(["git", "clone", "-q", FIXED_UPSTREAM_REPOSITORY, str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "checkout", "-q", FIXED_UPSTREAM_COMMIT], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "tests@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Tests"], check=True)
    source_path = repository / "sources.json"
    source_path.write_text(source_path.read_text(encoding="utf-8").replace("OpenAI", "Replaced source", 1), encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "sources.json"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "replacement"], check=True)
    subprocess.run(["git", "-C", str(repository), "replace", FIXED_UPSTREAM_COMMIT, "HEAD"], check=True)
    (repository / "sources.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "output"

    result = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            "--repository",
            str(repository),
            "--commit",
            FIXED_UPSTREAM_COMMIT,
            "--output-dir",
            str(output),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    sources = json.loads((output / "upstream_manifest.json").read_text(encoding="utf-8"))["sources"]
    assert sources
    assert sources[0]["name"] == "OpenAI"


def test_importer_rejects_any_commit_other_than_the_fixed_pin(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            "--repository",
            str(tmp_path),
            "--commit",
            "0" * 40,
            "--output-dir",
            str(tmp_path / "output"),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "fixed" in result.stderr.lower()
