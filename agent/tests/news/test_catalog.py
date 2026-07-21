from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from src.news import catalog
from src.news.catalog import FIXED_UPSTREAM_COMMIT, FIXED_UPSTREAM_REPOSITORY, group_endpoints, load_catalog


ROOT = Path(__file__).resolve().parents[3]
NEWS_DIR = ROOT / "agent" / "src" / "news"
IMPORTER = ROOT / "scripts" / "news" / "import_upstream.py"
GOLDEN_MANIFEST_SHA256 = "5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6"
STATIC_ARTIFACTS = (
    "upstream_manifest.json",
    "upstream_manifest.sha256",
    "THIRD_PARTY_NOTICES.md",
)


def _load_mutated_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    *,
    update_sidecar: bool = True,
    trust_mutated_digest: bool = True,
) -> None:
    payload = json.loads((NEWS_DIR / "upstream_manifest.json").read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(payload)
    manifest_bytes = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    (tmp_path / "upstream_manifest.json").write_bytes(manifest_bytes)
    sidecar = digest if update_sidecar else GOLDEN_MANIFEST_SHA256
    (tmp_path / "upstream_manifest.sha256").write_text(sidecar + "\n", encoding="utf-8")
    monkeypatch.setattr(catalog, "files", lambda _: tmp_path)
    if trust_mutated_digest:
        monkeypatch.setattr(catalog, "GOLDEN_MANIFEST_SHA256", digest, raising=False)
    load_catalog()


def _set_source_url(payload: dict[str, object], value: str) -> None:
    sources = payload["sources"]
    assert isinstance(sources, list)
    source = sources[0]
    assert isinstance(source, dict)
    track_id = source["track_id"]
    assert isinstance(track_id, str)
    source["url"] = value
    source["id"] = hashlib.sha256(f"{track_id}\0{value}".encode("utf-8")).hexdigest()[:16]


def _assert_generated_artifacts_match_tracked(output_dir: Path) -> None:
    assert tuple(sorted(path.name for path in output_dir.iterdir() if path.is_file())) == tuple(sorted(STATIC_ARTIFACTS))
    for artifact in STATIC_ARTIFACTS:
        assert (output_dir / artifact).read_bytes() == (NEWS_DIR / artifact).read_bytes()


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
    assert len({item.id for item in endpoints}) == 106
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


def test_load_catalog_rejects_a_manifest_that_does_not_match_its_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="digest"):
        _load_mutated_catalog(
            tmp_path,
            monkeypatch,
            lambda payload: payload.update(repository="https://attacker.invalid/catalog"),
            update_sidecar=False,
            trust_mutated_digest=False,
        )


def test_load_catalog_rejects_a_rehashed_manifest_that_does_not_match_the_golden_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="digest"):
        _load_mutated_catalog(
            tmp_path,
            monkeypatch,
            lambda payload: payload.update(repository="https://attacker.invalid/catalog"),
            trust_mutated_digest=False,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda payload: payload.update(repository="https://attacker.invalid/catalog"), id="repository"),
        pytest.param(lambda payload: payload.update(commit="0" * 40), id="commit"),
        pytest.param(lambda payload: payload["tracks"].pop(), id="track-count"),
        pytest.param(lambda payload: payload["sources"].pop(), id="source-count"),
        pytest.param(lambda payload: payload["tracks"][1].update(id=payload["tracks"][0]["id"]), id="track-id"),
        pytest.param(lambda payload: payload["sources"][0].update(track_id="unknown"), id="source-track"),
        pytest.param(lambda payload: payload["sources"][0].update(id="0" * 16), id="source-id"),
        pytest.param(lambda payload: payload["sources"][0].update(url="file:///etc/passwd"), id="source-url"),
    ],
)
def test_load_catalog_fails_closed_when_pinned_contract_invariants_are_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: object
) -> None:
    with pytest.raises(ValueError, match="catalog"):
        _load_mutated_catalog(tmp_path, monkeypatch, mutate)


def test_load_catalog_rejects_a_manifest_with_fewer_than_106_unique_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def duplicate_an_endpoint(payload: dict[str, object]) -> None:
        sources = payload["sources"]
        assert isinstance(sources, list)
        url_counts = Counter(item["url"] for item in sources)
        existing_assignments = {(item["track_id"], item["url"]) for item in sources}
        source = next(item for item in sources if url_counts[item["url"]] == 1)
        replacement = next(
            item
            for item in sources
            if item["url"] != source["url"]
            and (source["track_id"], item["url"]) not in existing_assignments
        )
        source["url"] = replacement["url"]
        source["id"] = hashlib.sha256(
            f"{source['track_id']}\0{source['url']}".encode("utf-8")
        ).hexdigest()[:16]

    with pytest.raises(ValueError, match="endpoint count"):
        _load_mutated_catalog(tmp_path, monkeypatch, duplicate_an_endpoint)


@pytest.mark.parametrize(
    "url",
    [
        "https://exa mple.com/rss",
        "https://./rss",
        "https://example.com/\0rss",
        "http://127.0.0.1/rss",
    ],
)
def test_load_catalog_rejects_invalid_urls_even_when_the_stable_id_is_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ValueError, match="URL"):
        _load_mutated_catalog(tmp_path, monkeypatch, lambda payload: _set_source_url(payload, url))


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
    _assert_generated_artifacts_match_tracked(output)

    faulty_output = tmp_path / "missing-artifact-output"
    shutil.copytree(output, faulty_output)
    (faulty_output / "upstream_manifest.json").unlink()
    with pytest.raises(AssertionError):
        _assert_generated_artifacts_match_tracked(faulty_output)

    faulty_output = tmp_path / "corrupt-hash-output"
    shutil.copytree(output, faulty_output)
    (faulty_output / "upstream_manifest.sha256").write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_generated_artifacts_match_tracked(faulty_output)

    faulty_output = tmp_path / "truncated-notice-output"
    shutil.copytree(output, faulty_output)
    notice = faulty_output / "THIRD_PARTY_NOTICES.md"
    notice.write_bytes(notice.read_bytes()[:-1])
    with pytest.raises(AssertionError):
        _assert_generated_artifacts_match_tracked(faulty_output)


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
