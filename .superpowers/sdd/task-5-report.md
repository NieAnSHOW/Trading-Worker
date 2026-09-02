# Task 5 Report — SessionDriver（Pi AgentSession 接线）+ 模型/Provider 接线

Status: DONE_WITH_CONCERNS（见 Concerns）

## 实现内容

按 task-5-brief.md 实现了三个文件 + 一处最小支撑改动：

- `pi-sidecar/src/model-wiring.ts`（Create）：`resolveDefaultModel` + `vibeProviderRegistrations`。
- `pi-sidecar/src/model-wiring.test.ts`（Create）：brief 测试原样 + 两处笔误修正（见 Deviations #6）。
- `pi-sidecar/src/session-driver.ts`（Create）：`PiSessionDriver implements SessionDriver` + `PiSessionHandle implements SessionLike`，真实接线 pinned SDK（`createAgentSession`/`SessionManager`/`Settings`）。
- `pi-sidecar/src/session-driver.test.ts`（Create）：brief 测试原样。
- `pi-sidecar/extensions/vibe-providers/index.ts`（Create）：Vibe provider env → `pi.registerProvider` 接线。
- `pi-sidecar/src/session-index.ts`（最小改动）：新增公开 `get sessionsDir()` 访问器（Deviations #5）。

内建工具锁定（Global Constraint 全落实）：`toolNames: []`、`restrictToolNames: true`、`allowRestrictedCustomTools: true`、`enableMCP: false`、`enableLsp: false`、`disableExtensionDiscovery: true`、`agentDir: PI_AGENT_DIR`、`settings: Settings.isolated()`、`sessionManager: SessionManager.create(cwd, sessionDir)`、`prompt` 内 `streamingBehavior: "steer"`。

## TDD 证据

**RED**（实现前运行）：
```
src/session-driver.test.ts:
error: Cannot find module './session-driver' from '.../src/session-driver.test.ts'
src/model-wiring.test.ts:
error: Cannot find module './model-wiring' from '.../src/model-wiring.test.ts'
 0 pass, 3 fail, 1 error
```

**GREEN**（实现后）：
```
$ cd pi-sidecar && bun test src/model-wiring.test.ts src/session-driver.test.ts
 9 pass, 0 fail, 19 expect() calls
```

**全 sidecar 套件**（确认 session-index 访问器改动不破坏既有测试）：
```
$ cd pi-sidecar && bun test
 40 pass, 0 fail, 92 expect() calls, Ran 40 tests across 6 files
```
输出 pristine（无 logger 噪音，真实 `createAgentSession` 的 smoke 测试静默通过）。

**vibe-providers 烟测**（临时脚本，已删）：
```
OK 3 providers: deepseek,zhipu,groq
```
deepseek → `{api:"openai-completions", apiKey:"DEEPSEEK_API_KEY"}`；groq → baseUrl 环境覆盖 `http://127.0.0.1:9/v1`；moonshot 未注册。均符合预期。

## Files changed

- `pi-sidecar/src/model-wiring.ts`（Create）
- `pi-sidecar/src/model-wiring.test.ts`（Create）
- `pi-sidecar/src/session-driver.ts`（Create）
- `pi-sidecar/src/session-driver.test.ts`（Create）
- `pi-sidecar/extensions/vibe-providers/index.ts`（Create）
- `pi-sidecar/src/session-index.ts`（改：新增 `sessionsDir` getter）

## Deviations from plan（含理由）

1. **不 import `./tool-bridge`（Task 7 未落地）**：task-5-brief 的 session-driver 草稿 `import type { ToolBridge } from "./tool-bridge"`，但该文件属于 Task 7，当前不存在（`pi-sidecar/src` 下无 tool-bridge.ts）。改用本地结构化接口 `ToolBridgeLike`（`customTools`/`setManifest`/`cancel`，与 Task 7 契约对齐），Task 7 落地后真实 ToolBridge 结构上满足。若直接 import 会编译期失败。

2. **`model` 字段 → `modelPattern`**：pinned SDK `createAgentSession` 的 `model?: Model`（对象：id/provider/api/baseUrl…），`resolveDefaultModel` 返回的是字符串（`"deepseek/deepseek-chat"`），直接传 `model` 是类型错误且运行时字段错位。正确的字符串入口是 `modelPattern?: string | string[]`，其文档明言用于"extensions 注册 provider 后延迟解析"——正是 vibe-providers 场景。已改用 `...(model ? { modelPattern: model } : {})`。

3. **`registerProvider(name, config)` 双参**：SDK `ExtensionAPI.registerProvider(name: string, config: ProviderConfig)` 第一参数是 provider 名，且 `ProviderConfig` **无 `name` 字段**；`apiKey` 是环境变量名字符串（非函数）。brief 草稿的 `pi.registerProvider({id,name,baseUrl,api,apiKey:()=>...})` 与 d.ts 不符，已改：`pi.registerProvider(reg.id, { baseUrl, api:"openai-completions", apiKey: reg.envKey })`。

