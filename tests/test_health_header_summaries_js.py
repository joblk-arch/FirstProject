import shutil
import subprocess
from pathlib import Path


def test_health_header_behavior():
    node = shutil.which('node')
    assert node, 'Node is required for header behavior tests'
    harness = Path(__file__).parent / 'js' / 'health-header-summaries.test.js'
    result = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
