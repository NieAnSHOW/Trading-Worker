# Task 1 Report: Reader Access-Restriction Search Fallback

## Scope

Implemented only Task 1 in the isolated worktree.

Files changed:
- `/Users/niean/Documents/project/Vibe-Trading-Desktop/.worktrees/web-reader-search-fallback/agent/src/tools/web_reader_tool.py`
- `/Users/niean/Documents/project/Vibe-Trading-Desktop/.worktrees/web-reader-search-fallback/agent/tests/test_web_reader_privacy.py`

Commit:
- `304119d8c313cf9618d543995ee7673d53425f54`
- Subject: `feat: add search fallback for restricted web reads`
- DCO trailer present: `Signed-off-by: NieAnSHOW <42022984+NieAnSHOW@users.noreply.github.com>`

## Implementation

- Classified reader failures as source-access restrictions for HTTP `401`, `403`, `429`, and `451`, plus explicit access-denial/bot/captcha markers.
- Constructed bounded, site-scoped fallback queries from URL pathname and hostname only; query string, fragment, and userinfo are excluded.
- Performed one `WebSearchTool.execute(query=..., max_results=5)` attempt for restricted source reads.
- Returned a labelled `summary_only` envelope on successful search results, including safe result title/url/snippet fields and security warnings on externally sourced text.
- Returned a stable `source_access_denied` error envelope when fallback search is unavailable, malformed, raises, or returns no usable results.
- Kept normal 200 full-content behavior and non-eligible HTTP-error behavior unchanged.
- Avoided including raw upstream Jina error bodies in fallback content or access-denied error envelopes.

## TDD Evidence

Initial red-phase command:

```text
$ pytest agent/tests/test_web_reader_privacy.py -q
F.....FFFFFFF                                                            [100%]
8 failed, 5 passed in 1.40s
```

The fallback tests failed because `WebSearchTool` was not yet imported or called by `web_reader_tool`; the changed generic HTTP assertion also still contained the old `451` expectation and was corrected before implementation.

Focused green-phase command:

```text
$ pytest agent/tests/test_web_reader_privacy.py -q
.............                                                            [100%]
13 passed in 1.73s
```

Additional deterministic security coverage:

```text
$ pytest agent/tests/test_web_reader_security.py -k 'rejects_non_public_targets_without_network' -q
..................                                                       [100%]
18 passed, 1 deselected in 1.70s
```

Changed-file lint:

```text
$ ruff check agent/src/tools/web_reader_tool.py agent/tests/test_web_reader_privacy.py
All checks passed!
```

Syntax check:

```text
$ python -m py_compile agent/src/tools/web_reader_tool.py
```

Output was empty with exit code 0.

Whitespace/self-review check:

```text
$ git diff --check
```

Output was empty with exit code 0.

Required combined command:

```text
$ pytest agent/tests/test_web_reader_privacy.py agent/tests/test_web_reader_security.py agent/tests/test_security_scanner.py -q
...............................F....F....                                [100%]
2 failed, 39 passed in 5.19s
```

Exact failures:

```text
FAILED agent/tests/test_web_reader_security.py::test_read_url_allows_public_http_targets
AssertionError: {'timeout': Timeout(connect=5, read=25, total=30)} != {'timeout': 30}

FAILED agent/tests/test_security_scanner.py::test_web_search_adds_security_warning_to_snippets
KeyError: 'security_warnings'
WARNING src.tools.web_search_tool:web_search_tool.py:359 qihu fallback failed: HTTP Error 302: The HTTP server returned a redirect error that would lead to an infinite loop.
The last 30x error message was:
Moved Temporarily
```

## Self-Review

- Checked that fallback search only executes after `_url_allowed` accepts the target.
- Checked that direct full-content success does not include fallback fields or invoke search.
- Checked that non-eligible HTTP 500 does not invoke search and retains the existing error contract.
- Checked that 403/429/451 produce summary-only results from one mocked search call.
- Checked that fallback search output excludes the upstream `AbuseAlleviationError` body.
- Checked that fallback query excludes `tracking`, `secret`, and `#section` from the target URL.

## Concerns

The two failures in the required combined command are unrelated baseline failures and were not modified, per task scope:

1. `test_read_url_allows_public_http_targets` expects the obsolete scalar `timeout=30`, while the existing reader implementation uses `urllib3.Timeout(connect=5, read=25, total=30)`; the privacy suite already asserts that current timeout contract.
2. `test_web_search_adds_security_warning_to_snippets` falls through to the qihu network fallback in this environment, yielding no result with `security_warnings`; this is in `web_search_tool` and outside Task 1.

No live reader traffic was made by Task 1 tests: `requests.get` and `WebSearchTool.execute` were monkeypatched in the new fallback coverage.

## Fix Report

Files changed:
- `/Users/niean/Documents/project/Vibe-Trading-Desktop/.worktrees/web-reader-search-fallback/agent/src/tools/web_reader_tool.py`
- `/Users/niean/Documents/project/Vibe-Trading-Desktop/.worktrees/web-reader-search-fallback/agent/tests/test_web_reader_privacy.py`

Fixes:
- Normalized non-OK search responses to the fixed safe reason `summary search failed: search unavailable`; backend error text is not exposed.
- Narrowed captcha restriction detection to explicit challenge, requirement, verification, or human-check context.
- Preserved the `site:<hostname>` clause when bounding long fallback queries.
- Added regressions for malicious search-error leakage, ordinary captcha text, and long-path site preservation.

Validation:

```text
$ pytest agent/tests/test_web_reader_privacy.py agent/tests/test_web_reader_security.py -q
.................................F                                       [100%]
1 failed, 33 passed in 1.93s

$ pytest agent/tests/test_web_reader_privacy.py -q
...............                                                          [100%]
15 passed in 1.64s

$ pytest agent/tests/test_web_reader_privacy.py -k 'fallback_search_error or ordinary_captcha or long_fallback' -q
...                                                                      [100%]
3 passed, 12 deselected in 1.26s

$ ruff check agent/src/tools/web_reader_tool.py agent/tests/test_web_reader_privacy.py
All checks passed!

$ python -m py_compile agent/src/tools/web_reader_tool.py agent/tests/test_web_reader_privacy.py

$ git diff --check
```

Commit:
- Implementation commit: `2845aca672c8de62054e316acf560d555902f7be`
- Subject: `fix: harden web reader search fallback`
- DCO trailer present: `Signed-off-by: NieAnSHOW <42022984+NieAnSHOW@users.noreply.github.com>`

Concerns:
- The required combined suite has one unrelated baseline failure: `agent/tests/test_web_reader_security.py::test_read_url_allows_public_http_targets` expects `timeout=30`, but the existing implementation passes `urllib3.Timeout(connect=5, read=25, total=30)`. This test was not modified because it is outside the requested fixes.
