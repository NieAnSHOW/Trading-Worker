# Task 16 Report — Fake provider + 基础集成（多轮/工具/并行只读串行写）

Status: DONE_WITH_CONCERNS（一个生产侧工具 manifest 时序缺陷，见 Concerns）

## What I implemented

Brief 的两个文件 + 集成测试暴露的四类 sidecar 接线缺口（in-place 修复，无新文件）：

1. `agent/tests/pi_sidecar/fake_provider.py`（Create，brief 代码 + 一处构造修复）
2. `agent/tests/pi_sidecar/test_integration_basic.py`（Create，brief 代码 + 三处修正，见 Deviations）
3. `pi-sidecar/src/session-driver.ts`（in-place 修复：isStreaming getter、restrictToolNames、B6 agentDir env）
4. `pi-sidecar/src/session-index.ts`（in-place 修复：B6 sessionsDir/indexPath env）
5. `pi-sidecar/src/main.ts`（in-place 修复：diagnostic 帧去掉空 session_id）

## TDD evidence

**RED 1（isStreaming 契约漂移）** — `pytest agent/tests/pi_sidecar/test_integration_basic.py --tb=short -q`：
```
E   src.pi_sidecar.client.SidecarError: internal: this.s.isStreaming is not a function.
    (In 'this.s.isStreaming?.()', 'this.s.isStreaming' is false)
2 failed in 12.55s
```
pinned SDK 18.0.11 的 `AgentSession.isStreaming` 是 **getter（boolean）**（agent-session.d.ts:270），
非 plan 假设的函数签名。修复：`isBusy()` 双形态兼容（`typeof v === "function" ? v() : Boolean(v)`）。

**RED 2（模型解析失败）** — 修复 isStreaming 后：
```
assert '' == 'first answer'
2 failed in 22.52s
```
模型从未被调用（provider.requests=0）。探针定位：`session.model` undefined →
prompt 报 "No model selected"。根因（SDK 18.0.11 实证，逐层验证）：
- registry 只认真实目录模型 id：`find("openai","fake-model")` → undefined，
  `find("openai","gpt-4o-mini")` → 命中。modelPattern 必须命中目录模型。
- openai provider 的全部 54 个目录模型 api 均为 **openai-responses**（`/v1/responses`），
  chat.completions stub 无法服务任何 openai 目录模型。
- vibe-providers extension 的 `registerProvider`（deepseek 等，api: openai-completions +
  baseUrl 覆盖）是 plan 自己的 model wiring 路径——但 **restrictToolNames=true 时 SDK
  完全不装载 extensions**（会话装配 `if (Tn) Dp = loadExtensions([])`，Tn=e.restrictToolNames===true），
  生产 vibe-providers/vibe-memory 实际从未运行。

修复：
- `restrictToolNames: false`（工具禁用由 `toolNames: []` 单独保证，实测 `_tools` 为空；
  修正后 vibe-providers 注册生效、请求打到本地 stub、memory extension 开始注入）。
- 测试 env 改用 deepseek 注册路径（见 Deviations #3）。

**RED 3（fake provider 构造 bug）** — brief 原文 `ThreadingHTTPServer(("127.0.0.1", 0), self._handler)`
把方法对象当 handler 类传入 → 服务器按 `(request, client_address, server)` 调用 →
`TypeError: FakeOpenAIProvider._handler() takes 1 positional argument but 4 were given`，
turn 挂到 _wait_terminal 超时。修复：`self._handler()`。

**GREEN** — `pytest "agent/tests/pi_sidecar/test_integration_basic.py" --tb=short -q`：
```
2 passed in 2.84s
```
输出零 warning/error/exception（`grep -iE "warning|error|exception"` 空）。

**自我评审负向验证**（断言有效性）：把 test 2 改回旧时序（new_session 后才 set_tool_manifest）→
新断言立即失败：
```
1 failed, 1 passed in 2.71s   # tool_invokes == ["doubler"]，实际 []
```

**全套**：`pytest agent/tests/pi_sidecar -q` → `82 passed in 6.42s`；
`cd pi-sidecar && bun test` → `65 pass, 0 fail`；
`ruff check agent/tests/pi_sidecar/fake_provider.py agent/tests/pi_sidecar/test_integration_basic.py` → clean
（仓库整体 `ruff check agent/src agent/tests` 的 291 个错误为既有基线，全部在未触碰的旧文件）。

## Files changed (commits)

- `e1ca82ba` fix(pi-sidecar): align session wiring with pinned SDK 18.0.11
  （session-driver.ts / session-index.ts / main.ts）
- `0c64669c` test(pi-sidecar): fake provider + basic sidecar integration（brief 指定提交信息）
- `5f19b66d` test(pi-sidecar): bind tool manifest before session creation
- `790f2f85` test(pi-sidecar): assert tool result value roundtrips through gateway

## Deviations from the plan (with reasons)

