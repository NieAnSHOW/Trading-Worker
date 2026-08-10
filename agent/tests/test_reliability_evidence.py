"""Tests for the reliability evidence verifier (Task 5)."""

from __future__ import annotations

import json
from pathlib import Path

from src.reliability.contracts import (
    EvidenceRef,
    StepResult,
    StepStatus,
)
from src.reliability.evidence import Claim, ClaimKind, EvidenceVerifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _success(step_id: str = "s1", evidence: tuple[EvidenceRef, ...] = ()) -> StepResult:
    return StepResult(
        step_id=step_id,
        status=StepStatus.SUCCESS,
        data={"ok": True},
        evidence=evidence,
        provider="yfinance",
        elapsed_ms=10,
    )


# ---------------------------------------------------------------------------
# verify_step_result — artifact refs
# ---------------------------------------------------------------------------

def test_valid_artifact_ref_keeps_success(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text('{"k": 1}', encoding="utf-8")
    ref = EvidenceRef(source_type="artifact", source_id="report.json")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is StepStatus.SUCCESS


def test_missing_artifact_ref_downgrades(tmp_path: Path) -> None:
    ref = EvidenceRef(source_type="artifact", source_id="does_not_exist.json")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS
    assert graded.status in (StepStatus.PARTIAL, StepStatus.BLOCKED)


def test_run_dir_none_artifact_ref_downgrades(tmp_path: Path) -> None:
    # Without a run_dir, artifact refs cannot be contained/resolved.
    ref = EvidenceRef(source_type="artifact", source_id="report.json")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=None)
    assert graded.status is not StepStatus.SUCCESS


# ---------------------------------------------------------------------------
# verify_step_result — metric fields
# ---------------------------------------------------------------------------

def test_metric_field_present_keeps_success(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"sharpe": 1.2, "sortino": 1.8}), encoding="utf-8")
    ref = EvidenceRef(source_type="metric", source_id="metrics.json", field="sharpe")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is StepStatus.SUCCESS


def test_missing_metric_field_downgrades(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"sharpe": 1.2}), encoding="utf-8")
    ref = EvidenceRef(source_type="metric", source_id="metrics.json", field="sortino")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


def test_missing_metric_file_downgrades(tmp_path: Path) -> None:
    ref = EvidenceRef(source_type="metric", source_id="metrics.json", field="sharpe")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


def test_malformed_metric_json_downgrades(tmp_path: Path) -> None:
    (tmp_path / "metrics.json").write_text("{ not json", encoding="utf-8")
    ref = EvidenceRef(source_type="metric", source_id="metrics.json", field="sharpe")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


def test_metric_csv_field_present_keeps_success(tmp_path: Path) -> None:
    (tmp_path / "metrics.csv").write_text("sharpe,sortino\n1.1,1.9\n", encoding="utf-8")
    ref = EvidenceRef(source_type="metric", source_id="metrics.csv", field="sharpe")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is StepStatus.SUCCESS


# ---------------------------------------------------------------------------
# verify_step_result — stale as_of
# ---------------------------------------------------------------------------

