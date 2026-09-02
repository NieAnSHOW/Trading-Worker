# Task 7 Report: Sidecar Tool Bridge（manifest 工具注册 + `tool_invoke` RPC 回 Python）

Status: DONE_WITH_CONCERNS（1 条 Task 5b 接线观察，非本任务缺陷）

Commit: `481223e3` feat(pi-sidecar): tool bridge routing executions to python host

## 实现内容

按 brief step 1-6 完成，三个文件：

1. **`pi-sidecar/src/host-rpc.ts`**（Create，49 行）— brief step 3 逐字实现：`HostRpc` 类，`seq` 自增 id 关联、`pending` Map、`call(op, params, timeoutMs=60_000)` 超时 reject（`code:"timeout"`）、`onFrame` 按 id 派发响应、`rejectAll`。无改动。

2. **`pi-sidecar/src/tool-bridge.ts`**（Create，141 行）— brief step 4 实现 + 两处经 d.ts 核验的偏差处理（见下）：
   - `ToolManifestEntry` 接口逐字；
   - `setManifest` 返回 manifest 长度；`customTools()` 每项 `{name, label??name, description, parameters, execute}`；
   - execute 构造 `tool_invoke` 参数（`call_id`/`toolCallId`/`toolName`/`arguments`/`idempotencyKey`/`is_readonly`/`side_effecting`/`repeatable`/`timeout_seconds`），结果 `{content:[{type:"text",text}], details:{outcome,idempotencyKey}}`，hostCall 抛错映射为 isError content（写类超时 `outcome_unknown` / 只读 `error`，side-effecting 永不重试——不重发即满足）；
   - 2×`timeout_seconds` 兜底计时器（`Math.max(timeout_seconds*2000, 3_600_000)`），触发 abort controller；`cancel(callId)` abort in-flight signal；
   - **M7a 幂等键回退**（见偏差 2）。

3. **`pi-sidecar/src/tool-bridge.test.ts`**（Create，101 行）— brief step 1 的 5 个用例逐字 + 1 个新增 M7a 回退用例（理由见下）。

## TDD 证据

- **RED**：`cd pi-sidecar && bun test src/tool-bridge.test.ts` →
  ```
  error: Cannot find module './tool-bridge' from '.../src/tool-bridge.test.ts'
   0 pass / 1 fail / 1 error
  ```
- **GREEN**：实现后同命令 →
  ```
   6 pass / 0 fail / 17 expect() calls
  Ran 6 tests across 1 file. [20.00ms]
  ```
- **全量回归**：`cd pi-sidecar && bun test` → `48 pass / 0 fail / 125 expect() calls`（7 文件，全部既有用例无回归）。

## 与 pinned SDK d.ts 的核验与偏差（brief 决策规则应用）

### 偏差 1（必须）：`CustomTool.execute` 实际签名 signal 在第 5 位，非第 3 位

- brief 假设：`execute(toolCallId, params, signal, onUpdate, ctx)`（测试也按此调用）。
- pinned SDK 实测：`extensibility/custom-tools/types.d.ts:218` 定义 `execute(toolCallId, params, onUpdate, ctx, signal?)`；`src/extensibility/custom-tools/wrapper.ts:37`（`CustomToolAdapter`）与 `src/sdk.ts:1005-1007`（`customToolToDefinition`）都按 **`(toolCallId, params, onUpdate, ctx, signal)`** 转发——**第 3 位是 onUpdate（函数），AbortSignal 在第 5 位**。
- brief 原样代码把第 3 位当 signal：运行时 `if (signal) signal.addEventListener(...)` 会对 onUpdate 函数调 `.addEventListener` → TypeError，每次工具执行必炸。
- **处理**：execute 形参声明为 `(toolCallId, args, onUpdateOrSignal, _ctx?, maybeSignal?)`，用 `instanceof AbortSignal` 探测第 5 位优先、第 3 位兜底；两者都不是 → `signal=null`。brief 测试传 `(id, params, null, null, {})` → 第 3 位 null、第 5 位 `{}` 均非 AbortSignal → signal=null，测试语义不变；SDK 真调用 → 第 5 位 AbortSignal 正确取到，cancel/外中止联动生效。onUpdate 回调本任务不需要（结果一次性返回），忽略即可。

### 偏差 2（Task 0 强制，M7a）：message_start 不带 entryId，幂等键走 turn 计数回退

