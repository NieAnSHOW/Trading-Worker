from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tarfile
from types import ModuleType, SimpleNamespace
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[3]
NOTICE = ROOT / "agent" / "src" / "news" / "THIRD_PARTY_NOTICES.md"
REQUIRED_NEWS_ASSETS = (
    "upstream_manifest.json",
    "upstream_manifest.sha256",
    "THIRD_PARTY_NOTICES.md",
)


def archives_from_env() -> tuple[Path, Path]:
    value = os.environ.get("NEWS_DIST_DIR")
    if value is None:
        pytest.skip("NEWS_DIST_DIR is required for archive verification")

    dist_dir = Path(value)
    assert dist_dir.is_dir(), f"NEWS_DIST_DIR is not a directory: {dist_dir}"
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    assert wheels, f"NEWS_DIST_DIR contains no wheel: {dist_dir}"
    assert sdists, f"NEWS_DIST_DIR contains no sdist: {dist_dir}"
    assert len(wheels) == 1, f"NEWS_DIST_DIR must contain exactly one wheel, found {len(wheels)}"
    assert len(sdists) == 1, f"NEWS_DIST_DIR must contain exactly one sdist, found {len(sdists)}"
    return wheels[0], sdists[0]


def _single_member(members: list[str], suffix: str) -> str:
    matches = [member for member in members if member.endswith(suffix)]
    assert len(matches) == 1, f"expected one archive member ending in {suffix!r}, found {matches}"
    return matches[0]


def _assert_news_assets(files: dict[str, bytes]) -> None:
    for asset in REQUIRED_NEWS_ASSETS:
        assert asset in files

    repository_notice = NOTICE.read_text(encoding="utf-8")
    archived_notice = files["THIRD_PARTY_NOTICES.md"].decode("utf-8")
    assert archived_notice == repository_notice
    assert hashlib.sha256(archived_notice.encode("utf-8")).hexdigest() == hashlib.sha256(
        repository_notice.encode("utf-8")
    ).hexdigest()


def test_wheel_contains_complete_news_notice_and_manifest_assets() -> None:
    wheel, _ = archives_from_env()

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        files = {
            asset: archive.read(_single_member(names, f"src/news/{asset}"))
            for asset in REQUIRED_NEWS_ASSETS
        }

    _assert_news_assets(files)


def test_sdist_contains_complete_news_notice_and_manifest_assets() -> None:
    _, sdist = archives_from_env()

    with tarfile.open(sdist, mode="r:gz") as archive:
        names = archive.getnames()
        files = {}
        for asset in REQUIRED_NEWS_ASSETS:
            member = archive.getmember(_single_member(names, f"agent/src/news/{asset}"))
            stream = archive.extractfile(member)
            assert stream is not None
            files[asset] = stream.read()

    _assert_news_assets(files)


def test_archives_from_env_rejects_empty_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEWS_DIST_DIR", str(tmp_path))

    with pytest.raises(AssertionError, match="contains no wheel"):
        archives_from_env()


def test_tier0_smoke_requires_news_snapshot_route(monkeypatch: pytest.MonkeyPatch) -> None:
    script = ROOT / "scripts" / "desktop" / "smoke_tier0.py"
    spec = importlib.util.spec_from_file_location("test_smoke_tier0", script)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    fake_cli = ModuleType("cli")
    fake_api_server = ModuleType("api_server")
    fake_api_server.app = SimpleNamespace(routes=[])
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setitem(sys.modules, "api_server", fake_api_server)
    monkeypatch.setattr(smoke, "MODULES", [])

    assert smoke.main() == 1


def test_tier0_smoke_accepts_deferred_news_snapshot_route(monkeypatch: pytest.MonkeyPatch) -> None:
    script = ROOT / "scripts" / "desktop" / "smoke_tier0.py"
    spec = importlib.util.spec_from_file_location("test_smoke_tier0_deferred", script)
    assert spec is not None and spec.loader is not None
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)

    fake_cli = ModuleType("cli")
    fake_api_server = ModuleType("api_server")
    fake_api_server.app = SimpleNamespace(
        routes=[],
        url_path_for=lambda name: "/news-api/snapshot" if name == "get_snapshot" else None,
    )
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setitem(sys.modules, "api_server", fake_api_server)
    monkeypatch.setattr(smoke, "MODULES", [])

    assert smoke.main() == 0
