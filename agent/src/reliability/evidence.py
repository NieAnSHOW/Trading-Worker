"""Evidence and claim verification for the reliability runtime.

The verifier re-grades a :class:`StepResult` by resolving its
:class:`EvidenceRef`\\ s against artifacts and metric files under an approved
``run_dir``. Missing, malformed, stale, or out-of-bounds evidence can only
*downgrade* a result (towards ``partial``/``blocked``) — it never upgrades a
non-success result to ``success``.

Security property: artifact and metric paths are resolved strictly under the
approved ``run_dir``. Path traversal (``../``) and absolute paths outside
``run_dir`` are rejected, so a claim cannot pull arbitrary filesystem objects.

Redaction property: this module never returns raw file contents or user data
— only booleans, short codes, and references.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from pydantic import BaseModel

from src.reliability.contracts import EvidenceRef, StepResult, StepStatus

# ponytail: 30-day recency window for as_of. Left as a constant rather than
# config because the verifier runs per-step at runtime.
_AS_OF_STALE_DAYS = timedelta(days=30)

# Source types that point at on-disk artifacts (path-resolved).
_ARTIFACT_SOURCE_TYPES = frozenset({"artifact", "file", "document"})
# Source types that point at parseable metric files (JSON/CSV) carrying named
# fields.
_METRIC_SOURCE_TYPES = frozenset({"metric", "metrics"})


class ClaimKind(str, Enum):
    FACT = "fact"
    DERIVED = "derived"
    INTERPRETATION = "interpretation"


class Claim(BaseModel):
    text: str
    kind: ClaimKind
    evidence: list[EvidenceRef]


def _is_stale(as_of: str | None) -> bool:
    """True when ``as_of`` is explicitly set, parseable, and older than the
    recency threshold.

    A missing ``as_of`` is treated as NOT stale — many artifact refs simply
    don't carry a date. Only an explicitly stale date downgrades a ref.
    A future date is accepted (clock-skew tolerant).
    """
    if not as_of:
        return False
    try:
        parsed = datetime.fromisoformat(as_of)
    except ValueError:
        try:
            parsed = date.fromisoformat(as_of)
        except ValueError:
            return True
    if isinstance(parsed, date) and not isinstance(parsed, datetime):
        parsed = datetime.combine(parsed, datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - parsed) > _AS_OF_STALE_DAYS


def _contains(run_dir: Path, candidate: Path) -> bool:
    """True when ``candidate`` resolves to a path inside ``run_dir``."""
    try:
        candidate.relative_to(run_dir)
        return True
    except ValueError:
        return False


def _resolve_under(run_dir: Path, source_id: str) -> Path | None:
    """Resolve ``source_id`` strictly under ``run_dir``.

    Returns the resolved absolute path if it is contained within
    ``run_dir``, else ``None``. Rejects absolute paths outside ``run_dir``
    and any traversal that escapes the approved directory.
    """
    if not source_id:
        return None
    candidate = (run_dir / source_id).resolve()
    run_dir_resolved = run_dir.resolve()
    if not _contains(run_dir_resolved, candidate):
        return None
    return candidate


def _load_metric_field(path: Path, field: str | None) -> bool:
    """True when ``path`` is a parseable JSON/CSV metric file that contains
    ``field`` as a top-level key.

    ponytail: a tiny local parser. The existing ``_load_metrics`` helper in
    ``src/shadow_account/backtester.py`` is private and dict-of-artifacts
    shaped; reusing it would couple the verifier to shadow-account concerns
    and require a wider refactor. This parser only reads what we need: top-
    level field presence in JSON, last-row column in CSV.
    """
    try:
        if path.suffix == ".json":
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                return field is not None and field in raw
            return False
        if path.suffix in (".csv", ".tsv"):
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
            if not header:
                return False
            return field in header
    except (OSError, ValueError):
        return False
    return False


class EvidenceVerifier:
    """Re-grade step results by resolving their evidence refs, and check
    that claims are backed by successful evidence.

    The verifier is fail-closed: a step whose evidence cannot be resolved is
    never graded ``success``.
    """

    def verify_step_result(
        self,
        result: StepResult,
        run_dir: Path | None = None,
    ) -> StepResult:
        """Re-grade ``result`` by resolving its ``EvidenceRef``\\ s.

        - Non-success terminal states (BLOCKED, RECOVERABLE_ERROR,
          UNSAFE_ERROR, CANCELLED) are returned unchanged — evidence cannot
          repair a step that already failed.
        - For SUCCESS/PARTIAL results, every evidence ref must resolve under
          ``run_dir``; otherwise the result is downgraded. SUCCESS with no
          evidence refs at all is downgraded to PARTIAL (unverifiable claim).
        """
        # Never upgrade a non-success terminal state.
        if result.status in {
            StepStatus.BLOCKED,
            StepStatus.RECOVERABLE_ERROR,
            StepStatus.UNSAFE_ERROR,
            StepStatus.CANCELLED,
        }:
            return result

        if not result.evidence:
            # Nothing to back the claim: downgrade success/partial to partial.
            if result.status is StepStatus.SUCCESS:
                return _with_status(result, StepStatus.PARTIAL)
            return result

        if run_dir is None:
            # Artifact/metric refs require a run_dir to be contained.
            return self._downgrade(result)

        all_ok = True
        any_ok = False
        for ref in result.evidence:
            ok = self._ref_resolves(ref, run_dir)
            all_ok = all_ok and ok
            any_ok = any_ok or ok

        if not any_ok:
            return self._downgrade(result, to_blocked=True)
        if not all_ok and result.status is StepStatus.SUCCESS:
            return _with_status(result, StepStatus.PARTIAL)
        if not all_ok:
            # Already PARTIAL: stay there.
            return result
        # All refs resolve. SUCCESS stays SUCCESS; PARTIAL stays PARTIAL
        # (we never upgrade). RECOVERABLE_ERROR without error is impossible
        # by StepResult invariant; fall through to return unchanged.
        return result

    def _ref_resolves(self, ref: EvidenceRef, run_dir: Path) -> bool:
        """True when ``ref`` points to a real, well-formed, non-stale artifact
        or metric field under ``run_dir``.
        """
        if _is_stale(ref.as_of):
            return False

        if ref.source_type in _ARTIFACT_SOURCE_TYPES:
            path = _resolve_under(run_dir, ref.source_id)
            if path is None or not path.exists() or not path.is_file():
                return False
            return True

        if ref.source_type in _METRIC_SOURCE_TYPES:
            path = _resolve_under(run_dir, ref.source_id)
            if path is None or not path.exists() or not path.is_file():
                return False
            return _load_metric_field(path, ref.field)

        # Unknown source types: fail-closed.
        return False

    def _downgrade(self, result: StepResult, *, to_blocked: bool = False) -> StepResult:
        if result.status is StepStatus.SUCCESS or to_blocked:
            new = StepStatus.BLOCKED if to_blocked else StepStatus.PARTIAL
            return _with_status(result, new)
        return result

    def verify_claim(
        self,
        claim: Claim,
        evidence: Sequence[StepResult],
    ) -> bool:
        """True when ``claim`` is backed by successful evidence.

        - FACT and INTERPRETATION require ≥1 ref in ``claim.evidence`` that
          is also carried by a SUCCESS StepResult in ``evidence``.
          INTERPRETATION is unsupported when its premises are absent.
        - DERIVED additionally requires the backing ref to declare a source
          ``field`` (named column / metric key) — derived values must trace
          to a concrete source field, not a whole artifact.
        """
        if not claim.evidence:
            return False

        successful_steps = [s for s in evidence if s.status is StepStatus.SUCCESS]
        if not successful_steps:
            return False

        # Index refs carried by successful steps.
        success_refs: set[EvidenceRef] = set()
        for step in successful_steps:
            success_refs.update(step.evidence)

        for ref in claim.evidence:
            if ref not in success_refs:
                continue
            if claim.kind is ClaimKind.DERIVED and not ref.field:
                # DERIVED needs a concrete source field on this backing ref.
                continue
            return True

        return False

    def coverage(
        self,
        claims: Sequence[Claim],
        evidence: Sequence[StepResult],
    ) -> float:
        """Fraction of ``claims`` whose premises verify against ``evidence``.

        Returns 0.0 when ``claims`` is empty (nothing to cover).
        """
        if not claims:
            return 0.0
        verified = sum(1 for c in claims if self.verify_claim(c, evidence))
        return verified / len(claims)


def _with_status(result: StepResult, new_status: StepStatus) -> StepResult:
    """Return a copy of ``result`` with ``status`` replaced.

    StepResult is a frozen dataclass; rebuild it verbatim. Keeps the original
    error, data, evidence tuple, provider, and elapsed_ms untouched.
    """
    return StepResult(
        step_id=result.step_id,
        status=new_status,
        data=result.data,
        error=result.error,
        evidence=result.evidence,
        provider=result.provider,
        elapsed_ms=result.elapsed_ms,
    )


__all__ = ["Claim", "ClaimKind", "EvidenceVerifier"]
