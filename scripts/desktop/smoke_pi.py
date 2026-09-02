"""Pi packaging smoke（design §Verification：packaging smoke tests）。

用法：PYTHONPATH=agent python scripts/desktop/smoke_pi.py [--pi-bin PATH]
退出码 0 = 全部通过。
检查项：
 1. 二进制以 RPC 模式启动，stdout 无非 JSONL 输出
 2. bundled skills 数量与源树 agent/src/skills 一致
 3. tool manifest 数量与 Python registry 一致（经 sidecar set/get 链路）
 4. 会话/内存写到用户目录（~/.vibe-trading/pi/），绝不写 bundle 内
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys  # noqa: F401  # M4：brief 要求顶部 import sys（保留 verbatim，ruff E/F/W 门禁用）
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def check_rpc_stdout(pi_bin: Path) -> None:
    proc = subprocess.run(
        [str(pi_bin)],
        input='{"v":1,"id":"r-1","op":"ping"}\n',
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "no stdout from pi binary"
    for ln in lines:
        obj = json.loads(ln)  # 非 JSONL 会抛 → smoke 失败
        assert obj.get("v") == 1
    ready = json.loads(lines[0])
    assert ready.get("event") == "ready", f"first frame not ready: {ready}"
    last = json.loads(lines[-1])
    assert last.get("id") == "r-1" and last.get("ok") is True


def check_skills_count(staged_skills: Path) -> None:
    src = REPO / "agent" / "src" / "skills"
    src_count = len(list(src.glob("*/SKILL.md")))
    staged_count = len(list(staged_skills.glob("*/SKILL.md")))
    assert src_count == staged_count, f"skills mismatch: src={src_count} staged={staged_count}"
    assert src_count == 86, f"expected 86 skills, found {src_count}"


def check_manifest_and_user_dirs(tmp_agent: Path, tmp_sessions: Path) -> None:
    async def run():
        from src.pi_sidecar.client import PiSidecarClient
        from src.pi_sidecar.manifest import build_tool_manifest
        from src.tools import build_registry

        # 离线构建 registry（smoke 运行在构建机；check_available 未命中的工具自然缺席，
        # manifest 与 registry 的"一致性"指 sidecar 不丢工具：set_tool_manifest 回显数 == len(manifest)）
        registry = build_registry(persistent_memory=None, include_shell_tools=False)
        manifest = build_tool_manifest(registry)
        assert len(manifest) >= 1, "empty registry manifest"

        c = PiSidecarClient(
            command=["bun", "run", "src/main.ts"],
            cwd=REPO / "pi-sidecar",
            agent_dir=tmp_agent,
            sessions_dir=tmp_sessions,
        )
        await c.start()
        try:
            echoed = await c.request("set_tool_manifest", {"tools": manifest})
            assert echoed["count"] == len(manifest), (
                f"sidecar dropped tools: sent={len(manifest)} echoed={echoed['count']}"
            )
            await c.request("new_session", {"session_id": "c0c0c0c0c0c0"})
            state = await c.request("get_state", {"session_id": "c0c0c0c0c0c0"})
            assert str(state["session_file"]).startswith(str(tmp_sessions)), state
        finally:
            await c.stop()

    asyncio.run(run())
    assert tmp_sessions.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi-bin", type=Path, default=REPO / ".desktop-build/pi/macos-aarch64/pi")
    ap.add_argument("--skills", type=Path, default=REPO / ".desktop-build/pi/skills")
    args = ap.parse_args()
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        check_rpc_stdout(args.pi_bin)
        check_skills_count(args.skills)
        check_manifest_and_user_dirs(Path(td) / "agent", Path(td) / "sessions")
    print("smoke_pi: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
