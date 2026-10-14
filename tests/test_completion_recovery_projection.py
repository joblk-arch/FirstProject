"""Tests for the completion-recovery summary projection and dashboard wiring."""
import pytest

from app import _safe_completion_recovery_summary, _project_dashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_summary(**overrides):
    """Return a valid completion-recovery summary dict with invariants holding."""
    base = {
        "sample_count": 15,
        "implementer_jobs": 15,
        "observed_jobs": 15,
        "unknown_jobs": 0,
        "attempted_jobs": 5,
        "recovered_jobs": 5,
        "failed_jobs": 0,
        "not_attempted_jobs": 10,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _safe_completion_recovery_summary – valid inputs
# ---------------------------------------------------------------------------

class TestValidSummary:
    def test_all_recovered(self):
        raw = _valid_summary()
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["sample_count"] == 15
        assert result["implementer_jobs"] == 15
        assert result["observed_jobs"] == 15
        assert result["unknown_jobs"] == 0
        assert result["attempted_jobs"] == 5
        assert result["recovered_jobs"] == 5
        assert result["failed_jobs"] == 0
        assert result["not_attempted_jobs"] == 10
        assert result["recovery_success_rate"] == 1.0
        assert result["incomplete"] is False

    def test_no_unknown(self):
        """observed=15, unknown=0, not_attempted=10, impl=15 – all invariants hold."""
        raw = _valid_summary(
            observed_jobs=15,
            unknown_jobs=0,
            not_attempted_jobs=10,
            implementer_jobs=15,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["observed_jobs"] == 15
        assert result["unknown_jobs"] == 0
        assert result["not_attempted_jobs"] == 10
        assert result["implementer_jobs"] == 15

    def test_partial_recovery(self):
        raw = _valid_summary(
            attempted_jobs=5,
            recovered_jobs=3,
            failed_jobs=2,
            not_attempted_jobs=10,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["recovery_success_rate"] == pytest.approx(0.6)

    def test_all_failed_rate_zero(self):
        raw = _valid_summary(
            attempted_jobs=5,
            recovered_jobs=0,
            failed_jobs=5,
            not_attempted_jobs=10,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["recovery_success_rate"] == 0.0

    def test_zero_attempts_rate_none(self):
        raw = _valid_summary(
            attempted_jobs=0,
            recovered_jobs=0,
            failed_jobs=0,
            not_attempted_jobs=15,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["recovery_success_rate"] is None

    def test_unknown_jobs_incomplete(self):
        raw = _valid_summary(
            observed_jobs=10,
            unknown_jobs=5,
            implementer_jobs=15,
            attempted_jobs=5,
            recovered_jobs=3,
            failed_jobs=2,
            not_attempted_jobs=5,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["incomplete"] is True

    def test_sample_count_zero_all_zero(self):
        raw = _valid_summary(
            sample_count=0,
            implementer_jobs=0,
            observed_jobs=0,
            unknown_jobs=0,
            attempted_jobs=0,
            recovered_jobs=0,
            failed_jobs=0,
            not_attempted_jobs=0,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["sample_count"] == 0
        assert result["recovery_success_rate"] is None

    def test_sample_count_max_20(self):
        raw = _valid_summary(
            sample_count=20,
            implementer_jobs=20,
            observed_jobs=20,
            unknown_jobs=0,
            attempted_jobs=10,
            recovered_jobs=10,
            failed_jobs=0,
            not_attempted_jobs=10,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["sample_count"] == 20


# ---------------------------------------------------------------------------
# _safe_completion_recovery_summary – rejection cases
# ---------------------------------------------------------------------------

class TestRejection:
    def test_non_dict_returns_none(self):
        assert _safe_completion_recovery_summary(None) is None
        assert _safe_completion_recovery_summary("string") is None
        assert _safe_completion_recovery_summary(42) is None
        assert _safe_completion_recovery_summary([]) is None

    def test_missing_key_returns_none(self):
        raw = _valid_summary()
        del raw["observed_jobs"]
        assert _safe_completion_recovery_summary(raw) is None

    def test_negative_count_returns_none(self):
        raw = _valid_summary(observed_jobs=-1)
        assert _safe_completion_recovery_summary(raw) is None

    def test_float_count_returns_none(self):
        raw = _valid_summary(observed_jobs=15.5)
        assert _safe_completion_recovery_summary(raw) is None

    def test_string_count_returns_none(self):
        raw = _valid_summary(observed_jobs="15")
        assert _safe_completion_recovery_summary(raw) is None

    def test_bool_count_returns_none(self):
        # bool is subclass of int in Python; _is_safe_int should reject
        raw = _valid_summary(observed_jobs=True)
        assert _safe_completion_recovery_summary(raw) is None

    def test_none_count_returns_none(self):
        raw = _valid_summary(observed_jobs=None)
        assert _safe_completion_recovery_summary(raw) is None

    def test_huge_int_rejected(self):
        """2**62 exceeds Number.MAX_SAFE_INTEGER; must be rejected, not accepted."""
        raw = _valid_summary(observed_jobs=2**62)
        result = _safe_completion_recovery_summary(raw)
        assert result is None

    def test_huge_int_no_exception(self):
        """Ensure no exception is raised for huge integers."""
        raw = _valid_summary(implementer_jobs=2**62)
        try:
            result = _safe_completion_recovery_summary(raw)
        except Exception as e:
            pytest.fail(f"Unexpected exception: {e}")
        assert result is None

    def test_sample_count_over_20_rejected(self):
        raw = _valid_summary(
            sample_count=21,
            implementer_jobs=21,
            observed_jobs=21,
            unknown_jobs=0,
            attempted_jobs=10,
            recovered_jobs=10,
            failed_jobs=0,
            not_attempted_jobs=11,
        )
        assert _safe_completion_recovery_summary(raw) is None

    def test_observed_plus_unknown_neq_impl(self):
        raw = _valid_summary(
            implementer_jobs=15,
            observed_jobs=10,
            unknown_jobs=3,  # 10+3=13 != 15
        )
        assert _safe_completion_recovery_summary(raw) is None

    def test_attempted_neq_recovered_plus_failed(self):
        raw = _valid_summary(
            attempted_jobs=5,
            recovered_jobs=3,
            failed_jobs=1,  # 3+1=4 != 5
        )
        assert _safe_completion_recovery_summary(raw) is None

    def test_observed_neq_not_attempted_plus_attempted(self):
        raw = _valid_summary(
            observed_jobs=15,
            not_attempted_jobs=10,
            attempted_jobs=3,  # 10+3=13 != 15
        )
        assert _safe_completion_recovery_summary(raw) is None

    def test_sample_zero_but_nonzero_counts(self):
        raw = _valid_summary(
            sample_count=0,
            implementer_jobs=1,
            observed_jobs=1,
            unknown_jobs=0,
            attempted_jobs=0,
            recovered_jobs=0,
            failed_jobs=0,
            not_attempted_jobs=1,
        )
        assert _safe_completion_recovery_summary(raw) is None

    def test_array_count_rejected(self):
        """Arrays are not safe integers; must be rejected."""
        raw = _valid_summary(observed_jobs=[1, 2, 3])
        assert _safe_completion_recovery_summary(raw) is None

    def test_derived_rate_ignored_in_input(self):
        """If raw contains a pre-computed rate, it must be ignored and recomputed."""
        raw = _valid_summary(recovery_success_rate=0.99)
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        # Rate should be recomputed: 5/5 = 1.0, not 0.99
        assert result["recovery_success_rate"] == 1.0

    def test_derived_flags_ignored_in_input(self):
        """If raw contains a pre-computed incomplete flag, it must be ignored."""
        raw = _valid_summary(incomplete=True)
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        # incomplete should be derived: unknown=0 → False
        assert result["incomplete"] is False

    def test_unknown_only_vs_no_attempt(self):
        """Unknown jobs with zero attempts: rate is None, incomplete is True."""
        raw = _valid_summary(
            observed_jobs=10,
            unknown_jobs=5,
            implementer_jobs=15,
            attempted_jobs=0,
            recovered_jobs=0,
            failed_jobs=0,
            not_attempted_jobs=10,
        )
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert result["recovery_success_rate"] is None
        assert result["incomplete"] is True

    def test_empty_dict_returns_none(self):
        assert _safe_completion_recovery_summary({}) is None

    def test_legacy_none_returns_none(self):
        assert _safe_completion_recovery_summary(None) is None

    def test_malicious_metadata_ignored(self):
        """Extra keys in raw should not appear in output."""
        raw = _valid_summary(**{"__class__": "evil", "constructor": "hack"})
        result = _safe_completion_recovery_summary(raw)
        assert result is not None
        assert "__class__" not in result
        assert "constructor" not in result
        # Only the expected keys should be present
        expected_keys = {
            "sample_count", "implementer_jobs", "observed_jobs",
            "unknown_jobs", "attempted_jobs", "recovered_jobs",
            "failed_jobs", "not_attempted_jobs",
            "recovery_success_rate", "incomplete",
        }
        assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# _project_dashboard wiring
# ---------------------------------------------------------------------------

class TestProjectDashboardWiring:
    def test_valid_summary_projected(self):
        """A valid completion_recovery_summary in raw should appear in the result."""
        raw = {
            "recent_workflows": [],
            "completion_recovery_summary": _valid_summary(),
        }
        result = _project_dashboard(raw)
        assert result["completion_recovery_summary"] is not None
        assert result["completion_recovery_summary"]["sample_count"] == 15

    def test_missing_summary_yields_none(self):
        """If completion_recovery_summary is absent, result should have None."""
        raw = {"recent_workflows": []}
        result = _project_dashboard(raw)
        assert result["completion_recovery_summary"] is None

    def test_legacy_none_summary(self):
        """If completion_recovery_summary is explicitly None, result should have None."""
        raw = {"recent_workflows": [], "completion_recovery_summary": None}
        result = _project_dashboard(raw)
        assert result["completion_recovery_summary"] is None

    def test_malformed_summary_yields_none(self):
        """A malformed summary (invariant violation) should yield None."""
        raw = {
            "recent_workflows": [],
            "completion_recovery_summary": {
                "sample_count": 15,
                "implementer_jobs": 15,
                "observed_jobs": 10,
                "unknown_jobs": 3,  # 10+3=13 != 15
                "attempted_jobs": 5,
                "recovered_jobs": 5,
                "failed_jobs": 0,
                "not_attempted_jobs": 10,
            },
        }
        result = _project_dashboard(raw)
        assert result["completion_recovery_summary"] is None

    def test_other_fields_preserved(self):
        """Existing fields in raw should pass through unchanged."""
        raw = {
            "recent_workflows": [{"id": "wf1", "overall": "completed"}],
            "some_custom_field": "preserved",
            "completion_recovery_summary": _valid_summary(),
        }
        result = _project_dashboard(raw)
        assert result["some_custom_field"] == "preserved"
        assert result["recent_workflows"][0]["id"] == "wf1"

    def test_non_mutation_of_input(self):
        """_project_dashboard must not mutate the input dict."""
        raw = {
            "recent_workflows": [],
            "completion_recovery_summary": _valid_summary(),
        }
        import copy
        snapshot = copy.deepcopy(raw)
        _project_dashboard(raw)
        assert raw == snapshot

    def test_huge_int_in_summary_rejected_via_dashboard(self):
        """2**62 in a count field should cause the summary to be None via dashboard."""
        raw = {
            "recent_workflows": [],
            "completion_recovery_summary": _valid_summary(observed_jobs=2**62),
        }
        result = _project_dashboard(raw)
        assert result["completion_recovery_summary"] is None