1. **`isStreaming` 是 getter 非函数**（session-driver.ts）— SDK 18.0.11 类型实证
   （agent-session.d.ts:270），plan 签名假设错误。双形态兼容修复，非破坏性。
2. **`restrictToolNames: true` 在 18.0.11 下禁用全部 extensions**（session-driver.ts）—
   这是集成测试暴露的最大接线缺口：vibe-providers（provider 注册）与 vibe-memory 在生产也从未
   运行。`toolNames: []` 单独即强制工具禁用（实测会话 `_tools` 为空、read/write/edit/bash
   均未注册）；restrictToolNames 改为 false 恢复 extension 装载。设计意图（Global Constraint:
   model wiring + 每轮 memory 注入）原本就要求 extension 生效。
3. **测试 env 从 openai 改为 deepseek 注册路径**（test_integration_basic.py）— brief 的
   `OPENAI_BASE_URL/OPENAI_API_KEY/LANGCHAIN_PROVIDER=openai/LANGCHAIN_MODEL_NAME=fake-model`
   无法工作（SDK 实证）：openai 目录模型全部 openai-responses api + "fake-model" 非目录 id。
   改走 plan §model wiring 正式路径：`DEEPSEEK_BASE_URL`（vibe-providers env 表注册 deepseek，
   api openai-completions，baseUrl 覆盖到 stub）+ `DEEPSEEK_API_KEY=test` +
   `LANGCHAIN_PROVIDER=deepseek` + `LANGCHAIN_MODEL_NAME=deepseek-v4-flash`（目录内真实
   openai-completions id）。这正是任务上下文所述 "the fake provider must be reachable under
   one of those registrations"。
4. **B6 目录 env 未被 sidecar 消费**（session-index.ts + session-driver.ts）— client._spawn
   注入 `VIBE_PI_AGENT_DIR/VIBE_PI_SESSIONS_DIR` 但 sidecar 用常量 `~/.vibe-trading/pi`，
   测试会写真实用户状态（集成测试初次运行时已产生污染，随后清理）。现按 env 解析
   （生产未设 env → 常量，行为不变）；测试断言 session_file 落在 tmp 之下。
5. **测试时序修正**（test_integration_basic.py）— SDK 在 createAgentSession 时快照
   customTools；new_session 之后 set_tool_manifest 不会绑定（实测 "Tool doubler not found"）。
   manifest 必须先于 new_session。生产 service.py 首 attempt 有同样缺陷（见 Concerns）。
6. **断言强化**（test_integration_basic.py）— brief 原断言在工具调用失败时也会通过
   （"Tool doubler not found" 后模型仍拿到脚本化第二轮响应）。新增：`tool_invokes ==
   ["doubler"]`（tool_invoke RPC 到达 Python）、tool_execution_end isError=False、
   解析 gateway StepResult 信封断言实际计算值 42 回灌模型（session 事件 + provider 第二轮
   tool 消息双处）。
7. **fake_provider 构造 bug**（fake_provider.py）— `self._handler` → `self._handler()`。
8. **main.ts diagnostic 帧**（main.ts）— `emitDiagnostic` 发 `session_id: ""` 违反 client
   协议（非空字符串要求）被拒帧；改为省略该键。

## Self-review findings

- **测试验证真实行为**：真实 `bun run src/main.ts` + 真实 SDK 18.0.11 + 真实 SSE stub +
  工具经真实 `ToolGateway`→`Doubler` 执行；负向验证证明断言有牙齿（旧时序必然失败）。
- **Hermetic**：localhost stub 专用端口、`test` 假 key、进程/服务器 finally 清理、
  `VIBE_PI_*` 目录 env 隔离（断言强制）。实测真实 `~/.vibe-trading/pi/` 不再被写入
  （session-index.json 不存在、sessions 恢复 11 个）；调试期产生的 2 个 stray JSONL、
  session-index.json、/tmp pi-probe-* 已清理。
- **无 scope creep**：未触碰 agent/src/live/、未改 SessionStore 语义；sidecar 改动均为
  测试暴露的接线缺口（brief 实现者注明确授权 in-place 修复）。
- **YAGNI**：未新增任何抽象；两处 env helper 直接落在既有模块。
- 首次 RED 之前（修复 B6 前）的调试运行曾把测试会话写入真实 `~/.vibe-trading/pi/`，
  已恢复原状；`agent/models.db`（SDK 模型目录缓存）mtime 曾被动过，内容为标准目录数据。

## Concerns

- **生产首 attempt 工具失效（service.py）**：`agent/src/session/service.py` 在
  open/new_session 之后、prompt 之前才发一次 `set_tool_manifest`（`_manifest_sent` 每进程一次）。
  由于 SDK 在会话创建时快照 customTools，**每个进程的第一个 Pi attempt 的工具调用全部失败**
  （"Tool doubler not found"）。`test_service_cutover.py:130` 还固化了这个错误时序
  （`ops[:3] == ["open_session", "new_session", "set_tool_manifest"]`）。修复建议：把
  set_tool_manifest 块移到会话建立之前（保持 `_manifest_sent` gate），并同步更新该断言。
  本任务 Files 范围不含 service.py，未改动，交由主 agent 决策（建议 follow-up 或并入 Task 17）。