4. **`Settings.isolated()` 不带 agentDir override**：`Settings.isolated(overrides: Partial<Record<SettingPath, unknown>>)`，`agentDir` 不是 SettingPath（settings schema 是点分键），传 `{ agentDir }` 是类型错误。agentDir 已由 `createAgentSession` 顶层 `agentDir: PI_AGENT_DIR` 提供；`Settings.isolated()`（内存隔离实例）保留隔离语义，不触碰全局 `~/.omp/agent`。

5. **session 目录由 index 派生（新增 `SessionIndex.sessionsDir` getter）**：brief 草稿的 `create()` 硬编码 `SessionManager.create(PI_SESSIONS_DIR, PI_SESSIONS_DIR)`，但权威接受测试断言 `s.sessionFile.startsWith(dir)`（注入 index 的临时目录）。二者矛盾——唯一连接 driver 与 `dir` 的是注入的 index。方案：driver 的 session 位置取 `this.index.sessionsDir`（生产默认 `DEFAULT_SESSIONS_DIR` = `~/.vibe-trading/pi/sessions` = `PI_SESSIONS_DIR`，与草稿一致），并在 SessionIndex 加公开 getter。最小、向后兼容、仅为此测试必需。cwd 同步用 sessionsDir。

6. **model-wiring.test.ts 两处 `ids.find` → `regs.find`**：brief 测试第 39/40 行 `ids.find((r) => r.id === "groq")!.baseUrl`——`ids = regs.map(r=>r.id)` 是 `string[]`，`r.id` 恒 undefined，谓词永假 → `find` 返回 undefined → `.baseUrl` 抛 TypeError，测试验证不到任何行为（且无法通过）。按注释意图（`// env 覆盖`）改为 `regs.find(...)`。这是对验收测试的明显笔误修正，非放宽断言。

7. **`vibeProviderRegistrations` 包含条件 = key **或** baseUrl**：brief 草稿实现只检查 `env[p.envKey]`，但其测试期望 groq 仅设 `GROQ_BASE_URL` 也被注册。测试为权威，改为 `if (!key && !baseOverride) continue`。

8. **`SessionManager.open` 是 async**：`static open(...): Promise<SessionManager>`，草稿未 await，已 `await SessionManager.open(p)`。

9. **消息追加用 `appendMessage`**（Binding Handoff 5）：brief 草稿的 `appendUserMessage`/`appendAssistantMessage` 在 pinned SDK 中不存在；实际是 `sessionManager.appendMessage(message): string`（同步，返回 entry id）+ `getEntries(): SessionEntry[]`。已按此实现，并通过 handle 的松散 `sessionManager` 视图把 `appendMessage` 参数类型定为 `unknown`（SDK 的 `Message` 联合类型对 assistant 有大量必填字段，运行时 appendMessage 不校验形状，仅写 `{type:"message", ..., message}`；测试只断言 index 登记，生产忠实性由 Phase 8 覆盖）。

10. **`currentModel()`/`currentThinkingLevel()`**（Binding Handoff 2）：get_state 契约读这两个可选方法，草稿未提供。已加：`currentModel()` 返回 `session.model` 的 `"provider/id"`，`currentThinkingLevel()` 返回 `session.thinkingLevel` 字符串。

11. **`setModel` 字符串 → Model 解析**：`AgentSession.setModel(model: Model)` 需要对象；handle 的 `setModel(model: string)` 用 `session.modelRegistry.find(provider, id)` 解析后传入。

12. **navigateTree 返回对象**：`AgentSession.navigateTree(targetId)` 返回 result 对象（非字符串），草稿的 `typeof r === "string" ? r : entryId` 恒返回 entryId——按草稿保留（契约仅需 leaf_id 字符串）。

## 自审发现（已修）

- **构建期引入死代码**：初稿 buildSession 用 `vibeIdFor()` 存根传空 sessionId，导致 `s.sessionId` 非 vibeId、测试失败——已改为 buildSession 透传 vibeId（修正后测试通过）。
- **YAGNI 回退**：初稿给 `setToolManifest` 加了 `if (!f) return tools.length` 兜底；按 brief 契约 ToolBridge.setManifest/cancel 是必需的，已删兜底。
- **无必要 inline cast**：`(result as {session}).session` 实为 SDK 已类型化的 `CreateAgentSessionResult.session`，已去掉 cast 直接用 `result.session` / `session.sessionFile`。

## Concerns

