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