- Task 0 report（`.superpowers/sdd/task-0-report.md:70-73`）实测：`MessageStartEvent = {type:"message_start", message: AgentMessage}`，AgentMessage 各变体均无 entry id；结论 "Task 7 MUST use M7a fallback"，且 bonus finding：`turn_end` 携带 `turnIndex`（`extensibility/shared-events.ts:212-217`，`turn_start` 同），driver 可直接用 SDK turnIndex 而非自维护计数器。
- brief 原样 step-4 代码只实现 message_start.entryId 路径（真实 SDK 永不触发 → 幂等键永远 `unknown:unknown:callId`），未实现 prose（brief 第 30 行 / plan 2354 行）要求的回退。
- **处理**：`onSessionEvent` 增加：`turn_start`/`turn_end` 的 `payload.turnIndex`（number）→ `currentTurnIndex`；execute 组键时 `assistantEntryRef = currentAssistantEntryId !== "unknown" ? currentAssistantEntryId : \`turn${currentTurnIndex}\`` → 键形如 `"<sessionId>:turn<iter>:<toolCallId>"`，与 brief prose 公式一致。entryId 路径保留（brief 测试断言 `"unknown:assist1:call-1"` 走它）。
- 回退开关：brief 描述"首个 message_start 无 entryId 即置位"的布尔探测；Task 0 判定该探测必然置位（SDK 永无 entryId），故按 Task 0 指引跳过探测、直通回退键方案（entryId 存在时仍优先用它，测试兼容）。
- **新增测试**：`"M7a fallback: idempotency key uses turn counter when SDK events carry no entry id"`——message_start 无 entryId + turn_start/turn_end turnIndex=2 → 断言 `idempotencyKey === "unknown:turn2:call-9"`。这是对 brief 强制要求行为的最小验证（brief 自带 5 用例全部逐字保留）。

## 文件变更

- `pi-sidecar/src/host-rpc.ts`（新增）
- `pi-sidecar/src/tool-bridge.ts`（新增）
- `pi-sidecar/src/tool-bridge.test.ts`（新增）

仅提交上述 3 文件；工作区其余 `M .superpowers/sdd/task-*-report.md` 为其他任务既有改动，未触碰未提交。

## 自审发现

1. **测试验证真实行为**：6 个用例全部用真实 `ToolBridge` + 注入 hostCall/deferred，无 mock 被测物；cancel 用例用不 resolve 的 deferred 验证"cancel 返回 true + 不挂死"；错误映射用例验证 hostCall 抛错 → content 含错误文本。
2. **YAGNI**：未添加 brief 之外任何功能（无 manifest 校验、无重试、无结果缓存——幂等键仅作为参数传给 Python，去重执行在 Python GatewayBridge，本任务不重复实现）。
3. **超时语义核验**：兜底计时器触发 abort 后，`cancelled = controller.signal.aborted && err.code === "timeout"` 仅在 hostCall 以 `code:"timeout"` reject 时给出 `outcome_unknown`（写类）。注意 brief 代码未给 hostCall 传 timeoutMs → 生产走 HostRpc 默认 60s（见 Concern 1）。

## Concerns（Task 5b 接线观察，非本任务缺陷）

1. **兜底计时器 vs hostCall 超时**：`execute` 调 `hostCall("tool_invoke", {...})` 未传 timeoutMs（brief 原样）。若 Task 5b 把 hostCall 接到 `hostRpc.call(op, params, timeoutMs)` 且不覆盖默认，则 60s 即 reject（`code:"timeout"`），而 2×1800s 兜底=1h——兜底计时器在超时 reject 前不会触发，`outcome_unknown` 分支在"Python 完全无响应"场景不会由兜底计时器产生（60s 先到且 controller 未 abort → `cancelled=false` → 只读/写类都返回 `error`）。Task 5b 若要让 M8 语义完整落地，需给 tool_invoke 的 hostCall 传 ≥2×timeout_seconds 的 timeoutMs，使兜底计时器先 abort 再让 transport 以 timeout reject。本任务按 brief 逐字实现，不越权改线。
2. **`onSessionEvent` 接线方**：ToolBridge 接口含 `onSessionEvent`，但当前 `session-driver.ts` 的 `ToolBridgeLike` 结构缝只有 `{customTools,setManifest,cancel}`（Task 5 交付面）。`onSessionEvent` 的调用方（driver 事件转发或 main.ts）由 Task 5b 负责接线；本任务只保证方法行为与 brief 一致。
3. **sessionId 来源**：`setSessionId`/`payload.sessionId` 两种注入点已实现，默认 `"unknown"`（brief 测试断言该默认值）。Task 5b 在 create/open 时应调 `setSessionId` 让幂等键带上真实 session。

