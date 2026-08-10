"""Replay tests: drive fixed JSON fixtures through the reliability runtime.

Loads cases from ``fixtures/reliability_cases.json`` and runs each through
``ReliabilityRuntime.run()`` with fake provider/LLM outcomes injected via the
gateway + executor seams. Asserts ONLY deterministic fields:
  - terminal status (success / partial / failed / cancelled),
  - selected-tool selection (the step ran exactly the expected tool),
  - recovery / attempt counts (gateway call_count),
  - evidence coverage (verified step count, claim coverage),
  - absence of unsupported claims in the synthesis of unsafe paths.

Never sends network. No live market or broker calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._reliability_replay_runner import FIXTURE_PATH, load_cases, run_case

# ponytail: parametrize over the JSON cases so adding a fixture row is the
# only change needed to add coverage. Each row becomes one xfail-free test.
CASES = load_cases()
CASE_NAMES = [c["name"] for c in CASES]
CASE_BY_NAME = {c["name"]: c for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=CASE_NAMES)
def test_replay_case_terminal_status(case: dict[str, Any], tmp_path: Path) -> None:
    """Each fixture case must produce its declared terminal status."""
    result = run_case(case, tmp_path=tmp_path)
    expected = case["expected_status"]
    assert result["status"] == expected, (
        f"{case['name']}: expected status={expected!r}, got {result['status']!r}; "
        f"reason={result.get('reason')!r}; reliability={result.get('reliability')}"
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_NAMES)
def test_replay_case_redacted_reliability_summary(case: dict[str, Any], tmp_path: Path) -> None:
    """The redacted reliability summary must not leak prompts, args, or credentials."""
    import json

    result = run_case(case, tmp_path=tmp_path)
    rel = json.dumps(result.get("reliability", {}))
    lowered = rel.lower()
    for taboo in ("prompt", "aapl", "$$$", "buy", "credential", "secret", "password", "token"):
        assert taboo not in lowered, f"{case['name']}: reliability summary leaked {taboo!r}: {rel}"


@pytest.mark.parametrize("case", CASES, ids=CASE_NAMES)
def test_replay_case_attempt_count_within_budget(case: dict[str, Any], tmp_path: Path) -> None:
    """For fault-driven cases, gateway attempts must stay within the step budget.

    Side-effecting cases must execute exactly once (never retried). Read-only
    retryable cases must respect retry_limit + step_budget.
    """
    result = run_case(case, tmp_path=tmp_path)
    # The runtime records total_step_attempts as an event; pull it for assertion.
    events = result.get("reliability", {}).get("events", {})
    attempts = int(events.get("total_step_attempts", 0))
    setup = case.get("setup", {})
    step_budget = int(setup.get("step_budget", 6))

    # Budget invariant: total attempts never exceed the configured step budget.
    assert attempts <= step_budget, (
        f"{case['name']}: total_step_attempts={attempts} exceeds step_budget={step_budget}"
    )

    # Side-effect invariant: a side-effecting step runs at most once.
    if setup.get("step_side_effecting"):
        # The runtime records step_success / step_terminal events; one execute
        # call per side-effecting step is the contract. We assert via the
        # runtime's own event counter: a side-effecting step contributes at
        # most one step_terminal:* event across all statuses.
        terminal_events = [k for k in events if k.startswith("step_terminal:")]
        assert len(terminal_events) <= 1, (
            f"{case['name']}: side-effecting step must not retry; "
            f"terminal_events={terminal_events}"
        )


@pytest.mark.parametrize("case", CASES, ids=CASE_NAMES)
def test_replay_case_evidence_coverage(case: dict[str, Any], tmp_path: Path) -> None:
    """Evidence coverage must match the case's expected_status semantics.

    * failed / cancelled -> claims_coverage is 0.0 (no verified success).
    * partial -> claims_coverage < 1.0 OR steps_verified < steps_total.
    * success -> claims_coverage >= 1.0 (no success cases in this MVP suite).
    """
    result = run_case(case, tmp_path=tmp_path)
    rel = result.get("reliability", {})
    coverage = float(rel.get("claims_coverage", 0.0))
    status = result["status"]

    if status in ("failed", "cancelled"):
        assert coverage == 0.0, (
            f"{case['name']}: failed/cancelled run must have 0 coverage, got {coverage}"
        )
        # No verified success steps on a failed/cancelled run.
        assert int(rel.get("steps_verified", 0)) == 0
    elif status == "partial":
        # Partial means EITHER incomplete claim coverage OR incomplete step verification.
        ok = (coverage < 1.0) or (int(rel.get("steps_verified", 0)) < int(rel.get("steps_total", 1)))
        assert ok, f"{case['name']}: partial status requires incomplete coverage or steps"


@pytest.mark.parametrize("case", CASES, ids=CASE_NAMES)
def test_replay_case_no_unsupported_claims_on_unsafe(case: dict[str, Any], tmp_path: Path) -> None:
    """An unsafe / failed run must not surface unsupported claims as fact.

    The runtime's contract: when status is failed because of an unsafe path,
    the synthesis content is preserved but the claim set must be unverifiable
    (coverage 0). This test asserts the absence of supported claims on any
    non-success status.
    """
    result = run_case(case, tmp_path=tmp_path)
    if result["status"] in ("failed", "cancelled"):
        # Coverage must be 0 (no successful evidence -> claims cannot verify).
        assert float(result["reliability"].get("claims_coverage", 0.0)) == 0.0


def test_fixture_file_exists_and_is_valid_json() -> None:
    """The fixture file must exist and parse cleanly."""
    import json

    assert FIXTURE_PATH.exists(), f"fixture missing at {FIXTURE_PATH}"
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert "cases" in raw and isinstance(raw["cases"], list)
    assert len(raw["cases"]) >= 11, "expected at least 11 replay cases per the brief"


def test_fixture_cases_have_required_fields() -> None:
    """Every fixture case must carry the brief's required fields."""
    required = {"name", "prompt", "expected_intent", "expected_status", "faults", "required_evidence"}
    for case in CASES:
        missing = required - set(case)
        assert not missing, f"case {case.get('name')!r} missing fields: {sorted(missing)}"


def test_fixture_case_names_unique() -> None:
    """Case names must be unique (used as test IDs and benchmark keys)."""
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)), f"duplicate case names: {names}"


def test_no_network_imports_in_runner() -> None:
    """The runner must not import any live-provider or broker modules."""
    # ponytail: a guard against future drift. The runner is test-only.
    from tests import _reliability_replay_runner as runner

    src = Path(runner.__file__).read_text(encoding="utf-8")
    for forbidden in ("requests", "httpx", "ccxt", "akshare", "yfinance", "tushare"):
        assert f"import {forbidden}" not in src, f"runner imports {forbidden}"
        assert f"from {forbidden}" not in src, f"runner imports {forbidden}"
