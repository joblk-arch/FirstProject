"""Tests for the validation diagnostics projection in _project_dashboard."""
import copy
import json
from pathlib import Path
import pytest

from app import _safe_validation_diagnostics, _project_dashboard


def test_deployed_gateway_contract_fixture():
    """Use the deployed API's literal field names, not UI-generated aliases."""
    raw = json.loads((Path(__file__).parent / "fixtures" / "validation_dashboard.json").read_text())
    before = copy.deepcopy(raw)
    result = _project_dashboard(raw)
    assert raw == before
    assert "recent_jobs" not in result
    assert result["jobs"][0]["validation_request_count"] == 4
    assert result["jobs"][0]["validation_execution_count"] == 2
    assert result["jobs"][0]["validation_last_outcome"] == "suppressed"
    assert result["jobs"][0]["validation_recovery_attempted"] == 1
    assert result["jobs"][0]["validation_recovery_outcome"] == "recovered"
    assert result["jobs"][0]["status"] == "failed"
    assert result["jobs"][1]["validation_request_count"] == 0
    assert result["jobs"][1]["validation_recovery_attempted"] is None
    assert result["jobs"][2]["validation_request_count"] is None
    assert result["projects"] == before["projects"]
    for job in result["jobs"]:
        assert "validation_requests" not in job
        assert "validation_executions" not in job
        assert "validation_recovery_count" not in job
        assert "validation_recovery_state" not in job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(role="implementer", **overrides):
    base = {
        "id": "job-1",
        "role": role,
        "stage": "build",
        "project": "alpha",
        "status": "completed",
    }
    base.update(overrides)
    return base


def _valid_impl_job(**overrides):
    base = _job(
        role="implementer",
        validation_request_count=10,
        validation_execution_count=8,
        validation_last_outcome="passed",
        validation_recovery_attempted=1,
        validation_recovery_outcome="recovered",
    )
    base.update(overrides)
    return base


def _valid_tester_job(**overrides):
    base = _job(
        role="tester",
        validation_request_count=5,
        validation_execution_count=3,
        validation_last_outcome="passed",
        validation_recovery_attempted=1,
        validation_recovery_outcome="recovered",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _safe_validation_diagnostics – role applicability
# ---------------------------------------------------------------------------

class TestRoleApplicability:
    @pytest.mark.parametrize("role", ["viewer", "admin", "unknown", None, 42, True])
    def test_non_implementer_tester_all_null(self, role):
        entry = _job(role=role, validation_request_count=10, validation_execution_count=5,
                     validation_last_outcome="passed", validation_recovery_attempted=1,
                     validation_recovery_outcome="recovered")
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] is None
        assert result["validation_execution_count"] is None
        assert result["validation_last_outcome"] is None
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None

    def test_implementer_valid(self):
        entry = _valid_impl_job()
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] == 10
        assert result["validation_execution_count"] == 8
        assert result["validation_last_outcome"] == "passed"
        assert result["validation_recovery_attempted"] == 1
        assert result["validation_recovery_outcome"] == "recovered"

    def test_tester_valid(self):
        entry = _valid_tester_job()
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] == 5
        assert result["validation_execution_count"] == 3
        assert result["validation_last_outcome"] == "passed"
        # tester: recovery not applicable
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None


# ---------------------------------------------------------------------------
# _safe_validation_diagnostics – request/execution/outcome triple
# ---------------------------------------------------------------------------

