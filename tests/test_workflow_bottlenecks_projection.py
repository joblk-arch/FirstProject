"""Tests for the workflow_bottlenecks projection adapter in _project_dashboard."""

import copy
import math
import pytest

from app import _project_dashboard

JS_SAFE_MAX = 2**53 - 1
STAGES = ("plan", "implement", "test", "review", "reason", "unknown")


def _stage(stage, job_count=1, dur=1.0, to=1, tm=0, tt=100, tmo=1, tmm=0):
    return {
        "stage": stage,
        "job_count": job_count,
        "duration_seconds": dur,
        "timing_observed_jobs": to,
        "timing_missing_jobs": tm,
        "timing_incomplete": tm > 0,
        "total_tokens": tt,
        "token_measured_jobs": tmo,
        "token_missing_jobs": tmm,
        "tokens_incomplete": tmm > 0,
    }


def _valid_summary():
    return {
        "sample_count": 3,
        "stages": [
            _stage("plan", 2, 1.5, 2, 0, 100, 2, 0),
            _stage("implement", 3, 2.0, 3, 0, 200, 3, 0),
            _stage("test", 1, 0.5, 1, 0, 50, 1, 0),
            _stage("review", 1, 1.0, 1, 0, 80, 1, 0),
            _stage("reason", 1, 0.8, 1, 0, 60, 1, 0),
            _stage("unknown", 0, None, 0, 0, None, 0, 0),
        ],
        "highest_time_stage": "implement",
        "highest_token_stage": "implement",
    }


def _raw_with_bottlenecks(summary):
    return {"workflow_bottlenecks": summary, "other_field": "preserved"}


# --- Valid schema and fixed shape/order ---

def test_valid_schema_shape_and_order():
    raw = _raw_with_bottlenecks(_valid_summary())
    result = _project_dashboard(raw)
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["sample_count"] == 3
    assert [s["stage"] for s in wb["stages"]] == list(STAGES)
    # Fixed order even if input is shuffled
    shuffled = copy.deepcopy(_valid_summary())
    shuffled["stages"] = list(reversed(shuffled["stages"]))
    result2 = _project_dashboard(_raw_with_bottlenecks(shuffled))
    assert [s["stage"] for s in result2["workflow_bottlenecks"]["stages"]] == list(STAGES)


def test_valid_zero_values():
    """Zero duration and zero tokens are valid when observed/measured > 0."""
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = 0
    s["stages"][0]["total_tokens"] = 0
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["duration_seconds"] == 0
    assert wb["stages"][0]["total_tokens"] == 0


# --- 0 vs None: conflicting zero-observation cases ---

def test_timing_observed_zero_with_non_none_duration_rejected():
    """When timing_observed==0, duration_seconds must be None; non-None => entire summary None."""
    s = _valid_summary()
    s["stages"][0]["timing_observed_jobs"] = 0
    s["stages"][0]["timing_missing_jobs"] = 2
    s["stages"][0]["duration_seconds"] = 5.0  # non-None, should reject
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_timing_observed_zero_with_none_duration_valid():
    """When timing_observed==0 and duration_seconds is None, it stays None."""
    s = _valid_summary()
    s["stages"][0]["timing_observed_jobs"] = 0
    s["stages"][0]["timing_missing_jobs"] = 2
    s["stages"][0]["duration_seconds"] = None
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["duration_seconds"] is None


def test_token_measured_zero_with_non_none_total_tokens_rejected():
    """When token_measured==0, total_tokens must be None; non-None (including 0) => entire summary None."""
    s = _valid_summary()
    s["stages"][0]["token_measured_jobs"] = 0
    s["stages"][0]["token_missing_jobs"] = 2
    s["stages"][0]["total_tokens"] = 0  # non-None, should reject
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_token_measured_zero_with_string_total_tokens_rejected():
    """When token_measured==0, total_tokens='0' (string) is non-None => reject."""
    s = _valid_summary()
    s["stages"][0]["token_measured_jobs"] = 0
    s["stages"][0]["token_missing_jobs"] = 2
    s["stages"][0]["total_tokens"] = "0"
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_token_measured_zero_with_bool_total_tokens_rejected():
    """When token_measured==0, total_tokens=True (bool) is non-None => reject."""
    s = _valid_summary()
    s["stages"][0]["token_measured_jobs"] = 0
    s["stages"][0]["token_missing_jobs"] = 2
    s["stages"][0]["total_tokens"] = True
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_token_measured_zero_with_none_total_tokens_valid():
    """When token_measured==0 and total_tokens is None, it stays None."""
    s = _valid_summary()
    s["stages"][0]["token_measured_jobs"] = 0
    s["stages"][0]["token_missing_jobs"] = 2
    s["stages"][0]["total_tokens"] = None
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["total_tokens"] is None


