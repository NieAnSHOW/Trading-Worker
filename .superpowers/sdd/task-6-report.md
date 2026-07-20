# Task 6 Report: LLM Enrichment

## Status

Completed. Updated news tracks are enriched through one project LLM instance per refresh, with one bounded call per updated track and a global concurrency limit of three.

## Commit

- `387eadb7341d1545ee4528e044c84bcec9f0f9f1 feat(news): enrich tracks with project llm` (DCO signed)

## Files

- `agent/src/news/llm.py`
- `agent/tests/news/test_llm_enrichment.py`

## RED / GREEN Evidence

- RED: `pytest agent/tests/news/test_llm_enrichment.py -q`
  - Result: collection failed with `ModuleNotFoundError: No module named 'src.news.llm'`, confirming the enrichment boundary did not exist.
- GREEN: `pytest agent/tests/news/test_llm_enrichment.py -q`
  - Result: `8 passed in 0.10s`.
- Static checks: `ruff check agent/src/news/llm.py agent/tests/news/test_llm_enrichment.py` and `git diff --check`
  - Result: passed.

## Summary

- Builds the configured project LLM once only when at least one supplied track is updated.
- Sends at most 16 items per updated track, invokes asynchronously with a shared `Semaphore(3)`, and supports both `ainvoke` and Codex-compatible `invoke` providers.
- Treats feed text as untrusted data; accepts JSON only from provider response `content`, then strictly validates titles, highlights, unknown IDs, duplicates, and 300-character limits.
- Keeps Chinese original titles unchanged and converts factory, provider, and invalid-output failures into the stable public `ai_unavailable` code without exposing provider details.

## Risk Signals And Concerns

- Provider behavior remains inherently external and may return semantically weak but structurally valid summaries; the boundary deliberately validates shape and safety constraints rather than factual correctness.
- The refresh coordinator integration is outside Task 6 scope. This module exposes the requested pure async boundary for that caller to use.
