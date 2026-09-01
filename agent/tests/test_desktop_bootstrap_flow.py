"""bootstrap 编排 —— 决策流可测:建 venv → 装 → 冒烟 → 就绪 / 失败 / 跳过。"""

from src.desktop_bootstrap.smoke import SmokeResult


def _drain(events):
    return [(e.stage, e.ok) for e in events]


def test_happy_path_installs_then_ready(tmp_path):
    from src.desktop_bootstrap.bootstrap import run_bootstrap

    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")
    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=lambda **kw: iter(["Collecting numpy", "Successfully installed numpy"]),
            smoke=lambda py: SmokeResult(ok=True),
        )
    )
    stages = _drain(events)
    assert ("installing", True) in stages
    assert stages[-1] == ("done", True)


def test_smoke_failure_marks_not_ready(tmp_path):
    from src.desktop_bootstrap.bootstrap import run_bootstrap

    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")
    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=lambda **kw: iter(["Successfully installed numpy"]),
            smoke=lambda py: SmokeResult(ok=False, failures=["scipy: no wheel"]),
        )
    )
    stages = _drain(events)
    assert stages[-1] == ("failed", False)


def test_pip_failure_yields_failed_with_reason(tmp_path):
    import subprocess
    from src.desktop_bootstrap.bootstrap import run_bootstrap

    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")

    def boom(**kw):
        yield "Collecting numpy"
        raise subprocess.CalledProcessError(1, ["pip"])

    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=boom,
            smoke=lambda py: SmokeResult(ok=True),
        )
    )
    stages = _drain(events)
    assert stages[-1][0] == "failed"


def test_already_ready_skips_install(tmp_path):
    from src.desktop_bootstrap.requirements_hash import compute_hash, write_marker
    from src.desktop_bootstrap.bootstrap import hash_marker_path, run_bootstrap

    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")
    (home / "venv" / "bin").mkdir(parents=True)
    (home / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    write_marker(hash_marker_path(home), compute_hash(req))
    calls = {"pip": 0}

    def counting_pip(**kw):
        calls["pip"] += 1
        return iter([])

    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=counting_pip,
            smoke=lambda py: SmokeResult(ok=True),
        )
    )
    assert calls["pip"] == 0
    assert _drain(events)[-1] == ("done", True)


def test_cli_sse_flag_emits_frames(tmp_path, monkeypatch, capsys):
    import src.desktop_bootstrap.cli as cli_mod
    from src.desktop_bootstrap.bootstrap import BootstrapEvent

    monkeypatch.setattr(
        cli_mod,
        "run_bootstrap",
        lambda **kw: iter([BootstrapEvent("installing", "x"), BootstrapEvent("done", "ready")]),
    )
    monkeypatch.setattr("src.config.paths.get_runtime_root", lambda: tmp_path)
    rc = cli_mod.run_bootstrap_cli(["--sse"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "installing" in out.lower() or "done" in out.lower()


def test_falls_back_to_aliyun_when_tsinghua_broken(tmp_path, monkeypatch):
    from src.desktop_bootstrap.bootstrap import run_bootstrap, _mirror_sequence
    from src.optional_deps.mirror import MIRROR_URLS, MirrorConfig

    # No persisted config -> default chain is tsinghua then aliyun.
    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")
    assert [u for u in _mirror_sequence(MirrorConfig())] == [MIRROR_URLS["tsinghua"], MIRROR_URLS["aliyun"]]

    tried: list[str] = []

    def pip_stream(python, requirements, index_url, trusted_host):
        tried.append(index_url)
        if index_url == MIRROR_URLS["tsinghua"]:
            import subprocess

            raise subprocess.CalledProcessError(1, ["pip"])
        yield "Successfully installed numpy"

    cfg_path = home / "runtime" / "optional_deps_mirror.json"
    monkeypatch.setattr(
        "src.optional_deps.mirror.default_config_path",
        lambda: cfg_path,
    )
    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=pip_stream,
            smoke=lambda py: SmokeResult(ok=True),
        )
    )
    assert tried == [MIRROR_URLS["tsinghua"], MIRROR_URLS["aliyun"]], tried
    assert events[-1].stage == "done" and events[-1].ok
    # The working mirror is persisted as the new default.
    saved = MirrorConfig(**json.loads(cfg_path.read_text(encoding="utf-8")))
    assert saved.name == "aliyun"