# --- Missing/legacy summary ---

def test_missing_summary_returns_none():
    result = _project_dashboard({})
    assert result["workflow_bottlenecks"] is None


def test_none_summary_returns_none():
    result = _project_dashboard({"workflow_bottlenecks": None})
    assert result["workflow_bottlenecks"] is None


def test_non_dict_summary_returns_none():
    result = _project_dashboard({"workflow_bottlenecks": "not a dict"})
    assert result["workflow_bottlenecks"] is None


def test_legacy_summary_without_stages_returns_none():
    result = _project_dashboard({"workflow_bottlenecks": {"sample_count": 5}})
    assert result["workflow_bottlenecks"] is None


# --- 6 fixed stages ---

def test_missing_stage_returns_none():
    s = _valid_summary()
    s["stages"] = s["stages"][:5]  # remove one stage
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_duplicate_stage_returns_none():
    s = _valid_summary()
    s["stages"] = s["stages"][:5] + [s["stages"][0]]  # duplicate plan
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_unknown_stage_returns_none():
    s = _valid_summary()
    s["stages"][0]["stage"] = "bogus"
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_seven_stages_returns_none():
    s = _valid_summary()
    s["stages"].append(_stage("extra"))
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


# --- Malformed counts/stages/numbers ---

def test_bool_sample_count_rejected():
    s = _valid_summary()
    s["sample_count"] = True
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_sample_count_over_20_rejected():
    s = _valid_summary()
    s["sample_count"] = 21
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_sample_count_negative_rejected():
    s = _valid_summary()
    s["sample_count"] = -1
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_bool_job_count_rejected():
    s = _valid_summary()
    s["stages"][0]["job_count"] = True
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_negative_job_count_rejected():
    s = _valid_summary()
    s["stages"][0]["job_count"] = -1
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_timing_observed_plus_missing_exceeds_job_count_rejected():
    s = _valid_summary()
    s["stages"][0]["job_count"] = 2
    s["stages"][0]["timing_observed_jobs"] = 2
    s["stages"][0]["timing_missing_jobs"] = 1  # 2+1 > 2
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_token_measured_plus_missing_not_equal_job_count_rejected():
    s = _valid_summary()
    s["stages"][0]["job_count"] = 3
    s["stages"][0]["token_measured_jobs"] = 2
    s["stages"][0]["token_missing_jobs"] = 0  # 2+0 != 3
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_nan_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = float("nan")
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_infinity_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = float("inf")
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_negative_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = -1.0
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_bool_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = True
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_string_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = "1.5"
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_float_total_tokens_rejected():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = 100.5
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_bool_total_tokens_rejected():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = True
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_negative_total_tokens_rejected():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = -1
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_oversized_total_tokens_rejected():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = JS_SAFE_MAX + 1
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_oversized_duration_rejected():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = JS_SAFE_MAX + 1
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_non_dict_stage_row_rejected():
    s = _valid_summary()
    s["stages"][0] = "not a dict"
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_stages_not_a_list_rejected():
    s = _valid_summary()
    s["stages"] = "not a list"
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


# --- Huge integer duration without OverflowError ---

def test_huge_integer_duration_no_overflow():
    """A huge int duration (> JS_SAFE_MAX) should be rejected, not crash."""
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = 10**100
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_huge_integer_total_tokens_no_overflow():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = 10**100
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


def test_huge_integer_job_count_no_overflow():
    s = _valid_summary()
    s["stages"][0]["job_count"] = 10**100
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


