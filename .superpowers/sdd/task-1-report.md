# Task 1 Report: Sidecar 协议帧编解码（TS）

**Status:** DONE

## What was implemented

`pi-sidecar/src/protocol.ts` — the JSONL wire codec for the Pi sidecar protocol (v1):

- `PROTOCOL_VERSION = 1`, `MAX_FRAME_BYTES = 1_048_576` (1 MiB, UTF-8 byte length — matches cross-task contract).
- `Frame` discriminated union: `RequestFrame {v,id,op,params?}`, `OkFrame {v,id,ok:true,result}`, `ErrFrame {v,id,ok:false,error:{code,message}}`, `EventFrame {v,event,session_id?,data?}`.
- `encodeFrame(frame)` → `JSON.stringify` + LF; throws `FrameError("frame_too_large")` when serialized bytes > 1 MiB.
- `decodeFrame(line)` → trims, parses, validates shape; error codes: `bad_json` (empty line / invalid JSON), `bad_frame` (wrong v, non-string id/op/event/session_id, bad params type, bad error payload, neither request/response/event).
- `LineSplitter.push(chunk)` → incremental LF splitting; throws `frame_too_large` when the buffered line exceeds 1 MiB (no LF yet), resets buffer on error.
- `FrameError` extends `Error` with `readonly code`.

`pi-sidecar/src/protocol.test.ts` — test suite verbatim from brief (10 用例 listed in brief; file actually contains **9** `test()` cases — see deviations).

## TDD evidence

**RED** — `cd pi-sidecar && bun test src/protocol.test.ts` (before implementation):

```
# Unhandled error between tests
error: Cannot find module './protocol' from '.../pi-sidecar/src/protocol.test.ts'
 0 pass
 1 fail
 1 error
Ran 1 test across 1 file. [88.00ms]
EXIT: 1
```

**GREEN** — same command after implementing `protocol.ts`:

```
 9 pass
 0 fail
 21 expect() calls
Ran 9 tests across 1 file. [20.00ms]
EXIT: 0
```

**Full package suite** — `cd pi-sidecar && bun test` (mandated TS verification):

```
 11 pass
 0 fail
 30 expect() calls
Ran 11 tests across 2 files. [514.00ms]
EXIT: 0
```

(Task 0's `check-sdk-exports.test.ts` still green alongside.)

## Files changed

- `pi-sidecar/src/protocol.ts` (new, 92 lines — verbatim from brief)
- `pi-sidecar/src/protocol.test.ts` (new, 93 lines — verbatim from brief)

## Commits

- `2344d5bd` feat(pi-sidecar): JSONL framing codec with 1MiB cap (DCO Signed-off-by: NieAnSHOW; no AI trailers)

## Deviations from plan

None in code. Two notes:

1. **Test-count discrepancy (brief-internal):** Brief step 4 says "全部 pass（10 用例）" but the brief's own test file contains 9 `test()` calls (2 encodeFrame + 4 decodeFrame + 3 LineSplitter, incl. the version-constant test). Test file was used verbatim; all 9 pass. The "10" was a miscount in the plan — no action needed.
2. **`bun-types` type-check gap (pre-existing, Task 0):** `pi-sidecar/tsconfig.json` sets `"types": ["bun-types"]` but `package.json` never declares `bun-types` (dev)dependency, so `bunx tsc --noEmit` fails with `error TS2688: Cannot find type definition file for 'bun-types'`. This predates Task 1 (Task 0 scaffold; `bun test` doesn't require the ambient types so the suite stays green). Fixing would touch `package.json`/lockfile — outside this brief's Files list. Recommend a later task add `bun-types` as a devDependency so `tsc --noEmit` works as a stricter gate.

## Self-review findings

- **Completeness vs brief:** All exported interfaces present with exact signatures (`PROTOCOL_VERSION`, `MAX_FRAME_BYTES`, `Frame`, `FrameError`, `encodeFrame`, `decodeFrame`, `LineSplitter`); implementation and tests byte-for-byte match the brief. Files list respected (only the two specified files; nothing else touched, working tree clean).
- **Quality / YAGNI:** No extras — no prefix validation, no logging, no parser dependencies. Plan's code is authoritative and used verbatim.
- **Tests verify real behavior, not mocks:** Round-trip encode→decode; 1 MiB cap exercised on both `encodeFrame` and `LineSplitter` paths; `bad_json` (parse error + whitespace-only line); 5 distinct `bad_frame` shapes; all four frame kinds accepted; incremental chunk splitting across two pushes; version constant. No mocks of the codec itself.
- **Cross-task contract check:** Single-frame JSON ≤ 1_048_576 UTF-8 bytes enforced via `Buffer.byteLength`; over-limit → `frame_too_large`; ids/event shapes match the RPC 帧协议（v1）section. `r-`/`s-` id prefixes are a producer-side wire convention (used as literals in tests), not codec-enforced — consistent with the brief.

## Concerns

- The `bun-types` gap above (pre-existing; suggests one-line fix in a later task).
- Minor: `LineSplitter` only raises `frame_too_large` when the oversized buffer contains no LF; a buffer containing a completed small line plus an oversized remainder defers the error to the next `push`. This is the brief's exact code and matches its tests; flagging only as a documented boundary, not changing it.