- **`bun tsc --noEmit` 无法启动**：tsconfig 引用 `bun-types` 但未安装（配置级错误，与本次改动无关，其余既有文件同样报此错）。brief 规定的 TS 门禁是 `cd pi-sidecar && bun test`，已全绿。未在本次范围加装 bun-types（Files list 不含 package.json）。
- **测试写真实 ~/.vibe-trading/pi/agent**：smoke 测试的 `create()` 走真实 `createAgentSession`，会在 `~/.vibe-trading/pi/agent` 创建 agent.db/models.yml（writable user state，允许；非网络）。这是 brief 设计的 smoke 语义，Phase 8 集成测试再覆盖真实 provider 链路。
- **test #5 的 `writeFileSync(join(dir,"marker"),"done")`** 无断言（仅证明 dir 可写），保留原样。

## Fix: navigateTree failure surfacing

**Finding (Important)**: `session-driver.ts` `PiSessionHandle.navigateTree` used
`typeof r === "string" ? r : entryId`, silently reporting a cancelled/failed
navigation as success with the requested entry id. Pinned SDK's navigateTree
returns `Promise<{cancelled: boolean, aborted?: boolean, editorText?, editorImages?,
summaryEntry?, sessionContext?, reopenAsk?, askReanswerCommitted?}>` — never a string
(`pi-sidecar/node_modules/@oh-my-pi/pi-coding-agent/dist/types/session/agent-session.d.ts:954-1006`).

**Change**:
- `pi-sidecar/src/session-driver.ts` `navigateTree` now inspects the SDK result:
  if `cancelled === true` or `aborted === true`, throws
  `Object.assign(new Error("navigateTree: navigation to <id> was cancelled/aborted"),
  { code: "navigation_failed" })`. On confirmed success returns the requested `entryId`
  (contract `{leaf_id}`). Error is converted to a protocol error frame by the existing
  no-throw outer guard via `errCode` (reads `e.code`, fallback `internal`) — code
  `navigation_failed` is consistent with existing snake_case codes (`no_session`,
  `stale_cursor`, …). `PiSessionHandle` exported to enable the focused unit test.
- `pi-sidecar/src/session-driver.test.ts`: added a test covering `{cancelled:true}`,
  `{aborted:true}`, SDK rejection passthrough, and the happy path (`{cancelled:false}` →
  returns entryId). Existing happy-path fake in `commands.test.ts` untouched (still passes).

**Test command**: `cd pi-sidecar && bun test`
**Output summary**: `41 pass / 0 fail / 96 expect() calls across 6 files` (~1.15s).

## Fix 2: importMessages assistant shape

**Change summary**: The assistant path of `importMessages` in `pi-sidecar/src/session-driver.ts` was appending a structurally invalid AssistantMessage — `content` was a plain string and the mandatory `api`/`provider`/`model`/`usage`/`stopReason` fields were absent. Per the pinned SDK d.ts, AssistantMessage.content MUST be a content-block array and those metadata fields are required; migrated turns would be corrupt for the driver's own read path and Pi-internal consumers (replay/compaction). Now the assistant branch wraps the string into a `[{ type: "text", text }]` block and fills mandatory metadata with deterministic synthetic values: `api: "anthropic-messages"`, `provider: "vibe-import"`, `model: "imported"`, `usage` zeroed (`{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0}`), `stopReason: "stop"`. Timestamps now parse the source `timestamp` when present (falling back to `Date.now()`); unparseable timestamps skip the message. The user path stays as-is (UserMessage.content legitimately accepts a plain string).

**d.ts facts relied on** (pinned `@oh-my-pi/pi-ai/dist/types/types.d.ts` + `@oh-my-pi/pi-catalog/dist/types/types.d.ts`):
- `AssistantMessage { role:"assistant"; content:(TextContent|ThinkingContent|…|ToolCall)[]; api:Api; provider:Provider; model:string; usage:Usage; stopReason:StopReason; timestamp:number; … }` (types.d.ts ~L742)
- `TextContent { type:"text"; text:string }` (types.d.ts L501)
- `StopReason = "stop"|"length"|"toolUse"|"error"|"aborted"` (types.d.ts L675)
- `Api = KnownApi | (string & {})` (pi-catalog types.d.ts L5); `Provider = string` (L62)
- `Usage { input; output; cacheRead; cacheWrite; totalTokens }` (pi-catalog types.d.ts L67)
- `UserMessage { role:"user"; content:string | (TextContent|ImageContent)[] }` (types.d.ts L683) — user path valid as-is.

**Test change**: `pi-sidecar/src/session-driver.test.ts` added a test that imports BOTH a user and an assistant message through a fake session manager (capturing `appendMessage` calls) and asserts the assistant payload has a content-block array (`[{type:"text",text:"hello back"}]`) plus all mandatory fields present; the user payload stays a plain string.

**Test command**: `cd pi-sidecar && bun test`
**Output summary**: `42 pass / 0 fail / 108 expect() calls across 6 files` (~0.74s).