def test_all_mirrors_failed_yields_failed(tmp_path):
    from src.desktop_bootstrap.bootstrap import run_bootstrap
    import subprocess

    home = tmp_path / ".vibe-trading"
    req = tmp_path / "requirements.txt"
    req.write_text("numpy\n", encoding="utf-8")

    def boom(python, requirements, index_url, trusted_host):
        yield "Collecting numpy"
        raise subprocess.CalledProcessError(1, ["pip"])

    events = list(
        run_bootstrap(
            home_vibe=home,
            requirements=req,
            ensure_venv=lambda h: h / "venv" / "bin" / "python",
            pip_stream=boom,
            smoke=lambda py: SmokeResult(ok=True),
        )
    )
    assert events[-1].stage == "failed"


def test_custom_mirror_no_fallback(tmp_path):
    from src.desktop_bootstrap.bootstrap import _mirror_sequence
    from src.optional_deps.mirror import MirrorConfig

    assert _mirror_sequence(MirrorConfig(name="custom", custom_index_url="https://x/simple")) == ["https://x/simple"]
    assert _mirror_sequence(MirrorConfig(name="off")) == [""]


import json  # noqa: E402  (used by the fallback persistence test above)


def _capture_cli_run(tmp_path, monkeypatch, argv):
    """Run the CLI entry with run_bootstrap stubbed; return captured kwargs."""
    import src.desktop_bootstrap.cli as cli_mod
    from src.desktop_bootstrap.bootstrap import BootstrapEvent

    captured = {}

    def fake_run_bootstrap(**kw):
        captured.update(kw)
        return iter([BootstrapEvent("done", "ready")])

    monkeypatch.setattr(cli_mod, "run_bootstrap", fake_run_bootstrap)
    monkeypatch.setattr("src.config.paths.get_runtime_root", lambda: tmp_path)
    rc = cli_mod.run_bootstrap_cli(argv)
    return captured, rc


def test_cli_default_keeps_fallback_chain(tmp_path, monkeypatch):
    """No explicit --index-url: the CLI must NOT pre-resolve the mirror URL.

    run_bootstrap treats a non-empty index_url as a verbatim override with
    no fallback. Pre-resolving the persisted/default mirror here used to
    collapse the chain to a single tsinghua entry, so any client whose
    network could not reach tuna hard-failed instead of falling back to
    aliyun.
    """
    cfg_path = tmp_path / ".vibe-trading" / "runtime" / "optional_deps_mirror.json"
    monkeypatch.setattr("src.optional_deps.mirror.default_config_path", lambda: cfg_path)
    captured, rc = _capture_cli_run(tmp_path, monkeypatch, [])
    assert captured["index_url"] == ""
    assert rc == 0


def test_cli_explicit_index_url_is_verbatim(tmp_path, monkeypatch):
    captured, _ = _capture_cli_run(tmp_path, monkeypatch, ["--index-url", "https://m.example/simple"])
    assert captured["index_url"] == "https://m.example/simple"


def test_cli_mirror_flag_persists_selection(tmp_path, monkeypatch):
    cfg_path = tmp_path / ".vibe-trading" / "runtime" / "optional_deps_mirror.json"
    monkeypatch.setattr("src.optional_deps.mirror.default_config_path", lambda: cfg_path)
    captured, _ = _capture_cli_run(tmp_path, monkeypatch, ["--mirror", "aliyun"])
    assert captured["index_url"] == ""
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["name"] == "aliyun"
