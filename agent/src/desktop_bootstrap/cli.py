"""`vibe-trading bootstrap` 渲染:默认人类可读,--sse 输出 SSE 帧给 Rust 控制台。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.desktop_bootstrap.bootstrap import run_bootstrap


def run_bootstrap_cli(argv: Optional[list[str]] = None) -> int:
    """Entry for the ``bootstrap`` subcommand. Returns process exit code."""
    import argparse
    from src.config.paths import get_runtime_root
    from src.optional_deps.mirror import (
        MirrorConfig,
        load_mirror_config,
        resolve_trusted_host,
        save_mirror_config,
    )
    from src.optional_deps.sse_lines import sse_event, stage_line

    parser = argparse.ArgumentParser(prog="vibe-trading bootstrap")
    parser.add_argument("--sse", action="store_true", help="Emit SSE frames (desktop console)")
    parser.add_argument("--mirror", default=None, help="tsinghua|aliyun|official|custom|off")
    parser.add_argument("--index-url", default=None, help="Custom index url (with --mirror custom)")
    args = parser.parse_args(argv)

    home = get_runtime_root()
    requirements = Path(__file__).resolve().parents[2] / "requirements.txt"
    # 桌面 venv 排除 weasyprint(需要 native cairo/pango,不可靠跨平台)。
    # 复制临时文件剔除 weasyprint 行,不修改原 requirements.txt。
    # ponytail: 与 install-deps.sh 一致。
    import tempfile

    _lines = requirements.read_text(encoding="utf-8").splitlines()
    _filtered = [ln for ln in _lines if "weasyprint" not in ln]
    if len(_filtered) != len(_lines):
        _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        _tmp.write("\n".join(_filtered))
        _tmp.close()
        requirements = Path(_tmp.name)
    # Mirror: CLI arg > persisted config > default tsinghua. An explicit
    # --index-url is a verbatim override; everything else flows through the
    # persisted config so run_bootstrap keeps its fallback chain
    # (selected mirror -> aliyun). Pre-resolving the URL here used to
    # collapse the chain to a single mirror with no fallback.
    cfg = load_mirror_config()
    if args.mirror:
        cfg = MirrorConfig(name=args.mirror, custom_index_url=args.index_url or "")
        save_mirror_config(cfg)
    if args.index_url:
        index_url = args.index_url
        trusted = resolve_trusted_host(MirrorConfig(name="custom", custom_index_url=args.index_url))
    else:
        index_url = ""
        trusted = ""
    log_path = home / "logs" / "bootstrap.log"

    exit_code = 0
    for ev in run_bootstrap(
        home_vibe=home,
        requirements=requirements,
        index_url=index_url,
        trusted_host=trusted,
        log_path=log_path,
    ):
        if args.sse:
            if ev.stage in ("done", "failed"):
                print(sse_event(ev.stage, {"ok": ev.ok, "message": ev.message}), end="", flush=True)
            else:
                print(stage_line(ev.stage, ev.message), end="", flush=True)
        else:
            print(f"[{ev.stage}] {ev.message}", flush=True)
        if ev.stage == "failed":
            exit_code = 1
    return exit_code
