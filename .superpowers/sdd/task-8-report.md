# Task 8 Report: FastAPI news routes

## Status

Implemented the authenticated, input-free `/news-api` boundary:

- `GET /news-api/snapshot`
- `POST /news-api/refresh` returns 202
- `GET /news-api/refresh/status`

The route module receives both the existing `require_auth` dependency and a
coordinator instance. `api_server` creates the lazy singleton once, registers
the routes before `serve_main` may mount the root SPA, and closes that same
instance during shutdown. The static fallback preserves SPA HTML for `/news`
but returns a JSON 404 for unknown `/news-api/...` paths.

No handler accepts a body or query parameters, so callers cannot select a
track, feed URL, or LLM configuration. Rejections and the unavailable
coordinator response use constant JSON details; no request values, provider
details, or credentials are logged or returned.

## TDD Evidence

Tests were written before `news_routes.py` existed. The required initial RED
command was run from the repository root:

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
```

It initially stopped at collection because importing the pre-existing
`api_server` registers `src.api.scheduled_routes.delete_scheduled_run` with
HTTP 204 and a default body response. On this machine (`Python 3.11.0`,
`FastAPI 0.115.2`), FastAPI raises `AssertionError: Status code 204 must not
have a response body` at `agent/src/api/scheduled_routes.py:229`. `git show
493badb` confirmed that route and its registration order were in the baseline.
The parent task explicitly expanded Task 8 scope for this compatibility
prerequisite. A subprocess `import api_server` regression test was RED, then
GREEN after the DELETE route explicitly uses `response_class=Response`,
`response_model=None`, and returns an empty 204 `Response`. The explicit
response model is necessary because postponed `-> None` annotations otherwise
become a truthy FastAPI response model.

An isolated news-route GREEN run uses only `FakeNewsCoordinator`,
`asyncio.Event`, and `TestClient`; it creates neither a feed client nor an LLM:

The initially added GET-with-body assertion correctly failed (`200 != 422`),
identifying a local conditional that checked bodies only on POST. It was
changed to reject a nonempty body for every method, then the isolated suite
passed. Syntax verification also passed:

```text
python -m py_compile agent/api_server.py agent/src/api/news_routes.py
```

The existing auth test fixture has a separate local dependency compatibility
constraint: Starlette 0.39.2's `TestClient` has no `client=(host, port)`
constructor argument, while the test suite uses that argument to model local
and remote peers. The initial full GREEN run therefore had 31 pre-request
`TypeError` failures. In the Task 8-allowed auth test file only, a conditional
ASGI scope shim supplies the same peer host via a private test header on old
Starlette. Newer Starlette continues to use its native `client=` argument;
production auth and dependencies were not changed.

## Verification

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
63 passed, 7 warnings

python -m py_compile agent/api_server.py agent/src/api/news_routes.py
PASS
```

## Access-Log Subroute Redaction Exception

### Root Cause and Repair

The initial access-log follow-up only recognized a value beginning with
`/news-api?`. As a result, a rejected subroute request such as
`/news-api/snapshot?llm=sk-canary` retained the arbitrary query value in the
Uvicorn access log.

The helper now partitions the value on `?` and removes the entire query only
when the resulting path is exactly `/news-api` or begins with `/news-api/`.
This retains the path for route diagnosis, does not match `/news-apiary`, and
leaves ordinary endpoint credential-name redaction unchanged.

### TDD Evidence

RED: the existing `LogRecord` canary was changed first to use
`/news-api/snapshot?llm=sk-canary&foo=bar`. Before the helper change, it
failed because the rendered log still contained `sk-canary`.

GREEN: the minimal namespace/path partition change made the focused test pass:

```text
pytest agent/tests/test_sse_ticket_and_headers.py::test_uvicorn_filter_removes_all_query_values_from_news_api_subroute_access_logs -q
1 passed, 7 warnings
```

### Verification

```text
pytest agent/tests/test_sse_ticket_and_headers.py -q
5 passed, 3 failed, 7 warnings
```

The three failures remain the documented Starlette 0.39.2 `TestClient` lack
of `client=(host, port)` support; the new subroute canary and existing generic
credential redaction test pass.

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
64 passed, 21 warnings