def test_stale_as_of_downgrades(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    ref = EvidenceRef(
        source_type="artifact",
        source_id="report.json",
        as_of="2000-01-01",  # clearly stale
    )
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


def test_fresh_as_of_keeps_success(tmp_path: Path) -> None:
    from datetime import date

    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    ref = EvidenceRef(
        source_type="artifact",
        source_id="report.json",
        as_of=date.today().isoformat(),
    )
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is StepStatus.SUCCESS


# ---------------------------------------------------------------------------
# verify_step_result — path containment
# ---------------------------------------------------------------------------

def test_path_traversal_ref_rejected(tmp_path: Path) -> None:
    # Create a file outside run_dir that the ref tries to reach via ../
    outside = tmp_path.parent / "secret.txt"
    try:
        outside.write_text("secret", encoding="utf-8")
    except OSError:
        # If we can't write to the parent, skip cleanup concerns.
        outside = None

    ref = EvidenceRef(source_type="artifact", source_id="../secret.txt")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS

    if outside and outside.exists():
        outside.unlink()


def test_absolute_path_outside_run_dir_rejected(tmp_path: Path) -> None:
    ref = EvidenceRef(source_type="artifact", source_id="/etc/passwd")
    result = _success(evidence=(ref,))

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


# ---------------------------------------------------------------------------
# verify_step_result — never upgrade; respect existing non-success states
# ---------------------------------------------------------------------------

def test_partial_not_upgraded_to_success(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text("{}", encoding="utf-8")
    ref = EvidenceRef(source_type="artifact", source_id="report.json")
    result = StepResult(
        step_id="s1",
        status=StepStatus.PARTIAL,
        data={"ok": True},
        evidence=(ref,),
        provider="yfinance",
    )

    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is not StepStatus.SUCCESS


def test_blocked_not_upgraded(tmp_path: Path) -> None:
    result = StepResult(step_id="s1", status=StepStatus.BLOCKED)
    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    assert graded.status is StepStatus.BLOCKED


def test_success_without_evidence_downgrades() -> None:
    # A success claim with zero evidence refs cannot be substantiated.
    result = _success()
    graded = EvidenceVerifier().verify_step_result(result, run_dir=None)
    assert graded.status is not StepStatus.SUCCESS


def test_error_step_unaffected(tmp_path: Path) -> None:
    from src.reliability.contracts import ErrorCode, ToolError

    result = StepResult(
        step_id="s1",
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.UNKNOWN, message="boom", retryable=True),
    )
    graded = EvidenceVerifier().verify_step_result(result, run_dir=tmp_path)
    # Error states are not re-graded on evidence.
    assert graded.status is StepStatus.RECOVERABLE_ERROR


# ---------------------------------------------------------------------------
# verify_claim
# ---------------------------------------------------------------------------

def test_fact_claim_with_successful_evidence_verified(tmp_path: Path) -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    step = _success(step_id="s1", evidence=(ref,))
    claim = Claim(text="price exists", kind=ClaimKind.FACT, evidence=[ref])

    assert EvidenceVerifier().verify_claim(claim, [step]) is True


def test_fact_claim_without_supporting_step_rejected() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    # No step in evidence carries this ref.
    other_ref = EvidenceRef(source_type="artifact", source_id="other.json")
    step = _success(step_id="s1", evidence=(other_ref,))
    claim = Claim(text="price exists", kind=ClaimKind.FACT, evidence=[ref])

    assert EvidenceVerifier().verify_claim(claim, [step]) is False


def test_fact_claim_against_failed_step_rejected() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    from src.reliability.contracts import ErrorCode, ToolError

    step = StepResult(
        step_id="s1",
        status=StepStatus.RECOVERABLE_ERROR,
        error=ToolError(code=ErrorCode.UNKNOWN, message="x", retryable=True),
        evidence=(ref,),
    )
    claim = Claim(text="price exists", kind=ClaimKind.FACT, evidence=[ref])

    assert EvidenceVerifier().verify_claim(claim, [step]) is False


def test_fact_claim_with_no_evidence_refs_rejected() -> None:
    # "Unsupported numerical claim": asserts a fact with no backing refs.
    claim = Claim(text="PE is 12.5", kind=ClaimKind.FACT, evidence=[])
    assert EvidenceVerifier().verify_claim(claim, [_success()]) is False


def test_derived_claim_requires_source_field() -> None:
    # DERIVED with a successful backing step but field=None → unsupported.
    ref_no_field = EvidenceRef(source_type="artifact", source_id="r.json")
    step = _success(step_id="s1", evidence=(ref_no_field,))
    claim = Claim(
        text="excess return is 3%",
        kind=ClaimKind.DERIVED,
        evidence=[ref_no_field],
    )
    assert EvidenceVerifier().verify_claim(claim, [step]) is False


def test_derived_claim_with_field_verified() -> None:
    ref = EvidenceRef(source_type="metric", source_id="metrics.json", field="excess_return")
    step = _success(step_id="s1", evidence=(ref,))
    claim = Claim(
        text="excess return is 3%",
        kind=ClaimKind.DERIVED,
        evidence=[ref],
    )
    assert EvidenceVerifier().verify_claim(claim, [step]) is True


def test_interpretation_claim_without_premises_rejected() -> None:
    claim = Claim(
        text="likely uptrend",
        kind=ClaimKind.INTERPRETATION,
        evidence=[],
    )
    assert EvidenceVerifier().verify_claim(claim, [_success()]) is False


def test_interpretation_claim_with_premises_verified() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    step = _success(step_id="s1", evidence=(ref,))
    claim = Claim(
        text="likely uptrend",
        kind=ClaimKind.INTERPRETATION,
        evidence=[ref],
    )
    assert EvidenceVerifier().verify_claim(claim, [step]) is True


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def test_coverage_all_verified() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    step = _success(step_id="s1", evidence=(ref,))
    claims = [
        Claim(text="a", kind=ClaimKind.FACT, evidence=[ref]),
        Claim(text="b", kind=ClaimKind.FACT, evidence=[ref]),
    ]
    assert EvidenceVerifier().coverage(claims, [step]) == 1.0


def test_coverage_half_verified() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    other = EvidenceRef(source_type="artifact", source_id="other.json")
    step = _success(step_id="s1", evidence=(ref,))
    claims = [
        Claim(text="a", kind=ClaimKind.FACT, evidence=[ref]),
        Claim(text="b", kind=ClaimKind.FACT, evidence=[other]),
    ]
    assert EvidenceVerifier().coverage(claims, [step]) == 0.5


def test_coverage_empty_claims_returns_zero() -> None:
    assert EvidenceVerifier().coverage([], [_success()]) == 0.0


def test_coverage_no_evidence_returns_zero() -> None:
    ref = EvidenceRef(source_type="artifact", source_id="r.json")
    claims = [Claim(text="a", kind=ClaimKind.FACT, evidence=[ref])]
    assert EvidenceVerifier().coverage(claims, []) == 0.0
