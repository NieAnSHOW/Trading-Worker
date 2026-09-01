"""Mirror config persistence for the optional-deps installer.

The chosen PyPI mirror is stored as JSON under
``~/.vibe-trading/runtime/optional_deps_mirror.json`` so it survives
restarts and is independent of the bundle template (which only manages
``runtime/agent``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

MIRROR_URLS = {
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "official": "https://pypi.org/simple",
}

# Bootstrap fallback order: try the user-selected mirror first, then each
# domestic mirror in turn. Official PyPI is intentionally excluded — it is
# slow / blocked in the domestic networks this fallback targets (only
# reached when every mirror is hijacked, where it won't help either).
FALLBACK_MIRRORS = ("tsinghua", "aliyun")

DEFAULT_MIRROR = "tsinghua"

_VALID_NAMES = {"tsinghua", "aliyun", "official", "custom", "off"}


def default_config_path() -> Path:
    """Return the on-disk config path under the writable runtime root."""
    return Path.home() / ".vibe-trading" / "runtime" / "optional_deps_mirror.json"


@dataclass
class MirrorConfig:
    """Persisted mirror selection.

    Attributes:
        name: One of tsinghua / aliyun / official / custom / off.
            ``off`` means "no index-url override" (pip uses official PyPI).
        custom_index_url: Only used when ``name == "custom"``.
    """

    name: str = DEFAULT_MIRROR
    custom_index_url: str = ""


def load_mirror_config(path: Optional[Path] = None) -> MirrorConfig:
    """Load the mirror config, falling back to the default when absent."""
    path = path or default_config_path()
    if not path.exists():
        return MirrorConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MirrorConfig()
    name = str(raw.get("name") or DEFAULT_MIRROR)
    if name not in _VALID_NAMES:
        name = DEFAULT_MIRROR
    return MirrorConfig(
        name=name,
        custom_index_url=str(raw.get("custom_index_url") or ""),
    )


def save_mirror_config(config: MirrorConfig, path: Optional[Path] = None) -> None:
    """Persist the mirror config to disk."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_index_url(config: MirrorConfig) -> str:
    """Return the ``--index-url`` value for pip, or ``""`` for official PyPI.

    ``""`` instructs the installer to omit ``--index-url`` entirely so pip
    falls back to its built-in official index.
    """
    if config.name == "off":
        return ""
    if config.name == "custom":
        return config.custom_index_url.strip()
    return MIRROR_URLS.get(config.name, MIRROR_URLS[DEFAULT_MIRROR])


def resolve_trusted_host(config: MirrorConfig) -> str:
    """Return the ``--trusted-host`` value for non-HTTPS mirrors, else ``""``.

    All bundled mirrors use HTTPS, so this is normally empty. Exposed so a
    user-configured ``http://`` custom mirror can still install.
    """
    url = resolve_index_url(config)
    if url.startswith("https://"):
        return ""
    # Strip scheme + path to get the bare host.
    bare = url.split("://", 1)[-1].split("/", 1)[0]
    return bare