- **extension 异步装载与首帧 new_session 的竞态**：dev 模式下 vibe-providers 经动态 import
  装载（Task 15 已接受该竞态），实测多次运行均稳定；若将来 flaky，可改为 main.ts 启动时
  await 装载完成再发 ready。
- **restrictToolNames=false 激活 memory extension 的生产影响**：vibe-memory 现在真正运行
  （每轮 memory_context host 调用 + remember/memory_search/memory_remove 工具注册）。
  这是设计意图（此前静默失效），但属于生产行为变化，需随 Task 14 验证一起确认。

## Validation performed (exactly)

- `pytest "agent/tests/pi_sidecar/test_integration_basic.py" --tb=short -q` → 2 passed，输出 pristine
- `pytest agent/tests/pi_sidecar -q` → 82 passed
- `cd pi-sidecar && bun test` → 65 pass / 0 fail
- `ruff check`（两个新文件）→ clean
- 负向验证（旧时序）→ 断言按预期失败
- 真实目录污染检查（session-index.json 不存在、sessions=11 恢复原状）

## Fix: manifest-before-session ordering

Task 16 Concern 落地（生产侧工具 manifest 时序缺陷，`agent/src/session/service.py` + `agent/tests/pi_sidecar/test_service_cutover.py`）：

- `service.py::_run_with_pi`：把 `set_tool_manifest` 块从 open/new_session **之后**移到 **之前**（新 section 2a），保证任何 `createAgentSession` 之前 client 已持有 manifest。保持每进程幂等 `_manifest_sent` gate；manifest push 失败落在既有 except → 映射为明确协议错误（reason=SidecarError code），绝不静默以空工具继续。
- `test_service_cutover.py:130`：断言更新为 `ops[:3] == ["set_tool_manifest", "open_session", "new_session"]`，固化"manifest 先于会话建立"。

验证（exactly）：
- `pytest agent/tests/pi_sidecar -q` → 82 passed
- `pytest agent/tests/test_sdk_order_gate.py agent/tests/test_mandate_enforcement.py -q` → 48 passed（safety narrow）
- `ruff check src/session/service.py tests/pi_sidecar/test_service_cutover.py` → clean（仓库全量 223 错误为既有基线，均在未触碰文件）
- 注：brief 的 `tests/test_session_service.py` 在本分支不存在（cutover 单测即 pi_sidecar/test_service_cutover.py，已含于 pi_sidecar 套件）

## Fix: builtin tool lockdown restored (runtime-verified)

Security finding (e1ca82ba flipped `restrictToolNames` false, dropping the plan-mandated builtin-disable gate). Runtime probe against pinned SDK 18.0.11 proved `toolNames:[]` alone does NOT disable builtins — a builtin WRITE was resolvable via `getToolByName` (the session's `ensureWriteRegistered` leak, sdk.ts, fires when `plan.enabled` is on). `restrictToolNames:true` WOULD close it but also skips loading the inline extensions (vibe-providers/vibe-memory) — breaking production model wiring. So neither the original combo nor the commit's combo is usable.

### Options landed (`pi-sidecar/src/session-driver.ts::buildSession`)
- `restrictToolNames: false` — kept (required so vibe-providers/vibe-memory inline extensions load; model wiring intact)
- `toolNames: []` — kept (createTools with empty list ⇒ zero builtins)
- `Settings.isolated({ "plan.enabled": false })` — NEW; neutralizes the sole remaining builtin-leak path (`ensureWriteRegistered`), which is driven by `planModeAvailable` (MCP is already off, no deferrable tools)

### Probe evidence (three candidate combos, pinned SDK, fake manifest custom tool + spy inline extension)
| combo | builtin write/read/edit/bash | custom manifest tool | inline extension invoked |
|---|---|---|---|
| A: restrict=true + allowCustom + toolNames:[] | none | present | **false** (model wiring breaks) |
| B: current commit (restrict=false + toolNames:[]) | **write present** | present | true |
| C: restrict=false + toolNames:[] + plan.enabled=false | **none** | present | **true** ✓ |

Combo C landed. Runtime probe of the landed code: `getToolByName("read"/"write"/"edit"/"bash")` all undefined; registered manifest tool present.

### Tests added
- `pi-sidecar/src/session-driver.test.ts` → "builtin write/edit/bash are absent and manifest custom tool is present in session tools": builds a driver whose toolBridge returns a fake manifest tool, `driver.create(...)`, asserts `getToolByName` for read/write/edit/bash is undefined and the custom tool is defined. (Regression guard the suite lacked.)

### Validation
- `cd pi-sidecar && bun test` → 66 pass / 0 fail
- `pytest agent/tests/pi_sidecar -q` → 82 passed
