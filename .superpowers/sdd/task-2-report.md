# Task 2 Report — Python 协议帧编解码（与 TS 镜像）+ engine 开关

**Status: DONE** · Branch `feat/pi-agent-sdk` · Commit `72156241`

## What was implemented

New package `agent/src/pi_sidecar/` (verbatim per brief):

- `__init__.py` — `get_agent_engine()` (env `VIBE_AGENT_ENGINE` ∈ {"pi","legacy"}, default/invalid → "pi") + `LEGACY_ONLY_PROVIDERS = frozenset({"vip_server", "openai-codex"})`.
- `protocol.py` — `PROTOCOL_VERSION = 1`, `MAX_FRAME_BYTES = 1_048_576`, `FrameError(code, message)`, `encode_frame` (LF-terminated, UTF-8 byte check, `frame_too_large` on over-limit), `decode_frame` (4 frame kinds: request/ok/err/event, `bad_json` / `bad_frame`), `LineSplitter` (incremental LF splitting, oversized-line guard).

Tests: `agent/tests/pi_sidecar/__init__.py` (empty) + `test_protocol.py` (verbatim from brief).

Mirror check against `pi-sidecar/src/protocol.ts` (read during implementation): same constants, same error codes, same validation order (v → event/id → op → ok → error shape), same LF/byte semantics. `json.dumps(ensure_ascii=False)` ≡ TS `JSON.stringify` byte-for-byte for the frames in question; `LineSplitter` oversized check matches TS (`no \n in buffer && byteLen > MAX`).

## TDD evidence

**RED** — `python -m pytest agent/tests/pi_sidecar/test_protocol.py --tb=short -q`:

```
E   ModuleNotFoundError: No module named 'src.pi_sidecar'
=========================== short test summary info ============================
ERROR agent/tests/pi_sidecar/test_protocol.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

**GREEN** — same command after implementation:

```
17 passed in 0.03s
```

**Lint** — `ruff check agent/src/pi_sidecar agent/tests/pi_sidecar` → `All checks passed!`

Focused dir run `pytest agent/tests/pi_sidecar -q` → `17 passed`.

## Files changed (all per brief's Files list)

- `agent/src/pi_sidecar/__init__.py` (new)
- `agent/src/pi_sidecar/protocol.py` (new)
- `agent/tests/pi_sidecar/__init__.py` (new, empty)
- `agent/tests/pi_sidecar/test_protocol.py` (new)

Commit `72156241` — `feat(pi-sidecar): python protocol codec mirroring TS framing + engine flag` (DCO Signed-off-by present, no AI trailers).

## Deviations from plan

1. **Brief's step 5 expects `20 passed`; actual is `17 passed`.** The brief's own verbatim test file contains 17 tests (2 encode + 2+5+1 decode + 3 splitter + 4 engine = 17). No code deviation — the "20" is a miscount in the brief's expected-output note. All 17 pass.

## Self-review

- Completeness vs brief: all 4 files created exactly as specified; all interfaces in the Produces block present with correct signatures and semantics.
- YAGNI: nothing extra added — no client/session modules (those are Tasks 3/9).
- Tests verify real behavior: they exercise `encode_frame`/`decode_frame`/`LineSplitter`/engine switch directly (round-trip, byte-limit, error codes, incremental chunking, env fallback); no mocks. The oversized-line test depends on `MAX_FRAME_BYTES` (~1 MiB of "x") — fine.
- Edge cases exercised: empty/whitespace line, non-object, wrong v, non-string id/op/event, non-dict params, ok non-boolean, malformed error frame, non-ASCII passthrough (`ensure_ascii=False` mirrors TS).
- No scope creep; `agent/src/live/` untouched.

## Concerns

- None blocking. The 17-vs-20 count noted above (cosmetic, brief-internal).
- `agent/tests/pi_sidecar` was created with `__init__.py` — required because sibling test packages (`agent/tests/`) use the same layout, and pytest's rootdir/import mode resolves `src.*` cleanly with it.
