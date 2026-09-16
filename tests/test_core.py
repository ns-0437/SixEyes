"""Unit tests for domain primitives. Money and hashing are load-bearing for every dollar
figure this product will ever put in front of a customer, so they get direct coverage
beyond what the graph tests exercise incidentally."""

from __future__ import annotations

import pytest

from sixeyes.core.ids import content_hash, is_digest
from sixeyes.core.types import (
    Confidence, Evidence, Finding, FindingSet, Provenance, Remediation, Severity,
)
from sixeyes.core.units import Money


class TestMoney:
    def test_construction_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            Money(3.14)  # type: ignore[arg-type]

    def test_from_usd_is_exact_for_cents(self) -> None:
        assert Money.from_usd("0.01").micros == 10_000

    def test_addition_and_subtraction(self) -> None:
        assert Money.from_usd(1) + Money.from_usd(2) == Money.from_usd(3)
        assert Money.from_usd(5) - Money.from_usd(2) == Money.from_usd(3)

    def test_scale_is_exact_and_does_not_lose_pennies_via_float(self) -> None:
        # 10.00 split 1/3 : 2/3 must sum back to the whole under integer scaling.
        whole = Money.from_usd(10)
        a = whole.scale(1, 3)
        b = whole.scale(2, 3)
        assert a.micros + b.micros <= whole.micros
        assert whole.micros - (a.micros + b.micros) < 3  # only integer-division remainder lost

    def test_scale_rejects_zero_denominator(self) -> None:
        with pytest.raises(ZeroDivisionError):
            Money.from_usd(1).scale(1, 0)

    def test_multiply_rejects_float_factor(self) -> None:
        with pytest.raises(TypeError):
            Money.from_usd(1) * 1.5  # type: ignore[operator]

    def test_ordering(self) -> None:
        assert Money.from_usd(1) < Money.from_usd(2)
        assert sorted([Money.from_usd(3), Money.from_usd(1)]) == [
            Money.from_usd(1), Money.from_usd(3)
        ]

    def test_str_renders_two_decimals(self) -> None:
        assert str(Money.from_usd(1234.5)) == "$1,234.50"


class TestContentHash:
    def test_deterministic_across_calls(self) -> None:
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    def test_produces_a_valid_digest(self) -> None:
        assert is_digest(content_hash("x"))

    def test_nested_structures_are_stable(self) -> None:
        a = content_hash({"x": [1, {"y": 2}], "z": (3, 4)})
        b = content_hash({"z": (3, 4), "x": [1, {"y": 2}]})
        assert a == b

    def test_distinguishes_types_that_stringify_the_same(self) -> None:
        assert content_hash(1) != content_hash("1")
        assert content_hash(True) != content_hash(1)
        assert content_hash(None) != content_hash("None")

    def test_distinguishes_nesting_shape(self) -> None:
        assert content_hash({"a": 1}) != content_hash([("a", 1)])

    def test_sets_are_order_independent(self) -> None:
        assert content_hash({1, 2, 3}) == content_hash({3, 2, 1})

    def test_frames_parts_unambiguously(self) -> None:
        """Regression (independent review, 2026-09-16): a NUL-delimited encoding let a
        single string containing an embedded NUL byte serialise identically to two
        separate arguments -- content_hash("a\\x00sb") == content_hash("a", "b"), a real,
        exploitable collision, not a cryptographic one. Fixed by length-framing every
        sub-value (see core/ids.py's `_frame`) instead of joining with a delimiter."""
        assert content_hash("a\x00sb") != content_hash("a", "b")
        # general injectivity sanity checks in the same family, for containers
        assert content_hash(["ab", "c"]) != content_hash(["a", "bc"])
        assert content_hash(["a"]) != content_hash(["a", ""])


class TestFinding:
    def _finding(self, **overrides: object) -> Finding:
        base = dict(
            detector="d",
            title="t",
            severity=Severity.LOW,
            confidence=Confidence.DERIVED,
            recoverable=Money.from_usd(1),
            evidence=Evidence(kind="k", locator="l"),
            remediation=Remediation(summary="s"),
            workload_id="w",
        )
        base.update(overrides)
        return Finding(**base)  # type: ignore[arg-type]

    def test_negative_recoverable_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._finding(recoverable=Money(-1))

    def test_blank_title_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._finding(title="   ")

    def test_id_is_stable_for_identical_evidence(self) -> None:
        f1 = self._finding()
        f2 = self._finding()
        assert f1.id == f2.id, "identical findings across runs must dedupe by id"

    def test_id_changes_when_evidence_differs(self) -> None:
        f1 = self._finding(evidence=Evidence(kind="k", locator="l"))
        f2 = self._finding(evidence=Evidence(kind="k", locator="different"))
        assert f1.id != f2.id

    def test_speculative_confidence_is_never_certified(self) -> None:
        f = self._finding(confidence=Confidence.SPECULATIVE)
        assert not f.is_certified

    def test_measured_confidence_on_clean_path_is_certified(self) -> None:
        f = self._finding(confidence=Confidence.MEASURED)
        assert f.is_certified


class TestFindingSet:
    def test_certified_total_excludes_uncertified(self) -> None:
        certified = Finding(
            detector="d", title="a", severity=Severity.LOW,
            confidence=Confidence.DERIVED, recoverable=Money.from_usd(100),
            evidence=Evidence("k", "l1"), remediation=Remediation("s"), workload_id="w",
        )
        speculative = Finding(
            detector="d", title="b", severity=Severity.LOW,
            confidence=Confidence.SPECULATIVE, recoverable=Money.from_usd(9999),
            evidence=Evidence("k", "l2"), remediation=Remediation("s"), workload_id="w",
        )
        fs = FindingSet((certified, speculative))
        assert fs.certified_total == Money.from_usd(100)
        assert len(fs) == 2