# --- Flags/winners derived ---

def test_derived_incomplete_flags():
    s = _valid_summary()
    # stage[0] has job_count=2; set missing=1, measured=1 to satisfy constraints
    s["stages"][0]["timing_observed_jobs"] = 1
    s["stages"][0]["timing_missing_jobs"] = 1
    s["stages"][0]["token_measured_jobs"] = 1
    s["stages"][0]["token_missing_jobs"] = 1
    s["stages"][0]["timing_incomplete"] = False  # wrong raw flag
    s["stages"][0]["tokens_incomplete"] = False  # wrong raw flag
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["timing_incomplete"] is True
    assert wb["stages"][0]["tokens_incomplete"] is True


def test_derived_flags_zero_missing():
    s = _valid_summary()
    s["stages"][0]["timing_missing_jobs"] = 0
    s["stages"][0]["token_missing_jobs"] = 0
    s["stages"][0]["timing_incomplete"] = True  # wrong raw flag
    s["stages"][0]["tokens_incomplete"] = True  # wrong raw flag
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["timing_incomplete"] is False
    assert wb["stages"][0]["tokens_incomplete"] is False


def test_highest_time_stage_derived():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = 10.0
    s["stages"][1]["duration_seconds"] = 20.0
    s["stages"][2]["duration_seconds"] = 5.0
    s["highest_time_stage"] = "test"  # wrong, should be derived
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["highest_time_stage"] == "implement"


def test_highest_token_stage_derived():
    s = _valid_summary()
    s["stages"][0]["total_tokens"] = 500
    s["stages"][1]["total_tokens"] = 300
    s["stages"][2]["total_tokens"] = 400
    s["highest_token_stage"] = "review"  # wrong, should be derived
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["highest_token_stage"] == "plan"


def test_highest_none_when_no_positive_observations():
    s = _valid_summary()
    for st in s["stages"]:
        st["duration_seconds"] = None
        st["total_tokens"] = None
        st["timing_observed_jobs"] = 0
        st["timing_missing_jobs"] = st["job_count"]
        st["token_measured_jobs"] = 0
        st["token_missing_jobs"] = st["job_count"]
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["highest_time_stage"] is None
    assert wb["highest_token_stage"] is None


def test_highest_tie_goes_to_first_in_fixed_order():
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = 5.0  # plan
    s["stages"][1]["duration_seconds"] = 5.0  # implement (tie)
    s["stages"][2]["duration_seconds"] = 3.0
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["highest_time_stage"] == "plan"  # first in fixed order wins tie


def test_zero_duration_not_positive_for_highest():
    """Zero duration is valid but not strictly positive, so not a candidate for highest."""
    s = _valid_summary()
    s["stages"][0]["duration_seconds"] = 0
    s["stages"][1]["duration_seconds"] = 0
    s["stages"][2]["duration_seconds"] = 1.0
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["highest_time_stage"] == "test"


# --- Extra stripping ---

def test_extra_fields_stripped_at_stage_level():
    s = _valid_summary()
    s["stages"][0]["extra_field"] = "should be stripped"
    s["stages"][0]["metadata"] = {"foo": "bar"}
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    stage_keys = set(wb["stages"][0].keys())
    expected_keys = {
        "stage", "job_count", "duration_seconds",
        "timing_observed_jobs", "timing_missing_jobs", "timing_incomplete",
        "total_tokens", "token_measured_jobs", "token_missing_jobs", "tokens_incomplete",
    }
    assert stage_keys == expected_keys


def test_extra_fields_stripped_at_top_level():
    s = _valid_summary()
    s["extra_top_field"] = "should be stripped"
    s["metadata"] = {"x": 1}
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    top_keys = set(wb.keys())
    expected_keys = {"sample_count", "stages", "highest_time_stage", "highest_token_stage"}
    assert top_keys == expected_keys


# --- No mutation ---

def test_no_mutation_of_input():
    s = _valid_summary()
    s["stages"][0]["extra_field"] = "original"
    raw = _raw_with_bottlenecks(s)
    original = copy.deepcopy(raw)
    _project_dashboard(raw)
    assert raw == original


