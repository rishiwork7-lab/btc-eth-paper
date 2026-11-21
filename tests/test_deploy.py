"""The "Deploy strategies" page: its data file, the local routes and the online copy."""
import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_deploy_data_lists_the_four_strategies_on_both_coins():
    d = json.loads((ROOT / "scalper" / "deploy.json").read_text())
    names = [s["name"] for s in d["strategies"]]
    assert names == ["Weekend trend spread", "Friday-night trend spread", "Friday-night tight condor", "Friday-night condor"]
    for s in d["strategies"]:
        for c in ("BTC", "ETH"):
            x = s["coins"][c]
            assert x["trades"] >= 100 and 0 < x["win_pct"] <= 100 and x["t"] > 2 and x["size_x"] > 0
            assert x["status"] in ("paper", "off", "new")
    assert d["portfolio"]["out_of_sample"]["max_dd_pct"] > 0 and d["risk_rules"] and d["steps"]


def test_local_server_serves_the_page_and_its_data():
    import socket
    import time
    from scalper.server import serve
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    threading.Thread(target=serve, args=(type("E", (), {"root": ROOT})(), port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            page = urllib.request.urlopen(base + "/deploy", timeout=2).read().decode()
            break
        except OSError:
            time.sleep(0.1)
    data = json.loads(urllib.request.urlopen(base + "/api/deploy", timeout=2).read())
    assert "<h1>Deploy strategies</h1>" in page and len(data["strategies"]) == 4


def test_online_copy_links_and_maps_the_page():
    js = (ROOT / "static.js").read_text()
    assert '"/api/deploy"' in js and '"deploy.json"' in js and '"deploy.html"' in js
    cloud = (ROOT / "cloud.py").read_text()
    assert '("deploy.html", "deploy.html")' in cloud and '"deploy.json"' in cloud
    for page in ("options.html", "advisor.html", "breakout.html", "deploy.html"):
        assert "/deploy" in (ROOT / "scalper" / page).read_text()
