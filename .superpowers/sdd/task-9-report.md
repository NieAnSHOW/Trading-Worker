# Task 9 Report: Sidecar 进程监管 + RPC 客户端（restart-once + `pi_sidecar_unavailable`）

**Status:** DONE
**Commit:** `15163be9` feat(pi-sidecar): supervised rpc client with restart-once semantics (DCO signed, no AI trailer)

## What was implemented

1. **`agent/tests/pi_sidecar/fixtures/stub_sidecar.py`** (Create, verbatim from brief) — protocol stub sidecar (pure Python, no bun):
   - Emits `ready` frame echoing `VIBE_TEST_ENV_MARKER` into the `sdk` field (B6 env-passthrough assertion)
   - Handles `ping` / `open_session` / `unknown_op` error frames
   - `STUB_CRASH_AFTER=<n>`: exits after handling the nth request (crash simulation)
   - `STUB_SEND_HOST_REQ=<op>`: after first request, sends one inbound host request (`s-host-1`) and **validates** the client's response frame; emits `host_roundtrip` event on success; on a non-ok frame its assert fails and the process dies (used by the error-path tests)

2. **`agent/src/pi_sidecar/client.py`** (Create, verbatim from brief + 2 minimal hygiene additions) — `PiSidecarClient`:
   - `SidecarError(code, message)`; restart budget exhausted → `code="pi_sidecar_unavailable"`
   - `start()` waits for the **ready frame** (process spawn alone is not readiness); timeout/crash counts against restart budget
   - `request(op, params, timeout=30.0)` correlates request ids to pending futures; error frames → `SidecarError`; `open_session`/`new_session` success registers active sessions
   - `respond_host(req_id, result)`: `{"_host_error_": {code, message}}` → ok:false frame; else ok:true + result (B3)
   - Inbound host-request dispatch via `on_host_request` (B3); handler crash also gets framed
   - `env` merged **over** `os.environ` for the child (B6); `VIBE_PI_AGENT_DIR`/`VIBE_PI_SESSIONS_DIR` injected when dirs given
   - `_default_command()`: `VIBE_PI_SIDECAR_BIN` if exists+executable → else `bun run src/main.ts` (B7)
   - Restart-once: crash/EOF → `_handle_process_loss` → reject in-flight with `sidecar_restarted` (no replay) → respawn → reopen active sessions; budget exhausted → permanent `pi_sidecar_unavailable`
   - `started` (ready frame + live process) / `unavailable` properties
   - stdout is the only protocol stream; stderr PIPE (diagnostics)

3. **`agent/tests/pi_sidecar/test_client.py`** (Create) — 10 tests: ping roundtrip, unknown-op error mapping, event dispatch (ready), inbound host-request roundtrip, handler-error → ok:false frame → stub exit → client restart serves next request, `respond_host` error frame shape, env passthrough, crash-restarts-once-and-reopens-sessions, second-crash-permanently-unavailable, unready-start-counts-against-budget.

## TDD evidence

**RED** (`client.py` temporarily removed):
```
$ pytest agent/tests/pi_sidecar/test_client.py --tb=short -q
E   ModuleNotFoundError: No module named 'src.pi_sidecar.client'
ERROR agent/tests/pi_sidecar/test_client.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```
Exactly the brief-predicted RED.

**GREEN**:
```
$ pytest agent/tests/pi_sidecar/test_client.py -q
10 passed in 2.20s
```
Stability: 3 consecutive repeat runs all `10 passed in 2.19s` (no flakiness in the restart/host-request timing tests).

**Focused dir** (per task instruction):
```
$ pytest agent/tests/pi_sidecar -q
43 passed in 2.68s        # includes sibling protocol/manifest/gateway_bridge tests
```

**Lint:**
```
$ ruff check agent/src/pi_sidecar agent/tests/pi_sidecar
All checks passed!
```

**Output pristineness:** 0 warnings — `grep -c 'Unraisable\|unclosed\|Event loop is closed'` = 0.

## Files changed