class TestTripleValidation:
    @pytest.mark.parametrize("req,exe,outcome,expect_null", [
        # Valid combinations
        (10, 8, "passed", False),
        (10, 8, "nonzero", False),
        (10, 8, "timeout", False),
        (10, 8, "invalid_request", False),
        (10, 8, "execution_error", False),
        (10, 8, "suppressed", False),
        (10, 8, "unknown", False),
        (0, 0, "not_attempted", False),
        (1, 1, "passed", False),
        (1000000, 1000000, "passed", False),
        # Invalid: exec > req
        (10, 20, "passed", True),
        # Invalid: req=0 but outcome != not_attempted
        (0, 0, "passed", True),
        # Invalid: req>0 but outcome == not_attempted
        (10, 5, "not_attempted", True),
        # Invalid: passed/nonzero/timeout need exec >= 1
        (10, 0, "passed", True),
        (10, 0, "nonzero", True),
        (10, 0, "timeout", True),
        # Invalid: exec=0 with invalid_request is OK (no exec>=1 requirement)
        (10, 0, "invalid_request", False),
        (10, 0, "execution_error", False),
        (10, 0, "suppressed", False),
        (10, 0, "unknown", False),
    ])
    def test_triple(self, req, exe, outcome, expect_null):
        entry = _valid_impl_job(
            validation_request_count=req,
            validation_execution_count=exe,
            validation_last_outcome=outcome,
        )
        result = _safe_validation_diagnostics(entry)
        if expect_null:
            assert result["validation_request_count"] is None
            assert result["validation_execution_count"] is None
            assert result["validation_last_outcome"] is None
        else:
            assert result["validation_request_count"] == req
            assert result["validation_execution_count"] == exe
            assert result["validation_last_outcome"] == outcome

    @pytest.mark.parametrize("bad_val", [True, False, 1.5, "10", None, -1, 1000001, [10]])
    def test_invalid_request_type(self, bad_val):
        entry = _valid_impl_job(validation_request_count=bad_val)
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] is None
        assert result["validation_execution_count"] is None
        assert result["validation_last_outcome"] is None

    @pytest.mark.parametrize("bad_val", [True, False, 1.5, "8", None, -1, 1000001])
    def test_invalid_execution_type(self, bad_val):
        entry = _valid_impl_job(validation_execution_count=bad_val)
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] is None
        assert result["validation_execution_count"] is None
        assert result["validation_last_outcome"] is None

    @pytest.mark.parametrize("bad_val", [True, 42, None, "PASSED", "invalid", ""])
    def test_invalid_outcome_type(self, bad_val):
        entry = _valid_impl_job(validation_last_outcome=bad_val)
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] is None
        assert result["validation_execution_count"] is None
        assert result["validation_last_outcome"] is None


# ---------------------------------------------------------------------------
# _safe_validation_diagnostics – recovery pair
# ---------------------------------------------------------------------------

class TestRecoveryPair:
    @pytest.mark.parametrize("count,state,expect_valid", [
        (0, "not_attempted", True),
        (1, "failed", True),
        (1, "recovered", True),
        # Mismatch: count=0 but state != not_attempted
        (0, "failed", False),
        (0, "recovered", False),
        # Mismatch: count=1 but state == not_attempted
        (1, "not_attempted", False),
    ])
    def test_implementer_recovery(self, count, state, expect_valid):
        entry = _valid_impl_job(
            validation_recovery_attempted=count,
            validation_recovery_outcome=state,
        )
        result = _safe_validation_diagnostics(entry)
        if expect_valid:
            assert result["validation_recovery_attempted"] == count
            assert result["validation_recovery_outcome"] == state
        else:
            assert result["validation_recovery_attempted"] is None
            assert result["validation_recovery_outcome"] is None

    @pytest.mark.parametrize("bad_count", [True, False, 2, -1, 1.0, "1", None])
    def test_implementer_invalid_count(self, bad_count):
        entry = _valid_impl_job(
            validation_recovery_attempted=bad_count,
            validation_recovery_outcome="recovered",
        )
        result = _safe_validation_diagnostics(entry)
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None

    @pytest.mark.parametrize("bad_state", [True, 42, None, "RECOVERED", "invalid", ""])
    def test_implementer_invalid_state(self, bad_state):
        entry = _valid_impl_job(
            validation_recovery_attempted=1,
            validation_recovery_outcome=bad_state,
        )
        result = _safe_validation_diagnostics(entry)
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None

    def test_tester_recovery_always_null(self):
        """For tester role, recovery pair is always NULL regardless of values."""
        entry = _valid_tester_job(
            validation_recovery_attempted=1,
            validation_recovery_outcome="recovered",
        )
        result = _safe_validation_diagnostics(entry)
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None


# ---------------------------------------------------------------------------
# Independence: triple and recovery validated independently
# ---------------------------------------------------------------------------

class TestIndependence:
    def test_invalid_triple_valid_recovery(self):
        """Invalid triple => triple NULL, but valid recovery pair preserved."""
        entry = _valid_impl_job(
            validation_request_count=10,
            validation_execution_count=20,  # exec > req => invalid
            validation_last_outcome="passed",
            validation_recovery_attempted=1,
            validation_recovery_outcome="recovered",
        )
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] is None
        assert result["validation_execution_count"] is None
        assert result["validation_last_outcome"] is None
        # Recovery pair is independent and valid
        assert result["validation_recovery_attempted"] == 1
        assert result["validation_recovery_outcome"] == "recovered"

    def test_valid_triple_invalid_recovery(self):
        """Valid triple preserved, but invalid recovery pair => NULL."""
        entry = _valid_impl_job(
            validation_request_count=10,
            validation_execution_count=8,
            validation_last_outcome="passed",
            validation_recovery_attempted=0,
            validation_recovery_outcome="recovered",  # mismatch
        )
        result = _safe_validation_diagnostics(entry)
        assert result["validation_request_count"] == 10
        assert result["validation_execution_count"] == 8
        assert result["validation_last_outcome"] == "passed"
        assert result["validation_recovery_attempted"] is None
        assert result["validation_recovery_outcome"] is None


