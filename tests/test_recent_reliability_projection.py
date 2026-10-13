"""Tests for the recent_workflow_reliability safe projection in _project_dashboard."""

import math

import pytest

from app import _project_dashboard, _safe_duration, _safe_recent_workflow_reliability


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _healthy_20_window():
    """A healthy 20-sample window with all fields valid."""
    return {
        "sample_count": 20,
        "successful": 15,
        "failed": 3,
        "active": 1,
        "excluded": 1,
        "rate": 0.8333,  # will be re-derived
        "failure_stage_counts": {
            "plan": 1,
            "implement": 1,
            "test": 1,
            "review": 0,
            "reason": 0,
            "unknown": 0,
        },
        "blocked_stage_counts": {
            "plan": 0,
            "implement": 0,
            "test": 0,
            "review": 0,
            "reason": 0,
            "unknown": 0,
        },
        "duration": {
            "sample_count": 20,
            "median_seconds": 120.5,
            "average_seconds": 130.2,
        },
        "tokens": {
            "total": 50000,
            "measured_jobs": 18,
            "missing_jobs": 2,
            "incomplete": True,
        },
    }


def _empty_none_rate():
    """Zero successful+failed => rate should be None."""
    return {
        "sample_count": 5,
        "successful": 0,
        "failed": 0,
        "active": 3,
        "excluded": 2,
        "rate": None,
        "failure_stage_counts": {},
        "blocked_stage_counts": {},
        "duration": {
            "sample_count": 5,
            "median_seconds": None,
            "average_seconds": None,
        },
        "tokens": {
            "total": None,
            "measured_jobs": 0,
            "missing_jobs": 5,
            "incomplete": True,
        },
    }


# ---------------------------------------------------------------------------
# Unit tests for _safe_recent_workflow_reliability
# ---------------------------------------------------------------------------

class TestHealthyWindow:
    def test_valid_20_window(self):
        raw = _healthy_20_window()
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["sample_count"] == 20
        assert result["successful"] == 15
        assert result["failed"] == 3
        assert result["active"] == 1
        assert result["excluded"] == 1
        # Rate derived from counts: 15/(15+3) = 0.8333...
        assert result["rate"] == pytest.approx(15 / 18)
        assert result["failure_stage_counts"]["plan"] == 1
        assert result["failure_stage_counts"]["implement"] == 1
        assert result["failure_stage_counts"]["test"] == 1
        assert result["duration"]["median_seconds"] == 120.5
        assert result["duration"]["average_seconds"] == 130.2
        assert result["tokens"]["total"] == 50000
        assert result["tokens"]["measured_jobs"] == 18
        assert result["tokens"]["missing_jobs"] == 2
        assert result["tokens"]["incomplete"] is True

    def test_rate_derived_not_trusted(self):
        """Input rate is ignored; derived from validated counts."""
        raw = _healthy_20_window()
        raw["rate"] = 0.99  # wrong, should be re-derived
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["rate"] == pytest.approx(15 / 18)


class TestEmptyNoneRate:
    def test_zero_denominator_rate_none(self):
        raw = _empty_none_rate()
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["rate"] is None
        assert result["tokens"]["total"] is None
        assert result["tokens"]["incomplete"] is True


class TestMissingInput:
    def test_none_input(self):
        assert _safe_recent_workflow_reliability(None) is None

    def test_non_dict_input(self):
        assert _safe_recent_workflow_reliability("string") is None
        assert _safe_recent_workflow_reliability(42) is None
        assert _safe_recent_workflow_reliability([1, 2]) is None

    def test_missing_sample_count(self):
        raw = _healthy_20_window()
        del raw["sample_count"]
        assert _safe_recent_workflow_reliability(raw) is None

    def test_missing_counts(self):
        raw = _healthy_20_window()
        del raw["successful"]
        assert _safe_recent_workflow_reliability(raw) is None


class TestInvalidBoolAsInt:
    def test_bool_sample_count(self):
        raw = _healthy_20_window()
        raw["sample_count"] = True
        assert _safe_recent_workflow_reliability(raw) is None

    def test_bool_successful(self):
        raw = _healthy_20_window()
        raw["successful"] = False
        assert _safe_recent_workflow_reliability(raw) is None


class TestInvalidFraction:
    def test_rate_out_of_range(self):
        """If counts are inconsistent with rate, we still derive from counts.
        But if counts themselves are invalid, reject."""
        raw = _healthy_20_window()
        # Make counts inconsistent: s+f+a+e != sample_count
        raw["successful"] = 25
        assert _safe_recent_workflow_reliability(raw) is None


class TestNaN:
    def test_nan_sample_count(self):
        raw = _healthy_20_window()
        raw["sample_count"] = float("nan")
        assert _safe_recent_workflow_reliability(raw) is None

    def test_nan_duration(self):
        raw = _healthy_20_window()
        raw["duration"]["median_seconds"] = float("nan")
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["duration"]["median_seconds"] is None


class TestInfinity:
    def test_infinity_duration(self):
        raw = _healthy_20_window()
        raw["duration"]["average_seconds"] = float("inf")
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["duration"]["average_seconds"] is None


