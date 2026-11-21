"""The Signals page (/signals): the shared signal logic (run with Node), the local routes and the online copy."""
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_signal_logic_with_node():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "signals_core.test.js")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_local_server_serves_the_page_and_its_script():
    from scalper.server import serve
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    threading.Thread(target=serve, args=(type("E", (), {"root": ROOT})(), port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            page = urllib.request.urlopen(base + "/signals", timeout=2).read().decode()
            break
        except OSError:
            time.sleep(0.1)
    js = urllib.request.urlopen(base + "/signals_core.js", timeout=2).read().decode()
    assert "<h1>Weekend signals</h1>" in page and 'src="signals_core.js"' in page and "buildPosition" in js
    assert len(json.loads(urllib.request.urlopen(base + "/api/deploy", timeout=2).read())["strategies"]) == 4


def test_online_copy_ships_the_page_and_links_it():
    cloud = (ROOT / "cloud.py").read_text()
    assert '("signals.html", "signals.html")' in cloud and '"signals_core.js"' in cloud
    assert '"/signals" ? "signals.html"' in (ROOT / "static.js").read_text()
    for page in ("options.html", "advisor.html", "breakout.html", "deploy.html", "signals.html"):
        assert "/signals" in (ROOT / "scalper" / page).read_text()