python -m py_compile agent/api_server.py agent/src/api/news_routes.py agent/src/api/security.py
ruff check agent/src/api/security.py agent/tests/test_sse_ticket_and_headers.py
git diff --check
PASS
```

## Contract Regression Strengthening

The production route implementation was already correct when this follow-up
test-only pass began, so the added assertions are documented as regression
verification rather than a fabricated RED/GREEN cycle. The focused suite
verifies that:

- `FakeNewsCoordinator` starts a background refresh that actually awaits an
  `asyncio.Event`; the second POST sees that task still running and does not
  create a second task.
- A corrupted snapshot returns the stable unavailable/corruption envelope, and
  a synthetic secret canary is absent from both the JSON response and captured
  logs.
- A cross-site refresh POST returns 403 before the real app's registered
  coordinator `start` method is invoked.
- A controlled `api_server` re-import replaces the coordinator factory and
  route-registration function only for the import. `TestClient` shutdown then
  proves the coordinator passed into registration is the exact instance closed
  by the registered shutdown hook.

Regression verification completed with the existing production implementation:

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
64 passed, 21 warnings

python -m py_compile agent/api_server.py agent/src/api/news_routes.py
PASS
```

## Commit

Current `HEAD`: `feat(news): expose authenticated refresh api` (DCO signed).

## Risk

The tested API server import, global auth, static fallback, and shutdown hook
are now covered. The remaining warnings are existing FastAPI `on_event`
deprecation warnings and a Starlette multipart parser warning; they do not
affect test outcomes.

## Access-Log Query Redaction Follow-up

### Root Cause

`api_server` already installs `_UvicornQuerySecretRedactionFilter` on
`uvicorn.access`, but its generic parameter-name regex only recognized known
credential names. Uvicorn renders the request path and query from its access
log record after the response, so a rejected `/news-api?llm=sk-canary&foo=bar`
request could still write arbitrary query values to the access log. The
`/news-api` API contract accepts no query parameters, making the entire query
illegal input rather than a value that should be selectively retained.

### TDD Evidence

RED: added a minimal `LogRecord` canary regression for
`/news-api?llm=sk-canary&foo=bar`. Before the filter change, the assertion
failed because the rendered access log contained `sk-canary`.

GREEN: the filter now replaces an access-log argument beginning with
`/news-api?` with `/news-api` before applying the existing targeted redaction
to all other paths. The focused regression passed:

```text
pytest agent/tests/test_sse_ticket_and_headers.py::test_uvicorn_filter_removes_all_query_values_from_news_api_access_logs -q
1 passed, 7 warnings
```

The full module was also run. It has three pre-existing failures caused by
Starlette 0.39.2 not supporting `TestClient(client=(host, port))`; the new
access-log regression passes, and this follow-up intentionally does not widen
scope into that test compatibility issue:

```text
pytest agent/tests/test_sse_ticket_and_headers.py -q
5 passed, 3 failed, 7 warnings
```

Task 8 integration and static checks passed:

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
64 passed, 21 warnings

python -m py_compile agent/api_server.py agent/src/api/news_routes.py agent/src/api/security.py
ruff check agent/src/api/security.py agent/tests/test_sse_ticket_and_headers.py
git diff --check
PASS
```

## Trusted Origin Policy Follow-up

### Approved Policy

Unsafe HTTP requests now reject `Sec-Fetch-Site: cross-site` and require any
supplied `Origin` to be either exact same-origin or an exact normalized member
of `_CORS_ORIGINS`. Requests without `Origin` retain the existing non-browser
authentication behavior. The default origins now include both Vite addresses:
`http://localhost:5899` and `http://127.0.0.1:5899`.

The comparison normalizes only HTTP(S) origins (scheme/host case, trailing host
dot, default ports, and IPv6 brackets) and rejects credentials, paths, query
strings, fragments, malformed ports, and non-HTTP(S) values. Custom
`CORS_ORIGINS` is compared as configured; defaults are not merged into it.
The guard is invoked by `require_auth`, `require_local_or_auth`, and
`require_settings_write_auth`. The later review found that this dependency
coverage did not include unsafe routes registered without one, so the
application-level repair below is required for global enforcement.

### TDD Evidence

RED, before the implementation:

```text
pytest agent/tests/test_security_auth_api.py -q -k 'vite_origin_can_refresh_across_ports or unconfigured_loopback_origin_cannot_start_news_refresh or same_origin_request_can_start_news_refresh or non_browser_request_without_origin_can_start_news_refresh or default_cors_origins_include_vite_loopback_hosts'
2 failed, 3 passed
```

