import json
from pathlib import Path
import subprocess
import isolated_status_projection as projection
import app

def test_comparison_exact_five_stage_projection():
    for key in ('powerpoint-task-27b-20261027','powerpoint-task-flash-20261027'):
        workflow,label,expected=projection._REGISTERED[key]
        source=dict(source=key,workflow_id=workflow,read_only=True,availability='available',
            jobs=[dict(id=j,stage=s,role=r,status='queued',started_at=None,finished_at=None) for j,(s,r) in expected.items()])
        assert len(expected)==5
        assert projection.project_isolated_workflows(dict(read_only=True,sources=[source]))['sources'][0]['availability']=='available'
        source['jobs'].pop()
        assert projection.project_isolated_workflows(dict(read_only=True,sources=[source]))['sources'][0]['availability']=='unavailable'
        source['jobs'].append(source['jobs'][0])
        assert projection.project_isolated_workflows(dict(read_only=True,sources=[source]))['sources'][0]['availability']=='unavailable'

def test_word_tool_exact_projection():
    key="word-tool-20261026"
    workflow,label,expected=projection._REGISTERED[key]
    assert list(expected)==["c746c620cdd4","f1d2c82a5290","35477fa8fea4","59f38ab39240"]
    source=dict(source=key,workflow_id=workflow,read_only=True,availability="available",
        jobs=[dict(id=j,stage=s,role=r,status="queued",started_at=None,finished_at=None) for j,(s,r) in expected.items()])
    result=projection.project_isolated_workflows(dict(read_only=True,sources=[source]))
    assert result["sources"][0]["availability"]=="available" and result["sources"][0]["label"]==label

def test_word_creation_exact_projection():
    key="word-create-20261025"
    workflow,label,expected=projection._REGISTERED[key]
    assert list(expected)==["26803bb81f52","865e6e12832b","ae721b1382f5","8e916cbd3378"]
    source=dict(source=key,workflow_id=workflow,read_only=True,availability="available",
        jobs=[dict(id=j,stage=s,role=r,status="running" if s=="plan" else "queued",started_at=None,finished_at=None) for j,(s,r) in expected.items()])
    result=projection.project_isolated_workflows(dict(read_only=True,sources=[source]))
    assert result["sources"][0]["availability"]=="available" and result["sources"][0]["label"]==label

def test_word_draft_exact_projection():
    key = "word-draft-20261025"
    workflow, label, expected = projection._REGISTERED[key]
    assert list(expected) == ["e090b5d7e352", "8615ae700d55", "2a1ad3e52bc9", "bd9f4c7580ad"]
    source = dict(source=key, workflow_id=workflow, read_only=True, availability="available",
        jobs=[dict(id=j, stage=s, role=r, status="queued", started_at=None, finished_at=None) for j, (s,r) in expected.items()])
    result = projection.project_isolated_workflows(dict(read_only=True, sources=[source]))
    assert result["sources"][0]["label"] == label and result["sources"][0]["availability"] == "available"
    source["jobs"][0]["id"] = next(iter(projection._EXPECTED))
    assert projection.project_isolated_workflows(dict(read_only=True,sources=[source]))["sources"][0]["availability"] == "unavailable"


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


def test_both_registered_builds_no_cross_membership_or_duplicates():
    raw = payload()
    key = "excel-registration-20261024"
    workflow, label, expected = projection._REGISTERED[key]
    new = {"source": key, "workflow_id": workflow, "label": "untrusted", "read_only": True,
           "availability": "available", "jobs": [dict(id=j, stage=s, role=r, status="queued", started_at=None, finished_at=None) for j, (s, r) in expected.items()]}
    raw["sources"].extend([new, new])
    result = projection.project_isolated_workflows(raw)
    assert len(result["sources"]) == 2
    assert result["sources"][1]["label"] == label
    assert all(s["availability"] == "available" for s in result["sources"])
    new["jobs"][0]["id"] = next(iter(projection._EXPECTED))
    result = projection.project_isolated_workflows(raw)
    assert result["sources"][0]["availability"] == "available"
    assert result["sources"][1]["availability"] == "unavailable"


