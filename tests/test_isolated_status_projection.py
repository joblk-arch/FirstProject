import json
from pathlib import Path
import subprocess
import isolated_status_projection as projection
import app


def payload():
    return {"read_only": True, "sources": [{"source": "excel-tool-20261024", "label": "Ignore untrusted title", "workflow_id": "officeexceltool20261024", "read_only": True, "availability": "available", "jobs": [{"id": key, "stage": value[0], "role": value[1], "status": "running" if value[0] == "plan" else "queued", "started_at": None, "finished_at": None, "path": "/secret", "actions": ["retry"]} for key, value in projection._EXPECTED.items()], "secret": "hidden"}]}


def test_strict_readonly_projection_and_dashboard_wiring():
    source = payload()
    result = app._project_dashboard({"isolated_workflows": source})["isolated_workflows"]
    assert len(result["sources"][0]["jobs"]) == 4
    assert result["sources"][0]["label"] == "Excel tool build"
    assert not any(x in json.dumps(result) for x in ("secret", "actions", "retry", "untrusted title"))
    result["sources"][0]["jobs"].clear()
    assert len(projection.project_isolated_workflows(source)["sources"][0]["jobs"]) == 4


def test_malformed_missing_and_unregistered_fail_closed():
    for source in (None, {}, {"sources": "bad"}, {"read_only": False, "sources": []}):
        assert projection.project_isolated_workflows(source)["sources"] == []
    source = payload()
    source["sources"][0]["workflow_id"] = "unregistered"
    assert projection.project_isolated_workflows(source)["sources"] == []
    source = payload()
    source["sources"][0]["jobs"][0]["status"] = "<script>"
    result = projection.project_isolated_workflows(source)["sources"][0]
    assert result["availability"] == "unavailable" and result["jobs"] == []
    source = payload()
    source["sources"][0]["jobs"][0]["id"] = source["sources"][0]["jobs"][1]["id"]
    assert projection.project_isolated_workflows(source)["sources"][0]["availability"] == "unavailable"


def test_actual_readonly_renderer():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", str(root/"tests/js/isolated-workflows.test.js")], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout+result.stderr


def test_separate_non_actionable_html_wiring():
    root = Path(__file__).resolve().parents[1]
    html = (root/"static/index.html").read_text()
    panel = html.split('id="isolated-workflows-panel"', 1)[1].split('</section>', 1)[0]
    assert 'id="isolated-workflow-status"' in panel and "Read-only" in panel
    assert not any(token in panel for token in ("<button", "<form", "data-workflow-id", "<a "))
    assert html.index('/isolated-workflows.js') < html.index('/app.js?')
    js = (root/"static/app.js").read_text()
    assert "renderIsolatedWorkflows(data.isolated_workflows)" in js
    assert "catch(error) { window.renderIsolatedWorkflows?.(null);" in js
    assert "COPY isolated_status_projection.py /app/isolated_status_projection.py" in (root/"Dockerfile").read_text()
