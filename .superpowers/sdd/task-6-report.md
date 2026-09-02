# Task 6 Report: 工具 manifest 转换（Python registry → sidecar manifest）

## Status: DONE

## What was implemented

`agent/src/pi_sidecar/manifest.py`（Create）+ `agent/tests/pi_sidecar/test_manifest.py`（Create），严格按 brief 逐字实现：

- `build_manifest_tool(tool: BaseTool) -> dict[str, Any]` — 将单个 `BaseTool` 转为 ManifestTool dict：`{name, description, parameters (JSON Schema), is_readonly, side_effecting, repeatable, timeout_seconds}`。
- `build_tool_manifest(registry: ToolRegistry) -> list[dict]` — 遍历 registry 全部工具（`registry._tools.values()`，同包受控访问，`# noqa: SLF001`），保插入序。
- 强制 side-effecting 分类与 `ToolGateway` 对齐（recon §3.3，已验证 `agent/src/reliability/gateway.py:65,70`）：`_FORCED_SIDE_EFFECTING_NAMES = frozenset({"bash"})` + `_FORCED_SIDE_EFFECTING_PREFIXES = ("trading_",)`；被强制分类的工具 `is_readonly` 强制为 False（`bool(tool.is_readonly) and not side_effecting`）。
- `TOOL_TIMEOUT_SECONDS = float(os.getenv("VIBE_TRADING_TOOL_TIMEOUT_SECONDS", "1800"))` — 与 `agent/src/agent/loop.py:60` 同源同默认（已验证）。
- 空 `parameters` 回退到 `{"type": "object", "properties": {}, "required": []}`（BashTool 无 parameters 时用共享 `_EMPTY_OBJECT_SCHEMA`）。

## TDD evidence

**RED** — `pytest agent/tests/pi_sidecar/test_manifest.py --tb=short -q`（实现前）：
```
agent/tests/pi_sidecar/test_manifest.py:6: in <module>
    from src.pi_sidecar.manifest import build_manifest_tool, build_tool_manifest
E   ModuleNotFoundError: No module named 'src.pi_sidecar.manifest'
=========================== short test summary info ============================
ERROR agent/tests/pi_sidecar/test_manifest.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.64s
```

**GREEN** — 实现后同一命令：
```
6 passed in 0.31s
```

**Lint** — `ruff check agent/src/pi_sidecar agent/tests/pi_sidecar`：`All checks passed!`

**Full scoped dir**（确认无跨测试干扰）— `pytest agent/tests/pi_sidecar -q`：`23 passed`（17 前序 protocol/engine + 6 新 manifest）。

## Files changed

- `agent/src/pi_sidecar/manifest.py`（新增，37 行）
- `agent/tests/pi_sidecar/test_manifest.py`（新增，85 行）

## Commit

- `6cf11b76` feat(pi-sidecar): tool manifest conversion from registry（`git commit -s`，DCO Signed-off-by 已确认，Conventional Commits subject，无 AI attribution trailer）

## Deviations from plan

无。brief 代码逐字落地，未做任何改动；实现前核对的三处事实（gateway 强制名单/前缀、loop.py 超时常量、`src.*` 导入经 `agent/tests/conftest.py` sys.path 注入在仓库根可跑）均与 brief 声明一致。

## Self-review findings

- **完整性**：brief Steps 1–6 全部执行；Files 列表严格限定两个新文件，未触碰 `agent/src/live/` 与 legacy AgentLoop。
- **测试真实性**：测试构造真实 `BaseTool` 子类（类属性声明 metadata）并经真实 `ToolRegistry.register` 走 `build_tool_manifest`，无 mock——断言直接验证转换逻辑本身。`test_bash_is_forced_side_effecting` 覆盖了"恶意/错误标注"场景（`is_readonly=True` 的 bash 被强制 side-effecting 且 `is_readonly` 翻转为 False），这正是 gateway 语义一致性的关键守卫。
- **YAGNI**：未加任何超 brief 的辅助函数/常量；`_EMPTY_OBJECT_SCHEMA` 为模块级共享（测试 `==` 断言，不要求同一对象）。
- **边界**：`test_readonly_tool_fields` 断言 `parameters` 传引用相等——`tool.parameters or _EMPTY_OBJECT_SCHEMA` 对非空 dict 返回原对象，通过。
- 一处既有差异（非本任务）：`.superpowers/sdd/task-1..4-report.md` 的 M 状态来自前序任务，未纳入本 commit。

## Concerns

无。注意点（非问题）：`TOOL_TIMEOUT_SECONDS` 与 `TOOL_TIMEOUT_SECONDS`（loop.py）都是 import 时读 env——若运行时设置 `VIBE_TRADING_TOOL_TIMEOUT_SECONDS` 需在模块 import 前生效；这与既有 loop.py 行为一致，按 brief 保留。测试文件自带 `TOOL_TIMEOUT_SECONDS = 1800.0` 硬编码默认值（brief 原文如此），在设置了该 env 的 CI 环境中可能失败——因 brief 要求逐字，未改动。
