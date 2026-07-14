"""Security regression tests for backtest signal_engine loading."""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import ModuleType

import backtest.runner as runner
import pytest

from backtest.runner import _load_module_from_file


def _module_name() -> str:
    """Return a unique module name for import tests."""
    return f"signal_engine_test_{uuid.uuid4().hex}"


def test_signal_engine_rejects_top_level_execution(tmp_path) -> None:
    artifact = tmp_path / "top_level_rce"
    # ``Path.as_posix()`` so the embedded path uses forward slashes; the raw
    # Windows form ``C:\Users\...`` looks like ``\U`` (a unicode escape) when
    # interpolated into Python source and breaks ``ast.parse`` before the
    # security scrubber under test ever runs.
    artifact_str = artifact.as_posix()
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "import os",
                f"os.system('touch {artifact_str}')",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Executable top-level statement"):
        _load_module_from_file(signal_file, _module_name())

    assert not artifact.exists()


def test_signal_engine_rejects_class_level_execution(tmp_path) -> None:
    artifact = tmp_path / "class_level_rce"
    artifact_str = artifact.as_posix()  # see top_level test for rationale
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "import os",
                "class SignalEngine:",
                f"    os.system('touch {artifact_str}')",
                "    def generate(self, *args, **kwargs):",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Executable class-level statement"):
        _load_module_from_file(signal_file, _module_name())

    assert not artifact.exists()


def test_signal_engine_rejects_subprocess_import_in_generate(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        import subprocess",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subprocess"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_absolute_write_open_in_generate(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        with open('/tmp/backtest-rce', 'w') as output:",
                "            output.write('pwned')",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="open"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_reachable_helper_with_http_client(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "def fetch_remote_signal():",
                "    import requests",
                "    return requests.get('https://example.test/signal')",
                "",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        fetch_remote_signal()",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requests"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_restricted_os_import_alias(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        import os as operating_system",
                "        return operating_system.system('id')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="operating_system.system"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_module_level_restricted_os_alias(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "import os as operating_system",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return operating_system.system('id')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="operating_system.system"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_dangerous_module_level_from_import(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "from subprocess import Popen as harmless",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return harmless(['echo', 'unsafe'])",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Import from 'subprocess'"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_sys_modules_indirection(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "import sys",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return sys.modules['os'].system('id')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sys"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_helper_called_through_local_alias(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "def fetch_remote_signal():",
                "    import requests",
                "    return requests.get('https://example.test/signal')",
                "",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        callback = fetch_remote_signal",
                "        callback()",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requests"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_helper_called_through_partial(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "def fetch_remote_signal():",
                "    import requests",
                "    return requests.get('https://example.test/signal')",
                "",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        import functools",
                "        callback = functools.partial(fetch_remote_signal)",
                "        callback()",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="functools.partial|requests"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_pathlib_write_in_generate(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return Path('/tmp/backtest-rce').write_text('pwned')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="write_text"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_dynamic_import_indirection(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return getattr(__builtins__, '__import__')('os').system('id')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="getattr|__builtins__"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_pathlib_secret_read(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return Path('/home/user/.vibe-trading/qveris.json').read_text()",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="read_text"):
        _load_module_from_file(signal_file, _module_name())


def test_signal_engine_rejects_pandas_absolute_write(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                "import pandas as pd",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        return pd.DataFrame({'x': [1]}).to_csv('/tmp/backtest-rce.csv')",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="to_csv"):
        _load_module_from_file(signal_file, _module_name())


def test_main_runs_generated_engine_with_temporary_loader_home(monkeypatch, tmp_path) -> None:
    real_home = tmp_path / "real-home"
    (real_home / ".vibe-trading" / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("USERPROFILE", str(real_home))

    run_dir = real_home / ".vibe-trading" / "runs" / "run"
    signal_dir = run_dir / "code"
    signal_dir.mkdir(parents=True)
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "codes": ["000001.SZ"],
                "start_date": "2025-01-01",
                "end_date": "2025-01-02",
                "source": "tushare",
            }
        ),
        encoding="utf-8",
    )
    (signal_dir / "signal_engine.py").write_text(
        "\n".join(
            [
                "from os.path import expanduser",
                "from pathlib import Path",
                "class SignalEngine:",
                "    def generate(self, *args, **kwargs):",
                "        home = expanduser('~')",
                "        return {",
                "            'home': home,",
                "            'has_vibe_directory': (Path(home) / '.vibe-trading').exists(),",
                "        }",
            ]
        ),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class Loader:
        def fetch(self, *args, **kwargs):
            return {"000001.SZ": object()}

    class Engine:
        def run_backtest(self, config, loader, signal_engine, run_dir, bars_per_year):
            captured.update(signal_engine.generate({}))

    swarm_store = ModuleType("src.swarm.store")
    swarm_store.swarm_runs_root = lambda: tmp_path / "swarm-runs"
    monkeypatch.setitem(sys.modules, "src.swarm.store", swarm_store)
    monkeypatch.setattr(runner, "_get_loader", lambda source: Loader)
    monkeypatch.setattr(runner, "_sanitize_data_map", lambda data_map: data_map)
    monkeypatch.setattr(runner, "_create_market_engine", lambda *args: Engine())

    runner.main(run_dir)

    sandbox_home = Path(str(captured["home"]))
    assert sandbox_home != real_home
    assert captured["has_vibe_directory"] is False
    assert not sandbox_home.exists()
    assert Path.home() == real_home


def test_signal_engine_allows_minimal_valid_strategy(tmp_path) -> None:
    signal_file = tmp_path / "signal_engine.py"
    signal_file.write_text(
        "\n".join(
            [
                '"""Generated signal engine."""',
                "THRESHOLD = 3",
                "class SignalEngine:",
                "    lookback = 20",
                "    def generate(self, *args, **kwargs):",
                "        return []",
            ]
        ),
        encoding="utf-8",
    )

    module = _load_module_from_file(signal_file, _module_name())

    assert module.SignalEngine().generate() == []
