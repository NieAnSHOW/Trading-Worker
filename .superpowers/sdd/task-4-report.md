# Task 4 Report: 命令循环 + 17 个 op 处理器（依赖注入）

- **Status:** DONE_WITH_CONCERNS（2 处计划缺陷已按契约意图最小修复，见 Deviations）
- **Commit:** `8cbfb3b1` feat(pi-sidecar): RPC command routing with injectable session driver
- **Files changed:** `pi-sidecar/src/commands.ts`（Create, 213 行）、`pi-sidecar/src/commands.test.ts`（Create, 164 行）

## What I implemented

按 brief 逐字实现 `createCommandHandler`：

- **接口**：`SessionLike`（AgentSession 子集，Task 5 的 PiSessionDriver 实现目标）、`SessionDriver`、`FrameSink`、`MsgOut` 全部按 brief 定义导出。
- **命令循环**：输入一行 → trim → 空行忽略 → `JSON.parse` 失败发 `bad_json` 错误帧 → 非 `{v:1,id,op}` 结构发 `bad_frame` → 分发 `handleRequest` → 结果帧经 `sink` 输出；外层 try/catch 兜底，**绝不 throw**（错误帧兜底，未知错误 code 落 `internal`）。
- **17 个 op**：`ping`（`{pong:true,protocol:1}`）、`new_session`/`open_session`（返回 `{session_file}`，成功后 `wireEvents` 接线订阅）、`prompt`（busy → `session_busy`，否则立即 ack `{accepted:true}`，`void s.prompt().catch()` 失败不 ack 失败）、`steer`/`follow_up`（`{queued:true}`）、`abort`（`{aborted:true}`）、`get_state`（busy/model/thinking_level/session_file/session_id；model/thinking_level 走可选 `currentModel?()/currentThinkingLevel?()` 探测）、`get_messages`（M1：limit 可选默认 100；非正整数 → `bad_request`；busy → `session_busy`；`before_cursor` 缺省 null；`nextCursor`→`next_cursor`）、`set_model`/`set_thinking_level`、`compact`（立即 ack `{started:true}`，fire-and-forget）、`navigate_tree`（`{leaf_id}`）、`export_session`（`{path}`）、`set_tool_manifest`（`{count}`）、`import_messages`（`{imported}`，会话内操作）、`tool_cancel`（`{cancelled}`）。
- **错误映射**：`unknown_op`、`no_session`、`session_busy`、`bad_request`、`bad_json`、`bad_frame`、`not_found`（driver.open 抛的 `code:"not_found"` 透传）、兜底 `internal`。
- **事件转发**：每会话 `wireEvents`（`Set<string>` 去重），driver 订阅回调 → `session_event` 帧 `{"event":"session_event","session_id":...,"data":{"type":...,"payload":...}}` 经 sink 发出；sink 写失败吞掉不 throw。`agent_end(isTerminal!==false)` 由 Task 5 的 driver 实现转成该帧（本层只透传）。
- `hostCall` 留在 deps 签名并解构（本任务未使用，Task 5/main 消费），按 brief 原样。

## TDD Evidence

**RED** — Step 2（写测试后、实现前）：
```
$ bun test src/commands.test.ts
error: Cannot find module './commands' from '.../pi-sidecar/src/commands.test.ts'
 0 pass / 1 fail / 1 error
```

**GREEN** — Step 4（实现后）：
```
$ bun test src/commands.test.ts
 10 pass
 0 fail
 24 expect() calls
Ran 10 tests across 1 file.
```

**全量回归**（不越界，仅 pi-sidecar 子套件）：
```
$ bun test   # pi-sidecar 目录
 31 pass
 0 fail
 73 expect() calls
Ran 31 tests across 4 files.
```
（pi-sidecar 无 tsc 依赖；全局约束指定的验证门是 `cd pi-sidecar && bun test`，已全绿。未跑仓库全量套件，符合非协商规则。）

## Deviations from plan（均记录在测试文件内注释）

1. **brief 实现代码末尾多一个 `}`（第 428 行）** — 按 brief 原样会语法错误（`createCommandHandler` 已在第 427 行 `}` 闭合，428 是游离括号）。已省略该多余 `}`；文件 213 行、括号平衡、bun 编译通过。这是计划文件未编译过的机械笔误，非契约变更。
2. **bad json 测试无法触发 `bad_json`** — harness 的 `send` 是 `handler(JSON.stringify(obj))`，`h.send("{nope")` 会把字符串再序列化成合法 JSON 字符串 `"\"{nope\""`，handler 解析成功（值是 string）→ 走 `bad_frame` 分支，测试收到 `bad_frame` 而非 `bad_json`。修复：`send` 对 string 类型直接透传原始行（`typeof obj === "string" ? obj : JSON.stringify(obj)`），对象仍走 JSON.stringify。断言文本未动。
3. **import_messages 测试缺会话创建** — 该 harness 从未 new_session/open_session，`needSession` 正确返回 `no_session`，与 `{imported:2}` 断言冲突（实现无错：import_messages 是会话内操作，brief 实现同样用 `needSession`）。修复：r-22 前插入 `new_session`（r-22-prep），后续帧下标 +1。其余断言未动。

以上两处修复都以 brief 自身契约（Step 4 "全部 pass（10 用例）" + result 形状表）为准，最小化改动测试设施，未改任何实现行为。

## Self-review findings

- **完整性**：17/17 op 逐字对齐 brief；result 形状与「跨任务接口契约」表一致；`prompt`/`compact` 立即 ack、错误经 session_event 流出；`get_messages` 的 M1 语义（limit 缺省 100、仅非正整数报 bad_request）与计划修复一致。
- **质量**：`needSession`/`requireParams`/`errCode` 单一职责助手；错误帧兜底保证 handler 永不 throw；事件接线幂等（`wired` Set）。
- **YAGNI**：未加任何计划外特性；`hostCall` 保持解构未用（Task 5 契约的一部分）。
- **测试真实性**：fake SessionLike 模拟 Task 5 SessionDriver 必须满足的契约面（prompt/steer/abort/getMessages/subscribe/importMessages…），断言的是协议帧行为而非实现内部——正是 brief 的 DI 设计意图。event 转发测试用真实 subscribe 回调注入验证。

## Concerns

- 两个测试缺陷意味着计划文件未经编译验证（Task 4 的测试与实现互斥）。Task 5 起若计划含可执行代码，建议先跑 RED 再实现，避免同类互斥。
- `encodeFrame` 对 >1MiB 帧抛 `FrameError`：外层 catch 会尝试再发一条错误帧（小帧，不会超限）——理论上若错误帧本身超限会二次抛，实际不可达，按 brief 原样保留。
- 未触碰 `agent/src/live/` 行为；未推送。

## Report file

本文件：`.superpowers/sdd/task-4-report.md`