# ---------------------------------------------------------------------------
# Non-mutation
# ---------------------------------------------------------------------------

class TestNonMutation:
    def test_input_not_mutated(self):
        entry = _valid_impl_job()
        original = copy.deepcopy(entry)
        _safe_validation_diagnostics(entry)
        assert entry == original

    def test_missing_fields_not_added_to_input(self):
        entry = _job(role="implementer")
        original_keys = set(entry.keys())
        _safe_validation_diagnostics(entry)
        assert set(entry.keys()) == original_keys


# ---------------------------------------------------------------------------
# _project_dashboard integration
# ---------------------------------------------------------------------------

class TestDashboardProjection:
    def test_jobs_projected(self):
        raw = {
            "jobs": [
                _valid_impl_job(id="j1"),
                _valid_tester_job(id="j2"),
            ],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        assert "jobs" in result
        assert len(result["jobs"]) == 2
        # First job: implementer with valid data
        j1 = result["jobs"][0]
        assert j1["validation_request_count"] == 10
        assert j1["validation_execution_count"] == 8
        assert j1["validation_last_outcome"] == "passed"
        assert j1["validation_recovery_attempted"] == 1
        assert j1["validation_recovery_outcome"] == "recovered"
        # Second job: tester
        j2 = result["jobs"][1]
        assert j2["validation_request_count"] == 5
        assert j2["validation_execution_count"] == 3
        assert j2["validation_last_outcome"] == "passed"
        assert j2["validation_recovery_attempted"] is None
        assert j2["validation_recovery_outcome"] is None

    def test_existing_fields_preserved(self):
        raw = {
            "jobs": [
                _valid_impl_job(id="j1", status="completed", stage="build", project="alpha"),
            ],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        j1 = result["jobs"][0]
        assert j1["id"] == "j1"
        assert j1["status"] == "completed"
        assert j1["stage"] == "build"
        assert j1["project"] == "alpha"
        assert j1["role"] == "implementer"

    def test_non_dict_jobs_skipped(self):
        raw = {
            "jobs": ["not-a-dict", 42, None, _valid_impl_job(id="j1")],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        assert len(result["jobs"]) == 1
        assert result["jobs"][0]["id"] == "j1"

    def test_raw_not_mutated(self):
        raw = {
            "jobs": [_valid_impl_job(id="j1")],
            "recent_workflows": [],
        }
        original = copy.deepcopy(raw)
        _project_dashboard(raw)
        assert raw == original

    def test_viewer_role_all_null(self):
        raw = {
            "jobs": [
                _job(role="viewer", validation_request_count=10, validation_execution_count=5,
                     validation_last_outcome="passed", validation_recovery_attempted=1,
                     validation_recovery_outcome="recovered"),
            ],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        j = result["jobs"][0]
        assert j["validation_request_count"] is None
        assert j["validation_execution_count"] is None
        assert j["validation_last_outcome"] is None
        assert j["validation_recovery_attempted"] is None
        assert j["validation_recovery_outcome"] is None

    def test_recovered_with_failed_status(self):
        """Recovered recovery state is independent of overall job status."""
        raw = {
            "jobs": [
                _valid_impl_job(status="failed", validation_recovery_outcome="recovered"),
            ],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        j = result["jobs"][0]
        assert j["status"] == "failed"
        assert j["validation_recovery_outcome"] == "recovered"

    def test_no_secrets_in_projected_fields(self):
        """Projected fields should not contain output/error/raw_command/path/secret."""
        raw = {
            "jobs": [
                _valid_impl_job(
                    output="secret-data",
                    error="error-msg",
                    raw_command="rm -rf /",
                    path="/etc/passwd",
                    secret="token123",
                ),
            ],
            "recent_workflows": [],
        }
        result = _project_dashboard(raw)
        j = result["jobs"][0]
        # The five validation fields should only contain validated values
        for field in ("validation_request_count", "validation_execution_count",
                      "validation_last_outcome", "validation_recovery_attempted",
                      "validation_recovery_outcome"):
            val = j[field]
            assert val is None or isinstance(val, (int, str)), f"{field} has unexpected type"
            if isinstance(val, str):
                assert "secret" not in val
                assert "token" not in val
                assert "/etc/" not in val