## 结论

- 5 个 brief 用例 + 1 个 M7a 用例全绿，全量 48 pass 无回归。
- 两处偏差均经 pinned SDK d.ts/src 实证，按 brief 决策规则处理并记录（signal 第 5 位；M7a 回退直通）。
- 提交 `481223e3`，DCO Signed-off-by 已含，Conventional Commits subject，无 AI attribution trailer。

---

## Fix: HostCall export + transport abort wiring

**Review findings fixed**（feat/pi-agent-sdk，本地未推送）：
- Finding 1：`tool-bridge.ts:1` 的 `import type { HostCall } from "./host-rpc"` 悬空——`host-rpc.ts` 之前只导出 `HostRpc` 类，`HostCall` 类型不存在。
- Finding 2（enforce M8）：`tool-bridge.ts:89` 调 `hostCall("tool_invoke", {...})` 未传 `timeoutMs`/`signal`，HostRpc 默认 60s 传输超时抢先于 2× 兜底计时器，写类传输失败被误判为 `outcome:"error"`；`cancel()` 只设 abort 不中断 in-flight execute；原 outcome_unknown 门 `controller.signal.aborted && err.code==="timeout"` 排除写类非超时传输失败。

### 改动

**`pi-sidecar/src/host-rpc.ts`**
- 新增导出 `export type HostCall = (op, params, timeoutMs?, signal?) => Promise<unknown>`（signal 可选，中止时传输层立即 reject `code:"aborted"`）。
- `HostRpc.call(op, params, timeoutMs=60_000, signal?)`：新增可选 `signal` 参数并遵守——`signal.aborted` 或 abort 事件触发即清掉自身超时计时器、从 `pending` 摘除、以 `code:"aborted"` reject；其余调用方（无 signal）仍走默认 60s。`call` 改为 `Promise.withResolvers` 线性控制流。
- **最终 HostRpc 签名**：`call(op: string, params: Record<string, unknown>, timeoutMs?: number, signal?: AbortSignal): Promise<unknown>`（`timeoutMs` 默认 `60_000` 保留给所有其他调用方）。

**`pi-sidecar/src/tool-bridge.ts`**
- execute 把兜底预算 `fallbackMs = Math.max(timeout_seconds*2000, 3_600_000)` 同时作为 `timeoutMs`（≥ 兜底预算）与 `controller.signal` 传给 hostCall——sidecar 自己的兜底计时器先 abort、由它决定分类，HostRpc 默认 60s 不再抢先。
- `Inflight` 增 `cancelled?: boolean`；`cancel()` 置位并 abort。
- M8 分类门：写类（`!t.is_readonly`）任何传输层失败（超时/中止/连接错误）→ `outcome_unknown`（绝不伪造结果）；只读 → `error`；`cancel()` 显式取消 → `tool_cancelled`（commands.ts 语义）。

### M8 映射表（failure type × class → outcome）

| failure | 只读 (read-only) | 写类 (side-effecting) |
|---|---|---|
| 传输超时 (code:"timeout") | error | **outcome_unknown** |
| 传输中止/连接错误 (code:"aborted"/连接异常) | error | **outcome_unknown** |
| cancel() 显式取消 | error + tool_cancelled | outcome_unknown + tool_cancelled |
| Python 正常返回 (ok/error) | 透传 r.outcome | 透传 r.outcome |

side-effecting 永不自动重试（不重发即满足 M8）。

**`pi-sidecar/src/tool-bridge.test.ts`** — 新增 3 用例（注入 fake hostCall 遵守 `(op, params, timeoutMs, signal)`）：
- `M8: write-class transport timeout resolves outcome_unknown`（写类 + timeout → outcome_unknown）
- `M8: read-class transport timeout resolves error`（只读 + timeout → error）
- `cancel() interrupts in-flight execute and aborts the injected signal`（断言注入的 signal 确被 abort、timeoutMs≥3.6M 透传、返回 tool_cancelled/outcome_unknown 且不挂死）

### 测试

```
cd pi-sidecar && bun test
 51 pass / 0 fail / 136 expect() calls（7 文件，既有 48 + 新增 3 全绿）
```
