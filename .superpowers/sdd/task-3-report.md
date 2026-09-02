# Task 3 Report: 会话索引（Vibe id → Pi JSONL 路径）+ header 校验

Status: DONE

## What I implemented

`pi-sidecar/src/session-index.ts`（Create，per brief verbatim）+ `pi-sidecar/src/session-index.test.ts`（Create，per brief verbatim）。

Exported surface (matches the brief's Interfaces block exactly):

- `DEFAULT_SESSIONS_DIR` = `~/.vibe-trading/pi/sessions`（writable user state，非只读 app bundle）
- `DEFAULT_INDEX_PATH` = `~/.vibe-trading/pi/session-index.json`
- `isValidVibeSessionId(id)` — `/^[0-9a-f]{12}$/`（recon: uuid4().hex[:12]）
- `readSessionHeaderId(jsonPath)` — 扫描前 8KB，找 `type==="session"` 条目返回其 `id`；找不到/解析失败/文件缺失 → `null`；坏行（如固定宽度 title slot 截断）跳过
- `class SessionIndex` — 构造时读取索引文件（兼容 v0 纯路径格式）；`register(vibeId, jsonlPath)`：header 缺失 → throw，否则记录 `{path, headerId}` 并原子持久化（tmp+rename，dirs 递归 mkdir）；`resolve(vibeId)`：仅当文件仍有 session header 且 `id === 记录的 headerId` 才返回 path（替换检测），非法 id → null；`all()` 返回拷贝

B2 核心语义已落地：`register` 只要求"存在合法 session header"，不强制 Pi header id === Vibe id（Task 0 已核对 SessionManager.create 不接受显式 session id，headerId 记录是权威设计）。Task 5 的 SessionDriver 在 buildSession 后立即 `register()`，restart-after-open 不会 yield `not_found`。

## TDD evidence

RED（step 2，实现前）— `cd pi-sidecar && bun test src/session-index.test.ts`：

```
src/session-index.test.ts:
# Unhandled error between tests
error: Cannot find module './session-index' from '.../src/session-index.test.ts'
 0 pass / 1 fail / 1 error
```

GREEN（step 4，实现后，含 brief 注的 API 替换）— `cd pi-sidecar && bun test src/session-index.test.ts`：

```
 10 pass
 0 fail
 19 expect() calls
Ran 10 tests across 1 file.
```

Full scoped suite `cd pi-sidecar && bun test`（protocol 8 + session-index 10 + Task 0 的 scripts/check-sdk-exports.test.ts 3）：**21 pass / 0 fail / 49 expect，输出无警告无残留**。

## Files changed

- `pi-sidecar/src/session-index.ts`（Create，+87）
- `pi-sidecar/src/session-index.test.ts`（Create，+91）

## Deviation from plan + reason

- `readSessionHeaderId` uses `readFileSync(jsonPath, "utf8").slice(0, 8192)` instead of the brief's verbatim `Bun.file(jsonPath).slice(0, 8192).text()`.
  Reason: this is the brief's **own sanctioned fallback**（note at brief line 217：API 与当前 Bun 版本不符时等价替换，语义相同——8KB 前缀扫描）。In Bun 1.3.14, `BunFile.text()` returns a `Promise<string>`; the brief's snippet doesn't await it, so `text.split` fails at runtime (`TypeError: text.split is not a function`). The sync replacement also fixes the missing-file case: `Bun.file()` never throws synchronously, so the brief's try/catch could not return `null` for a nonexistent file (observed as an unhandled ENOENT rejection mid-suite). The sync `readFileSync` inside try/catch returns `null` correctly for missing files — matching the "missing file → null" test.
  Deviation documented in a code comment at the site. No other deviations.

## Self-review findings

- Completeness: all 6 exported symbols match the brief's Interfaces block; test file byte-for-byte per brief (including the brief's own unused `mkdirSync` import — kept verbatim per "use code verbatim" rule).
- YAGNI: no extra features; v0 plain-path compat and `all()` copy are brief-specified, not added scope.
- Tests verify real behavior: real temp files, real fs reads, no mocks of the thing under test. The B2 core case (Pi-assigned header id ≠ vibeId accepted, recorded as headerId) and replacement detection (rewrite file with different header → resolve null) both exercise the actual fs path. The `register` throw paths assert both `toThrow(/header/i)` and `toThrow(/invalid/i)`.
- Edge checks done by reading: resolve on missing file → readSessionHeaderId null ≠ recorded headerId → null（文件没了 = 会话没了，语义正确）；title-slot 256-byte 填充行是合法 JSON（尾随空格），type "title" 被跳过，header 在下一行命中。
- No scope creep: only the brief's two files touched; `agent/src/live/` untouched.

## Concerns

- None blocking. Minor note for Task 4/5: `resolve` intentionally re-reads the JSONL header on every call (≤8KB) — fine for per-request lookup; if Task 5 ever needs hot-path resolve in a loop, caching with mtime check would be the upgrade, but YAGNI now.
- The brief's verbatim `Bun.file(...)` snippet was not runtime-correct on the pinned Bun 1.3.14; the fallback note resolved it — worth a one-line mention to the plan owner so Task 5 doesn't re-copy the async pattern.

## Commit

`fa14980a` feat(pi-sidecar): session index with header validation（`git commit -s`，DCO Signed-off-by: NieAnSHOW <42022984+NieAnSHOW@users.noreply.github.com>，无 AI attribution trailer）。仅含 brief Files list 的两文件；未推送。`.superpowers/sdd/task-1-report.md`、`task-2-report.md` 的未提交修改为主 agent 的报表文件，非本任务范围。