- `agent/src/pi_sidecar/client.py` (new, 314 lines)
- `agent/tests/pi_sidecar/fixtures/stub_sidecar.py` (new, 87 lines)
- `agent/tests/pi_sidecar/test_client.py` (new, 225 lines)
- Commit `15163be9`, `git commit -s`, Conventional Commits subject, no AI-attribution trailer. Only these 3 files staged; sibling task-report modifications left untouched.

## Deviations from plan (with reasons)

1. **Test called `c._respond_to_host(...)` but the brief's own `client.py` defines `respond_host`** (also the cross-task interface name in the brief's Interfaces section). Internal brief inconsistency. Resolution: test now calls `respond_host`; the client has no private `_respond_to_host`. (1 line in test.)

2. **Brief's two host-request tests never set `STUB_SEND_HOST_REQ`** — the stub's own documented env switch ("STUB_SEND_HOST_REQ=<op> 处理完第 1 个请求后，向 stdout 发出一个入站 host 请求"). Without it the stub never sends an inbound request, so:
   - `test_inbound_host_request_dispatched_and_response_framed` failed with `StopIteration` (host_roundtrip never arrived; 10s poll deadline).
   - `test_host_request_handler_error_yields_error_frame` **passed vacuously** — all asserts held while the B3 path was never exercised.
   Resolution: both tests construct the client with `env={"STUB_SEND_HOST_REQ": "tool_invoke"}`. This is required for the brief's own claimed "10 passed" and makes both tests verify real behavior. This also means brief step 5's "10 passed" could not have held as-written on a clean environment.

3. **Subprocess transport GC noise ("Event loop is closed" unraisable)**: the brief's `stop()`/`_handle_process_loss` never disconnect the child's stdin write pipe, so the asyncio subprocess transport never reaches `_try_finish` (it needs all pipes disconnected + process exited). When the transport is GC'd after `asyncio.run` closes the loop, `BaseSubprocessTransport.__del__` → `close()` → `call_soon` on the closed loop → `RuntimeError: Event loop is closed`, surfaced as `PytestUnraisableExceptionWarning` in 8/10 tests (verified empirically: `_try_finish` only completes when all `_pipes` are disconnected; closing `proc.stdin` while the loop is alive completes it — probe-verified CLEAN). Fix: in `stop()` and `_handle_process_loss`, `proc.stdin.close()` (idempotent) + guarded `await proc.wait()`. No behavioral change; output now pristine. This was mandated by "test output must be pristine".

4. **Cosmetic**: LSP formatter wrapped a few >120-col lines in client.py (e.g. the `_handle_process_loss` call in `_spawn`). Semantics identical to the brief (verified by parse + API surface probe + tests).

## Self-review findings

- **Tests verify real behavior, not mocks of the thing under test**: all subprocess tests use the real stub sidecar over real pipes; `test_respond_host_error_frame_shape` is the single deliberate unit-level fake (fake stdin) to inspect frame bytes — appropriate.
- **Restart tests are real**: `_proc.kill()` on the live stub, restart + session-reopen asserted via a request spy; `test_unready_start_counts_against_budget` uses a real `sys.exit(3)` subprocess.
- **YAGNI**: no extra features, no abstractions; client is the brief's code plus ~10 lines of transport hygiene.
- **Double-`_handle_process_loss` re-entrancy** (reader-EOF path vs request-path can theoretically race and double-increment the restart budget): the 0.2s sleeps in the brief's restart tests make it unreachable there, and `stop()`'s `_stopping` guard prevents post-stop races. Noted as a latent concern for the integration task, not fixed here (would deviate from brief).
- Stderr is PIPE but never drained (brief as-written); fine for the stub (writes < 64 KB buffer), noted for the real sidecar integration task (Task 15+).

## Concerns

- None blocking. Two integration-time notes: (a) the `_handle_process_loss` re-entrancy race above; (b) stderr pipe is never drained — a chatty real sidecar could block on a full stderr buffer (64 KB) in production use.
