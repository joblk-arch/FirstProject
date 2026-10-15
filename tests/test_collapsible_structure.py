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


def test_actual_collapsible_structure():
    page = Structure((Path(__file__).resolve().parents[1] / 'static/index.html').read_text())
    assert not page.stack
    ids = [n['attrs']['id'] for n in page.nodes if 'id' in n['attrs']]
    assert len(ids) == len(set(ids)), 'duplicate IDs break renderer/control targeting'
    by_id = {n['attrs']['id']: n for n in page.nodes if 'id' in n['attrs']}
    wrappers = {'reliability-section': False, 'start-build-panel': True,
                'cluster-health-panel': False, 'ups-panel': False,
                'work-distribution-panel': False, 'token-ledger-panel': False}
    for key, opened in wrappers.items():
        node = by_id[key]
        assert node['tag'] == 'details'
        assert ('open' in node['attrs']) is opened
        assert node['children'][0]['tag'] == 'summary'
        summary = node['children'][0]
        assert not any(n['tag'] in {'button', 'input', 'select', 'textarea', 'a'}
                       and summary in n['parents'] for n in page.nodes)
    roots = {'reliability-card': 'reliability-section',
             'recent-workflow-reliability': 'reliability-section',
             'workflow-bottlenecks': 'reliability-section',
             'completion-recovery': 'reliability-section',
             'cluster-health': 'cluster-health-panel', 'ups-status': 'ups-panel',
             'projects': 'work-distribution-panel', 'models': 'token-ledger-panel',
             'start-build-form': 'start-build-panel'}
    for root, wrapper in roots.items():
        assert by_id[wrapper] in by_id[root]['parents']
        assert by_id[root]['tag'] != 'details'
    for key in ['metrics', 'alerts', 'running-now', 'jobs', 'workflows', 'user-management']:
        assert not any(n['tag'] == 'details' for n in by_id[key]['parents'])
    assert 'hidden' in by_id['user-management']['attrs']
    assert by_id['user-management']['tag'] == 'section'