The arbitrary `http://127.0.0.1:45678` Origin incorrectly received `202` and
started the news coordinator; the two default Vite origins were absent.

An additional global-write RED demonstrated that the same arbitrary loopback
Origin could write `/settings/llm` in local dev mode:

```text
pytest agent/tests/test_security_auth_api.py::test_unconfigured_loopback_origin_cannot_write_llm_settings -q
1 failed (expected 403, got 200)
```

GREEN:

```text
pytest agent/tests/test_security_auth_api.py -q -k 'vite_origin_can_refresh_across_ports or unconfigured_loopback_origin_cannot_start_news_refresh or custom_cors_origins_do_not_merge_loopback_defaults or same_origin_request_can_start_news_refresh or non_browser_request_without_origin_can_start_news_refresh or remote_trusted_origin_still_requires_api_key or unconfigured_loopback_origin_cannot_write_llm_settings or default_cors_origins_include_vite_loopback_hosts'
8 passed
```

The tests use local/remote `TestClient` ASGI scope fakes and verify configured
cross-port Vite access, arbitrary loopback rejection before coordinator work,
same-origin and no-Origin regressions, custom-origin authority, and remote
`API_AUTH_KEY` enforcement.

### Verification

```text
pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -q
72 passed, 21 warnings

pytest agent/tests/test_api_infrastructure.py -q -k 'not test_api_server_is_thin_assembler'
32 passed, 1 deselected, 7 warnings

python -m py_compile agent/src/api/security.py
ruff check agent/src/api/security.py agent/tests/test_security_auth_api.py agent/tests/test_api_infrastructure.py
git diff --check
PASS
```

`test_api_server_is_thin_assembler` remains a pre-existing unrelated failure:
the current `api_server.py` has 655 lines while its stale assertion requires
fewer than 400. It was not changed because it is outside this security-only
Task 8 scope.

## Global Unsafe-Origin Middleware Repair

### Root Cause and Repair

The prior trusted-Origin guard only ran through authentication dependencies.
`POST /watchlist/stocks` and `DELETE /watchlist/stocks/{code}` are registered
without one, so a loopback request with an arbitrary Origin could reach the
SQLite write handler.

`_reject_untrusted_browser_write_middleware` now runs for every request before
route handling. It delegates to the existing
`_reject_untrusted_browser_write` and `_reject_cross_site_browser_request`
policy; it does not duplicate Origin parsing or alter CORS configuration.
Dependency-level checks remain intentionally redundant and fail closed.

### TDD Evidence

RED was added before production code and run from the repository root:

```text
pytest agent/tests/test_security_auth_api.py::test_unconfigured_loopback_origin_cannot_write_watchlist_before_persistence -q
1 failed (expected 403, got 200)
```

The test replaces the watchlist connection with a spy and proves the handler
executes before the repair. After adding the middleware, the same command was
GREEN (`1 passed`). Additional global-route regression cases prove that the
configured `http://localhost:5899` origin, exact same-origin requests, and
non-browser requests without Origin still reach the watchlist write spy; the
unconfigured `http://127.0.0.1:45678` case receives 403 with no write.

### Verification

```text
pytest agent/tests/test_security_auth_api.py -q -k 'watchlist or vite_origin_can_refresh_across_ports or unconfigured_loopback_origin_cannot_start_news_refresh or same_origin_request_can_start_news_refresh or non_browser_request_without_origin_can_start_news_refresh or default_cors_origins_include_vite_loopback_hosts'
9 passed, 59 deselected, 7 warnings

pytest agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py agent/tests/test_watchlist_crud.py -q
83 passed, 21 warnings

pytest agent/tests/test_api_infrastructure.py -q -k 'not test_api_server_is_thin_assembler'
32 passed, 1 deselected, 7 warnings

python -m py_compile agent/src/api/security.py agent/api_server.py
ruff check agent/src/api/security.py agent/tests/test_security_auth_api.py agent/tests/test_api_infrastructure.py
git diff --check
PASS
```

`ruff check agent/api_server.py` continues to report the pre-existing
`ENV_PATH` F811 redefinition, so that unrelated file-wide lint violation is
not claimed as clean.
