"""Strict non-actionable isolated telemetry projection; no queue access."""
from datetime import datetime

_EXPECTED = {"a97bf857edcf": ("plan", "planner"), "33c5ba12b83e": ("implement", "implementer"), "37ed0da9cdee": ("test", "tester"), "5ed8a02d1264": ("review", "reviewer")}
_STATUSES = {"queued", "running", "completed", "failed", "blocked", "cancelled", "approved", "merged", "pushed", "discarded", "archived"}


def _time(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 40 or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError
    return value


def project_isolated_workflows(raw):
    result = {"read_only": True, "sources": []}
    if type(raw) is not dict or raw.get("read_only") is not True or type(raw.get("sources")) is not list or len(raw["sources"]) > 8:
        return result
    for source in raw["sources"]:
        if type(source) is not dict or source.get("source") != "excel-tool-20261024" or source.get("workflow_id") != "officeexceltool20261024" or source.get("read_only") is not True:
            continue
        clean = {"source": "excel-tool-20261024", "label": "Excel tool build", "workflow_id": "officeexceltool20261024", "read_only": True, "availability": "unavailable", "jobs": []}
        try:
            jobs = source.get("jobs")
            if source.get("availability") != "available" or type(jobs) is not list or len(jobs) != 4:
                raise ValueError
            rows, ids = [], []
            for job in jobs:
                if type(job) is not dict or type(job.get("id")) is not str or job["id"] not in _EXPECTED or (job.get("stage"), job.get("role")) != _EXPECTED[job["id"]] or type(job.get("status")) is not str or job["status"] not in _STATUSES:
                    raise ValueError
                ids.append(job["id"])
                rows.append({"id": job["id"], "stage": job["stage"], "role": job["role"], "status": job["status"], "started_at": _time(job.get("started_at")), "finished_at": _time(job.get("finished_at"))})
            if len(set(ids)) != 4:
                raise ValueError
            clean.update(availability="available", jobs=rows)
        except (ValueError, TypeError):
            pass
        result["sources"].append(clean)
        break
    return result
