# Task 8 Report: Bounded Result Cache And Provider Health

## Status: DONE

## What was implemented

### 1. `agent/src/reliability/cache.py` — `ResultCache`

Bounded LRU cache for read-only `StepResult`s. Sits in front of tool invocation in the reliability path only; does NOT touch `agent/src/providers/` loader caches.

**Interface (verbatim from brief):**
```python
class ResultCache:
    def get(self, key: str, *, now: datetime | None = None) -> StepResult | None: ...
    def put(self, key: str, result: StepResult, *, ttl_seconds: float) -> None: ...
```

### 2. `agent/src/reliability/providers.py` — `ProviderHealth`

Per-provider consecutive-failure counter with bounded circuit and fallback selection.

**Interface (verbatim from brief):**
```python
class ProviderHealth:
    def record_success(self, provider: str, elapsed_ms: int) -> None: ...
    def record_failure(self, provider: str, code: ErrorCode) -> None: ...
    def choose_fallback(self, provider: str, candidates: Sequence[str]) -> str | None: ...
```

### 3. Gateway integration (`agent/src/reliability/gateway.py`)

Added optional `cache` and `health` params to `ToolGateway.__init__`. When provided:
- Cache check after schema validation, before execution (read-only tools only).
- Telemetry events: `cache_hit` / `cache_miss` via `counters.record_reliability_event`.
- Health recording after execution: success resets failures, failure increments.
- Successful read-only results cached with a 300s TTL.

When cache/health are None (default), behavior is identical to before — existing loader caches and the off-path are completely untouched.

## Eviction policy

**LRU via `OrderedDict`.** On `get` hit, `move_to_end` promotes recency. On `put`, if size exceeds `max_size` (default 128), `popitem(last=False)` evicts the oldest entry. Simplest correct LRU using stdlib only.

## Key-normalization design

Caller passes a JSON string key. The cache re-parses it and re-serializes with `sort_keys=True` at all depths (`json.dumps(parsed, sort_keys=True)`). This makes `{"b":2,"a":1}` and `{"a":1,"b":2}` map to the same entry. Non-JSON keys pass through unchanged. The gateway builds keys as `json.dumps({"tool": tool_name, "args": arguments})`, so ordering independence is automatic.

## Monotonic expiry + test injection

Each entry stores two clocks at insertion:
- `mono_inserted`: `time.monotonic()` — used in production (`now=None`).
- `wall_inserted`: `datetime.now().timestamp()` — used when `get(now=...)` is injected.

This avoids wall-clock drift in production while allowing tests to inject `datetime` values without sleeping. The `put` method has no `now` param (per the fixed interface); tests inject time solely through `get`.

## How `_is_side_effecting` was reused

The cache does NOT re-derive the side-effecting classification. Instead:
1. **Result-level defense**: `put` refuses any `StepResult` with `status == UNSAFE_ERROR` or `error.code == UNSAFE_SIDE_EFFECT`.
2. **Key-level defense**: imports `_SIDE_EFFECTING_NAMES` and `_SIDE_EFFECTING_NAME_PREFIXES` from `gateway.py` and checks the normalized key string against them. Conservative: any key containing `trading_` or `bash` is refused.
3. **Caller-level defense**: the gateway integration checks `_is_side_effecting(tool_name, tool)` and only calls `cache.put` for read-only `SUCCESS` results.

Triple defense ensures side-effecting results are never cached.

## Files changed

| File | Action |
|------|--------|
| `agent/src/reliability/cache.py` | Created (ResultCache) |
| `agent/src/reliability/providers.py` | Created (ProviderHealth) |
| `agent/tests/test_reliability_cache.py` | Created (14 tests) |
| `agent/tests/test_reliability_providers.py` | Created (8 tests) |
| `agent/src/reliability/gateway.py` | Modified (optional cache/health integration, +63 lines) |

## TDD evidence

**RED** (before implementation):
```
ModuleNotFoundError: No module named 'src.reliability.cache'
ModuleNotFoundError: No module named 'src.reliability.providers'
2 errors during collection
```

**GREEN** (after implementation):
```
agent/tests/test_reliability_cache.py agent/tests/test_reliability_providers.py: 22 passed
```

## Step 5 regression output

```
pytest agent/tests/test_reliability_cache.py agent/tests/test_reliability_providers.py \
       agent/tests/test_local_loader.py agent/tests/test_yahoo_loader.py -q
....................................................                     [100%]
52 passed in 1.79s
```

All referenced loader test files exist. Full reliability suite + loaders: 183 passed, 0 failed.

## Verification of hard constraints

1. **Never cache side-effecting**: verified by `test_unsafe_error_result_not_cached`, `test_side_effecting_key_refused`, `test_bash_key_refused`. Three layers of defense (result status, error code, key pattern).
2. **Ordering-independent keys**: verified by `test_normalized_keys_ignore_argument_ordering` and `test_deeply_nested_ordering_normalized`. Deep recursive sort via `json.dumps(sort_keys=True)`.
3. **Off-path untouched**: the gateway constructor defaults `cache=None, health=None`. When None, zero cache/health code runs. Existing gateway tests (20 tests in `test_reliability_gateway.py`) all pass unchanged. Loader tests (`test_local_loader.py`, `test_yahoo_loader.py`) pass.
4. **No raw prompt/credential values**: the cache module adds nothing to keys — it only re-normalizes caller-provided JSON. Documented in module docstring.
5. **Monotonic expiry**: production path uses `time.monotonic()`. Test injection via `now` param uses wall-clock consistently.
6. **No new dependency**: stdlib only (`json`, `time`, `collections`, `dataclasses`, `datetime`).

## Concerns

None.