def test_current_completion_build_exact_membership():
    key = "excel-task-20261025"
    workflow, label, expected = projection._REGISTERED[key]
    source = dict(source=key, workflow_id=workflow, label="ignored", read_only=True, availability="available",
        jobs=[dict(id=j, stage=s, role=r, status="running" if s == "plan" else "queued", started_at=None, finished_at=None) for j, (s, r) in expected.items()])
    result = projection.project_isolated_workflows(dict(read_only=True, sources=[source]))
    assert result["sources"][0]["label"] == label
    assert result["sources"][0]["availability"] == "available"
    assert [j["id"] for j in result["sources"][0]["jobs"]] == list(expected)
    source["jobs"][0]["id"] = next(iter(projection._EXPECTED))
    assert projection.project_isolated_workflows(dict(read_only=True, sources=[source]))["sources"][0]["availability"] == "unavailable"
def test_word_registration_source_exact():
    workflow, label, expected = projection._REGISTERED['word-registration-20261026']
    assert workflow == 'wordregistration20261026'
    assert list(expected) == ['4f2a1a477586','5897539b121d','a358307ccc57','151aecfaca8c']
    raw = dict(read_only=True, sources=[dict(source='word-registration-20261026', workflow_id=workflow, read_only=True, availability='available', jobs=[dict(id=j, stage=s, role=r, status='queued', started_at=None, finished_at=None) for j,(s,r) in expected.items()])])
    result = projection.project_isolated_workflows(raw)['sources'][0]
    assert result['availability'] == 'available' and result['label'] == label


def test_word_task_source_exact():
    workflow, label, expected = projection._REGISTERED['word-task-20261026']
    assert workflow == 'wordtask20261026'
    assert list(expected) == ['c3c9f6650366','f641e1e00ef4','489cd923394d','700008c1c456']
    raw = dict(read_only=True, sources=[dict(source='word-task-20261026', workflow_id=workflow, read_only=True, availability='available', jobs=[dict(id=j, stage=s, role=r, status='queued', started_at=None, finished_at=None) for j,(s,r) in expected.items()])])
    result = projection.project_isolated_workflows(raw)['sources'][0]
    assert result['availability'] == 'available' and result['label'] == label


def test_powerpoint_registration_source_exact():
    workflow, label, expected = projection._REGISTERED['powerpoint-registration-20261027']
    assert workflow == 'powerpointregistration20261027'
    assert list(expected) == ['b587a7142995','24bf587f6384','02ca151cb660','d0466253da77']


def test_powerpoint_tool_source_exact():
    workflow, label, expected = projection._REGISTERED['powerpoint-tool-20261027']
    assert workflow == 'powerpointtool20261027'
    assert list(expected) == ['e61b2e0fa074','19315614734c','3526d1f90839','67791ba85554']


def test_powerpoint_creation_source_exact():
    workflow, label, expected = projection._REGISTERED['powerpoint-create-20261026']
    assert workflow == 'powerpointcreate20261026'
    assert list(expected) == ['7e881d7908aa','6a798090f5bd','5847207dccff','7dafab8e3b95']


def test_powerpoint_source_and_registered_bound():
    workflow, label, expected = projection._REGISTERED['powerpoint-draft-20261026']
    assert workflow == 'powerpointdraft20261026'
    assert list(expected) == ['4c6e9851a91e','eb3398820854','6c5696e2c6d7','69163e94def6']
    sources = [dict(source=k,workflow_id=w,read_only=True,availability='available',jobs=[dict(id=j,stage=s,role=r,status='queued',started_at=None,finished_at=None) for j,(s,r) in e.items()]) for k,(w,l,e) in projection._REGISTERED.items()]
    assert len(projection.project_isolated_workflows(dict(read_only=True,sources=sources))['sources']) == len(projection._REGISTERED)
    assert projection.project_isolated_workflows(dict(read_only=True,sources=sources+[sources[0]]))['sources'] == []
