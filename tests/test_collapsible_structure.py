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


def test_four_new_summary_hints_in_actual_html():
    """Parse actual static/index.html and verify the four new-summary hint spans.

    Maps each hint ID to its wrapper ID and asserts:
    - Each ID occurs exactly once
    - Is a span with class="hint" and aria-live="polite"
    - Initial text is "Unavailable"
    - Nested in the wrapper's direct first child summary (not collapsed body)
    - Expected heading is preserved in the summary
    - No interactive controls in the summary
    """
    page = Structure((Path(__file__).resolve().parents[1] / 'static/index.html').read_text())
    assert not page.stack, 'HTML must be well-formed'

    # Map hint IDs to their wrapper IDs
    hint_to_wrapper = {
        'recent-workflows-hint': 'recent-workflows-panel',
        'recent-agent-activity-hint': 'recent-agent-activity-panel',
        'work-distribution-hint': 'work-distribution-panel',
        'token-ledger-hint': 'token-ledger-panel',
    }

    # Expected headings for each wrapper's summary
    expected_headings = {
        'recent-workflows-panel': 'Recent workflows',
        'recent-agent-activity-panel': 'Recent agent activity',
        'work-distribution-panel': 'Work distribution',
        'token-ledger-panel': 'Token ledger',
    }

    all_ids = [n['attrs']['id'] for n in page.nodes if 'id' in n['attrs']]
    by_id = {n['attrs']['id']: n for n in page.nodes if 'id' in n['attrs']}

    for hint_id, wrapper_id in hint_to_wrapper.items():
        # 1. Each hint ID occurs exactly once
        count = all_ids.count(hint_id)
        assert count == 1, f'{hint_id} occurs {count} times, expected exactly 1'

        # 2. Each wrapper ID occurs exactly once
        wrapper_count = all_ids.count(wrapper_id)
        assert wrapper_count == 1, f'{wrapper_id} occurs {wrapper_count} times, expected exactly 1'

        # 3. Hint is a span with class="hint" and aria-live="polite"
        hint_node = by_id[hint_id]
        assert hint_node['tag'] == 'span', f'{hint_id} must be a span, got {hint_node["tag"]}'
        assert hint_node['attrs'].get('class') == 'hint', \
            f'{hint_id} must have class="hint", got {hint_node["attrs"].get("class")!r}'
        assert hint_node['attrs'].get('aria-live') == 'polite', \
            f'{hint_id} must have aria-live="polite", got {hint_node["attrs"].get("aria-live")!r}'

        # 4. Initial text is "Unavailable"
        text = hint_node.get('text', '').strip()
        assert text == 'Unavailable', \
            f'{hint_id} initial text must be "Unavailable", got {text!r}'

        # 5. Nested in wrapper's direct first child summary (not collapsed body)
        wrapper_node = by_id[wrapper_id]
        assert wrapper_node['tag'] == 'details', f'{wrapper_id} must be details'
        assert len(wrapper_node['children']) >= 1, f'{wrapper_id} must have children'
        first_child = wrapper_node['children'][0]
        assert first_child['tag'] == 'summary', \
            f'{wrapper_id} first child must be summary, got {first_child["tag"]}'
        # The hint must be a descendant of the summary (direct first child of wrapper)
        assert first_child in hint_node['parents'], \
            f'{hint_id} must be nested in {wrapper_id}\'s summary (first child)'
        # Ensure it's NOT in a collapsed-body div
        for parent in hint_node['parents']:
            assert not (parent['tag'] == 'div' and 'collapsed-body' in (parent['attrs'].get('class') or '')), \
                f'{hint_id} must not be in a collapsed-body div'

        # 6. Expected heading preserved in the summary
        heading_text = [n.get('text', '').strip() for n in page.nodes
                        if n['tag'] == 'h2' and first_child in n['parents']]
        expected = expected_headings[wrapper_id]
        assert expected in ' '.join(heading_text), \
            f'{wrapper_id} summary must contain heading "{expected}", got {heading_text}'

        # 7. No interactive controls in the summary
        interactive_tags = {'button', 'input', 'select', 'textarea', 'a'}
        interactive_in_summary = [n for n in page.nodes
                                  if n['tag'] in interactive_tags and first_child in n['parents']]
        assert not interactive_in_summary, \
            f'{wrapper_id} summary contains interactive controls: {[n["tag"] for n in interactive_in_summary]}'


def test_health_summary_hints_stay_in_visible_headers():
    page = Structure((Path(__file__).resolve().parents[1] / 'static/index.html').read_text())
    mapping = {'system-health-hint': 'cluster-health-panel', 'ups-summary-hint': 'ups-panel',
               'reliability-summary-hint': 'reliability-section', 'accounts-summary-hint': 'user-management'}
    for hint_id, panel_id in mapping.items():
        hints = [n for n in page.nodes if n['attrs'].get('id') == hint_id]
        panels = [n for n in page.nodes if n['attrs'].get('id') == panel_id]
        assert len(hints) == len(panels) == 1
        hint, panel = hints[0], panels[0]
        assert hint['tag'] == 'span' and hint['attrs'].get('aria-live') == 'polite'
        assert panel['children'][0]['tag'] == 'summary'
        assert panel['children'][0] in hint['parents']
        assert hint.get('text', '').strip() == ('Admin only · Unavailable' if panel_id == 'user-management' else 'Unavailable')
        if panel_id == 'user-management':
            assert 'hidden' in panel['attrs']
