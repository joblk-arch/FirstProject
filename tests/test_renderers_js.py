"""Pytest wrapper that runs the Node.js renderer regression harness.

Invokes the Node built-in fs/vm/assert harness (tests/js/renderers.test.js)
with a timeout. Fails clearly if Node is missing (never skips) and fails with
the harness output if the harness exits non-zero or times out.
"""

import shutil
import subprocess
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / "js" / "renderers.test.js"
TIMEOUT_SECONDS = 60


def test_node_renderers_harness():
    node = shutil.which("node")
    assert node is not None, (
        "node executable not found on PATH; install nodejs (see Dockerfile.sandbox) "
        "to run the JS renderer regression tests"
    )
    assert HARNESS.exists(), f"Node harness not found at {HARNESS}"

    try:
        result = subprocess.run(
            [node, str(HARNESS)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Node harness timed out after {TIMEOUT_SECONDS}s"
        ) from exc

    assert result.returncode == 0, (
        f"Node harness exited with code {result.returncode}:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