# --- Unaffected other fields ---

def test_other_dashboard_fields_preserved():
    raw = _raw_with_bottlenecks(_valid_summary())
    raw["other_field"] = "preserved"
    result = _project_dashboard(raw)
    assert result["other_field"] == "preserved"
    assert result["workflow_bottlenecks"] is not None


def test_other_fields_preserved_when_bottlenecks_none():
    raw = {"workflow_bottlenecks": "garbage", "other_field": "still_here"}
    result = _project_dashboard(raw)
    assert result["other_field"] == "still_here"
    assert result["workflow_bottlenecks"] is None


# --- Empty sample 0 implies all job_count 0 ---

def test_sample_count_zero_all_job_counts_zero_valid():
    s = _valid_summary()
    s["sample_count"] = 0
    for st in s["stages"]:
        st["job_count"] = 0
        st["timing_observed_jobs"] = 0
        st["timing_missing_jobs"] = 0
        st["token_measured_jobs"] = 0
        st["token_missing_jobs"] = 0
        st["duration_seconds"] = None
        st["total_tokens"] = None
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["sample_count"] == 0
    for st in wb["stages"]:
        assert st["job_count"] == 0


def test_sample_count_zero_with_nonzero_job_count_rejected():
    s = _valid_summary()
    s["sample_count"] = 0
    s["stages"][0]["job_count"] = 5  # non-zero, should reject
    result = _project_dashboard(_raw_with_bottlenecks(s))
    assert result["workflow_bottlenecks"] is None


# --- Multiple jobs > 20 (no cap on stage job_count) ---

def test_job_count_over_20_allowed():
    s = _valid_summary()
    s["sample_count"] = 20
    s["stages"][0]["job_count"] = 50  # > 20, should be allowed
    s["stages"][0]["timing_observed_jobs"] = 50
    s["stages"][0]["token_measured_jobs"] = 50
    s["stages"][0]["token_missing_jobs"] = 0
    result = _project_dashboard(_raw_with_bottlenecks(s))
    wb = result["workflow_bottlenecks"]
    assert wb is not None
    assert wb["stages"][0]["job_count"] == 50


# --- Non-mutation of raw input structure ---

def test_input_not_mutated_extra_fields():
    s = _valid_summary()
    s["stages"][0]["injected"] = "data"
    raw = _raw_with_bottlenecks(s)
    snapshot = copy.deepcopy(raw)
    _project_dashboard(raw)
    assert raw == snapshot


@pytest.mark.parametrize("field", [
    "job_count", "timing_observed_jobs", "timing_missing_jobs",
    "token_measured_jobs", "token_missing_jobs",
])
@pytest.mark.parametrize("bad", [True, False, -1, 1.0, "1", None, JS_SAFE_MAX + 1])
def test_each_count_rejects_invalid_values(field, bad):
    summary = _valid_summary()
    summary["stages"][0][field] = bad
    assert _project_dashboard(_raw_with_bottlenecks(summary))["workflow_bottlenecks"] is None


@pytest.mark.parametrize("bad", [False, 1.0, "1", None, JS_SAFE_MAX + 1])
def test_sample_count_rejects_other_invalid_values(bad):
    summary = _valid_summary()
    summary["sample_count"] = bad
    assert _project_dashboard(_raw_with_bottlenecks(summary))["workflow_bottlenecks"] is None


@pytest.mark.parametrize("key", ["duration_seconds", "total_tokens"])
def test_measured_value_cannot_be_missing(key):
    summary = _valid_summary()
    summary["stages"][0][key] = None
    assert _project_dashboard(_raw_with_bottlenecks(summary))["workflow_bottlenecks"] is None


def test_token_tie_uses_fixed_order_not_supplied_winner():
    summary = _valid_summary()
    summary["stages"][0]["total_tokens"] = 500
    summary["stages"][1]["total_tokens"] = 500
    summary["highest_token_stage"] = "implement"
    projected = _project_dashboard(_raw_with_bottlenecks(summary))["workflow_bottlenecks"]
    assert projected["highest_token_stage"] == "plan"
