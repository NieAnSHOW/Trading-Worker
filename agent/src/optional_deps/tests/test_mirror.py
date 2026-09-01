"""Tests for optional_deps.mirror config persistence."""

from __future__ import annotations

import json

from src.optional_deps.mirror import (
    DEFAULT_MIRROR,
    MIRROR_URLS,
    MirrorConfig,
    load_mirror_config,
    resolve_index_url,
    save_mirror_config,
)


def test_default_mirror_is_tsinghua(tmp_path):
    cfg = load_mirror_config(tmp_path / "mirror.json")
    assert cfg.name == DEFAULT_MIRROR == "tsinghua"
    assert cfg.custom_index_url == ""


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "mirror.json"
    save_mirror_config(MirrorConfig(name="aliyun", custom_index_url=""), path)
    cfg = load_mirror_config(path)
    assert cfg.name == "aliyun"


def test_custom_mirror_persists_url(tmp_path):
    path = tmp_path / "mirror.json"
    save_mirror_config(
        MirrorConfig(name="custom", custom_index_url="https://my.mirror/simple"),
        path,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["custom_index_url"] == "https://my.mirror/simple"
    cfg = load_mirror_config(path)
    assert cfg.custom_index_url == "https://my.mirror/simple"


def test_resolve_index_url_tsinghua():
    cfg = MirrorConfig(name="tsinghua", custom_index_url="")
    assert resolve_index_url(cfg) == MIRROR_URLS["tsinghua"]


def test_resolve_index_url_off_returns_empty():
    """Mirror 'off' -> empty string -> pip uses official PyPI default."""
    cfg = MirrorConfig(name="off", custom_index_url="")
    assert resolve_index_url(cfg) == ""


def test_resolve_index_url_custom_uses_custom_url():
    cfg = MirrorConfig(name="custom", custom_index_url="https://x/simple")
    assert resolve_index_url(cfg) == "https://x/simple"


def test_resolve_index_url_official_uses_pypi():
    """'official' must resolve to pypi.org, not fall back to the tsinghua default."""
    cfg = MirrorConfig(name="official", custom_index_url="")
    assert resolve_index_url(cfg) == "https://pypi.org/simple"
