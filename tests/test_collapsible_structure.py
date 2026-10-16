"""Regression checks against the shipped HTML, not mock details elements."""
from html.parser import HTMLParser
from pathlib import Path


class Structure(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack = []
        self.nodes = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), parents=list(self.stack), children=[])
        if self.stack:
            self.stack[-1]['children'].append(node)
        self.nodes.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img',
                       'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1]['tag'] == tag, f'Unbalanced {tag}'
        self.stack.pop()

    def handle_data(self, data):
        for node in self.stack:
            node['text'] = node.get('text', '') + data


def test_actual_collapsible_structure():
    page = Structure((Path(__file__).resolve().parents[1] / 'static/index.html').read_text())
    assert not page.stack
    ids = [n['attrs']['id'] for n in page.nodes if 'id' in n['attrs']]
    assert len(ids) == len(set(ids)), 'duplicate IDs break renderer/control targeting'
    by_id = {n['attrs']['id']: n for n in page.nodes if 'id' in n['attrs']}
    wrappers = {'reliability-section': False, 'start-build-panel': True,
                'cluster-health-panel': False, 'ups-panel': False,
                'work-distribution-panel': False, 'token-ledger-panel': False,
                'recent-agent-activity-panel': True,
                'alerts-panel': False, 'recent-workflows-panel': False,
                'user-management': False}
    for key, opened in wrappers.items():
        node = by_id[key]
        assert node['tag'] == 'details', f'{key} must be a details element'
        assert ('open' in node['attrs']) is opened, f'{key} open attr mismatch'
        assert node['children'][0]['tag'] == 'summary', f'{key} first child must be summary'
        summary = node['children'][0]
        assert not any(n['tag'] in {'button', 'input', 'select', 'textarea', 'a'}
                       and summary in n['parents'] for n in page.nodes), \
            f'{key} summary must not contain interactive controls'
    roots = {'reliability-card': 'reliability-section',
             'recent-workflow-reliability': 'reliability-section',
             'workflow-bottlenecks': 'reliability-section',
             'completion-recovery': 'reliability-section',
             'cluster-health': 'cluster-health-panel', 'ups-status': 'ups-panel',
             'projects': 'work-distribution-panel', 'models': 'token-ledger-panel',
             'start-build-form': 'start-build-panel',
             'jobs': 'recent-agent-activity-panel'}
    for root, wrapper in roots.items():
        assert by_id[wrapper] in by_id[root]['parents']
        assert by_id[root]['tag'] != 'details'
    # alerts and workflows are now inside their details wrappers
    assert by_id['alerts-panel'] in by_id['alerts']['parents']
    assert by_id['recent-workflows-panel'] in by_id['workflows']['parents']
    # user-management is a details with hidden
    assert by_id['user-management']['tag'] == 'details'
    assert 'hidden' in by_id['user-management']['attrs']
    assert 'aria-labelledby' in by_id['user-management']['attrs']
    # alerts-hint must have aria-live polite
    assert by_id['alerts-hint']['attrs'].get('aria-live') == 'polite'
    assert by_id['alerts-panel']['children'][0] in by_id['alerts-hint']['parents']
    assert by_id['alerts-panel'] in by_id['alerts-history']['parents']
    for key in ['user-create-form', 'user-table-body']:
        assert by_id['user-management'] in by_id[key]['parents']
    for key in ['user-confirm-modal', 'user-password-modal', 'metrics', 'running-now']:
        assert not any(n['tag'] == 'details' for n in by_id[key]['parents'])


def test_recent_activity_heading_and_table_are_preserved():
    page = Structure((Path(__file__).resolve().parents[1] / 'static/index.html').read_text())
    wrappers = [n for n in page.nodes if n['attrs'].get('id') == 'recent-agent-activity-panel']
    jobs = [n for n in page.nodes if n['attrs'].get('id') == 'jobs']
    assert len(wrappers) == len(jobs) == 1
    wrapper = wrappers[0]
    summary = wrapper['children'][0]
    assert summary['tag'] == 'summary'
    assert [n.get('text', '').strip() for n in page.nodes
            if n['tag'] == 'h2' and summary in n['parents']] == ['Recent agent activity']
    assert 'Newest first' in summary.get('text', '')
    assert jobs[0]['tag'] == 'tbody' and wrapper in jobs[0]['parents']
    tables = [n for n in page.nodes if n['tag'] == 'table' and wrapper in n['parents']]
    assert len(tables) == 1 and tables[0] in jobs[0]['parents']
    assert [n.get('text', '').strip() for n in page.nodes
            if n['tag'] == 'th' and tables[0] in n['parents']] == [
                'Stage', 'Project', 'Status', 'Model', 'Tokens', 'Duration']
