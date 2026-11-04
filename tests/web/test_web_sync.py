"""The web UI's session sync, run under node with a stand-in DOM (tests/web/session_sync.js); skipped without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
APP_JS = HERE.parent.parent / "valet" / "control" / "static" / "app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_failed_history_fetch_is_retried_not_skipped():
    """Live events queued during a failing fetch mustn't move the cursor past the history it was fetching."""
    out = subprocess.run(["node", str(HERE / "session_sync.js"), str(APP_JS)], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    result = json.loads(out)
    assert result["applied"] == ["m1", "m2", "m3", "m4", "m5"]
    assert [u.split("?")[1] for u in result["urls"]] == ["after=0&limit=500"] * 2