class TestNegative:
    def test_negative_successful(self):
        raw = _healthy_20_window()
        raw["successful"] = -1
        assert _safe_recent_workflow_reliability(raw) is None

    def test_negative_sample_count(self):
        raw = _healthy_20_window()
        raw["sample_count"] = -5
        assert _safe_recent_workflow_reliability(raw) is None


class TestHighCounts:
    def test_oversize_sample_count(self):
        raw = _healthy_20_window()
        raw["sample_count"] = 21
        assert _safe_recent_workflow_reliability(raw) is None

    def test_oversize_successful(self):
        raw = _healthy_20_window()
        raw["successful"] = 100
        # 100 + 3 + 1 + 1 != 20
        assert _safe_recent_workflow_reliability(raw) is None


class TestInconsistentTally:
    def test_totals_not_equal_sample(self):
        raw = _healthy_20_window()
        raw["successful"] = 16  # 16+3+1+1=21 != 20
        assert _safe_recent_workflow_reliability(raw) is None


class TestMaliciousField:
    def test_extra_keys_dropped(self):
        raw = _healthy_20_window()
        raw["prompts"] = ["secret prompt"]
        raw["paths"] = ["/etc/passwd"]
        raw["api_keys"] = ["sk-123"]
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert "prompts" not in result
        assert "paths" not in result
        assert "api_keys" not in result

    def test_unknown_stage_key_dropped(self):
        raw = _healthy_20_window()
        raw["failure_stage_counts"]["deploy"] = 5
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert "deploy" not in result["failure_stage_counts"]


class TestUnknownStagePath:
    def test_unknown_stage_in_blocked(self):
        raw = _healthy_20_window()
        raw["blocked_stage_counts"]["mystery"] = 3
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert "mystery" not in result["blocked_stage_counts"]


class TestTokens:
    def test_measured_zero_total_none(self):
        raw = _healthy_20_window()
        raw["tokens"] = {
            "total": 1000,
            "measured_jobs": 0,
            "missing_jobs": 20,
            "incomplete": True,
        }
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["tokens"]["total"] is None
        assert result["tokens"]["incomplete"] is True

    def test_measured_positive_zero_total_allowed(self):
        raw = _healthy_20_window()
        raw["tokens"] = {
            "total": 0,
            "measured_jobs": 5,
            "missing_jobs": 15,
            "incomplete": True,
        }
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["tokens"]["total"] == 0
        assert result["tokens"]["incomplete"] is True

    def test_no_missing_incomplete_false(self):
        raw = _healthy_20_window()
        raw["tokens"] = {
            "total": 50000,
            "measured_jobs": 20,
            "missing_jobs": 0,
            "incomplete": False,
        }
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["tokens"]["incomplete"] is False

    def test_missing_tokens_field(self):
        raw = _healthy_20_window()
        del raw["tokens"]
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["tokens"] is None

    def test_tokens_no_zero_fallback(self):
        """If tokens dict is present but total is missing, total should be None not 0."""
        raw = _healthy_20_window()
        raw["tokens"] = {
            "measured_jobs": 10,
            "missing_jobs": 10,
            "incomplete": True,
        }
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["tokens"]["total"] is None


class TestDuration:
    def test_missing_duration(self):
        raw = _healthy_20_window()
        del raw["duration"]
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["duration"] is None

    def test_duration_no_zero_fallback(self):
        """If median_seconds is missing, it should be None not 0."""
        raw = _healthy_20_window()
        raw["duration"] = {
            "sample_count": 20,
            "average_seconds": 100.0,
        }
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["duration"]["median_seconds"] is None
        assert result["duration"]["average_seconds"] == 100.0

    def test_duration_negative(self):
        raw = _healthy_20_window()
        raw["duration"]["median_seconds"] = -1.0
        result = _safe_recent_workflow_reliability(raw)
        assert result is not None
        assert result["duration"]["median_seconds"] is None


# ---------------------------------------------------------------------------
# Route-level projection tests
# ---------------------------------------------------------------------------

