"""Strict non-actionable isolated telemetry projection; no queue access."""
from datetime import datetime

_EXPECTED = {"a97bf857edcf": ("plan", "planner"), "33c5ba12b83e": ("implement", "implementer"), "37ed0da9cdee": ("test", "tester"), "5ed8a02d1264": ("review", "reviewer")}
_REGISTERED = {
    "powerpoint-create-20261026": ("powerpointcreate20261026", "PowerPoint creation compatibility build", {
        "7e881d7908aa": ("plan", "planner"), "6a798090f5bd": ("implement", "implementer"),
        "5847207dccff": ("test", "tester"), "7dafab8e3b95": ("review", "reviewer")}),
    "powerpoint-draft-20261026": ("powerpointdraft20261026", "PowerPoint draft build", {
        "4c6e9851a91e": ("plan", "planner"), "eb3398820854": ("implement", "implementer"),
        "6c5696e2c6d7": ("test", "tester"), "69163e94def6": ("review", "reviewer")}),
    "word-task-20261026": ("wordtask20261026", "Word-only completion build", {
        "c3c9f6650366": ("plan", "planner"), "f641e1e00ef4": ("implement", "implementer"),
        "489cd923394d": ("test", "tester"), "700008c1c456": ("review", "reviewer")}),
    "word-registration-20261026": ("wordregistration20261026", "Word worker integration build", {
        "4f2a1a477586": ("plan", "planner"), "5897539b121d": ("implement", "implementer"),
        "a358307ccc57": ("test", "tester"), "151aecfaca8c": ("review", "reviewer")}),
    "word-tool-20261026": ("wordtool20261026", "Word tool build", {
        "c746c620cdd4": ("plan", "planner"), "f1d2c82a5290": ("implement", "implementer"),
        "35477fa8fea4": ("test", "tester"), "59f38ab39240": ("review", "reviewer")}),
    "word-create-20261025": ("wordcreate20261025", "Word creation compatibility build", {
        "26803bb81f52": ("plan", "planner"), "865e6e12832b": ("implement", "implementer"),
        "ae721b1382f5": ("test", "tester"), "8e916cbd3378": ("review", "reviewer")}),
    "word-draft-20261025": ("officeword20261025", "Word document draft build", {
        "e090b5d7e352": ("plan", "planner"), "8615ae700d55": ("implement", "implementer"),
        "2a1ad3e52bc9": ("test", "tester"), "bd9f4c7580ad": ("review", "reviewer")}),
    "excel-task-20261025": ("officeexceltask20261025", "Excel-only completion build", {
        "92f1684dc59d": ("plan", "planner"), "22386be3e1a5": ("implement", "implementer"),
        "f9a7542e0a84": ("test", "tester"), "6e4bcd5632ee": ("review", "reviewer")}),
    "excel-tool-20261024": ("officeexceltool20261024", "Excel tool build", _EXPECTED),
    "excel-registration-20261024": ("officeexcelregistration20261024", "Excel worker integration build", {
        "7c833861c145": ("plan", "planner"), "0f5b48e6968e": ("implement", "implementer"),
        "abefd3823ebb": ("test", "tester"), "7af2a241315a": ("review", "reviewer")}),
}
_STATUSES = {"queued", "running", "completed", "failed", "blocked", "cancelled", "approved", "merged", "pushed", "discarded", "archived"}


def _time(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 40 or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError
    return value


def project_isolated_workflows(raw):
    result = {"read_only": True, "sources": []}
    if type(raw) is not dict or raw.get("read_only") is not True or type(raw.get("sources")) is not list or len(raw["sources"]) > len(_REGISTERED):
        return result
    seen = set()
    for source in raw["sources"]:
        if type(source) is not dict or type(source.get("source")) is not str or source["source"] not in _REGISTERED or source.get("read_only") is not True:
            continue
        key = source["source"]
        workflow, label, expected = _REGISTERED[key]
        if key in seen or source.get("workflow_id") != workflow:
            continue
        seen.add(key)
        clean = {"source": key, "label": label, "workflow_id": workflow, "read_only": True, "availability": "unavailable", "jobs": []}
        try:
            jobs = source.get("jobs")
            if source.get("availability") != "available" or type(jobs) is not list or len(jobs) != 4:
                raise ValueError
            rows, ids = [], []
            for job in jobs:
                if type(job) is not dict or type(job.get("id")) is not str or job["id"] not in expected or (job.get("stage"), job.get("role")) != expected[job["id"]] or type(job.get("status")) is not str or job["status"] not in _STATUSES:
                    raise ValueError
                ids.append(job["id"])
                rows.append({"id": job["id"], "stage": job["stage"], "role": job["role"], "status": job["status"], "started_at": _time(job.get("started_at")), "finished_at": _time(job.get("finished_at"))})
            if len(set(ids)) != 4:
                raise ValueError
            clean.update(availability="available", jobs=rows)
        except (ValueError, TypeError):
            pass
        result["sources"].append(clean)
    return result