class TestProjectDashboard:
    def test_raw_extras_removed(self):
        """The projection must strip unknown keys from recent_workflow_reliability."""
        raw = {
            "all_time_counts": {"total": 100},
            "recent_workflow_reliability": {
                "sample_count": 10,
                "successful": 8,
                "failed": 1,
                "active": 1,
                "excluded": 0,
                "rate": 0.8,
                "failure_stage_counts": {"plan": 1},
                "blocked_stage_counts": {},
                "duration": {"sample_count": 10, "median_seconds": 5.0, "average_seconds": 6.0},
                "tokens": {"total": 1000, "measured_jobs": 10, "missing_jobs": 0, "incomplete": False},
                "prompts": ["malicious"],
                "paths": ["/etc/shadow"],
                "api_keys": ["sk-abc"],
            },
        }
        result = _project_dashboard(raw)
        rel = result["recent_workflow_reliability"]
        assert rel is not None
        assert "prompts" not in rel
        assert "paths" not in rel
        assert "api_keys" not in rel
        # Other keys unchanged
        assert result["all_time_counts"] == {"total": 100}

    def test_other_keys_unchanged(self):
        """Existing fields pass through unchanged."""
        raw = {
            "all_time_counts": {"total": 50, "successful": 40},
            "repair_enabled": True,
            "recent_workflows": [],
            "recent_workflow_reliability": _healthy_20_window(),
        }
        result = _project_dashboard(raw)
        assert result["all_time_counts"] == {"total": 50, "successful": 40}
        assert result["repair_enabled"] is True
        assert result["recent_workflows"] == []
        assert result["recent_workflow_reliability"] is not None

    def test_missing_reliability_field(self):
        """If the gateway doesn't send the field, result gets None."""
        raw = {
            "all_time_counts": {"total": 10},
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        assert result["recent_workflow_reliability"] is None

    def test_malformed_reliability_field(self):
        """Malformed input yields None in the result."""
        raw = {
            "all_time_counts": {"total": 10},
            "recent_workflow_reliability": "garbage",
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        assert result["recent_workflow_reliability"] is None

    def test_reliability_overrides_raw_passthrough(self):
        """Even though result=dict(raw), the projected value replaces the raw one."""
        raw = {
            "recent_workflow_reliability": {
                "sample_count": 5,
                "successful": 3,
                "failed": 1,
                "active": 1,
                "excluded": 0,
                "secret_field": "should_not_appear",
            },
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        rel = result["recent_workflow_reliability"]
        assert rel is not None
        assert "secret_field" not in rel
        # The raw dict in result should also be the projected version
        assert result["recent_workflow_reliability"] == rel


# ---------------------------------------------------------------------------
# Focused tests: _safe_duration overflow correction
# ---------------------------------------------------------------------------

class TestSafeDurationOverflow:
    """Verify _safe_duration rejects huge ints BEFORE any float conversion."""

    def test_huge_int_2_pow_4096_returns_none(self):
        """2**4096 must not raise OverflowError; must return None."""
        raw = {"sample_count": 5, "median_seconds": 2**4096, "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None
        assert result["average_seconds"] == 1.0

    def test_negative_huge_int_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": -(2**4096), "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_nan_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": float("nan"), "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_inf_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": float("inf"), "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_neg_inf_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": float("-inf"), "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_bool_true_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": True, "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_bool_false_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": False, "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_string_returns_none(self):
        raw = {"sample_count": 5, "median_seconds": "120", "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None

    def test_valid_zero_preserved(self):
        raw = {"sample_count": 5, "median_seconds": 0, "average_seconds": 0.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] == 0
        assert result["average_seconds"] == 0.0

    def test_valid_fraction_preserved(self):
        raw = {"sample_count": 5, "median_seconds": 1.5, "average_seconds": 2.5}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] == 1.5
        assert result["average_seconds"] == 2.5

    def test_valid_60_preserved(self):
        raw = {"sample_count": 5, "median_seconds": 60, "average_seconds": 60.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] == 60
        assert result["average_seconds"] == 60.0

    def test_none_preserved(self):
        raw = {"sample_count": 5, "median_seconds": None, "average_seconds": None}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None
        assert result["average_seconds"] is None

    def test_js_safe_int_max_boundary(self):
        """Exactly _JS_SAFE_INT_MAX is valid; one above is rejected."""
        js_max = 2**53 - 1
        raw = {"sample_count": 5, "median_seconds": js_max, "average_seconds": 1.0}
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] == js_max

        raw2 = {"sample_count": 5, "median_seconds": js_max + 1, "average_seconds": 1.0}
        result2 = _safe_duration(raw2)
        assert result2 is not None
        assert result2["median_seconds"] is None


class TestSafeDurationIntegration:
    """Projection with huge duration: summary remains valid, no 500."""

    def test_huge_duration_in_reliability_still_valid_summary(self):
        raw = {
            "all_time_counts": {"total": 10},
            "recent_workflow_reliability": {
                "sample_count": 10,
                "successful": 8,
                "failed": 1,
                "active": 1,
                "excluded": 0,
                "rate": 0.8,
                "failure_stage_counts": {"plan": 1},
                "blocked_stage_counts": {},
                "duration": {
                    "sample_count": 10,
                    "median_seconds": 2**4096,
                    "average_seconds": 5.0,
                },
                "tokens": {"total": 1000, "measured_jobs": 10, "missing_jobs": 0, "incomplete": False},
            },
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        rel = result["recent_workflow_reliability"]
        assert rel is not None
        assert rel["sample_count"] == 10
        assert rel["successful"] == 8
        assert rel["duration"]["median_seconds"] is None
        assert rel["duration"]["average_seconds"] == 5.0
        # No exception raised, no 500

    def test_huge_duration_no_exception(self):
        """Direct call must not raise any exception."""
        raw = {
            "sample_count": 10,
            "median_seconds": 2**4096,
            "average_seconds": 2**4096,
        }
        result = _safe_duration(raw)
        assert result is not None
        assert result["median_seconds"] is None
        assert result["average_seconds"] is None
