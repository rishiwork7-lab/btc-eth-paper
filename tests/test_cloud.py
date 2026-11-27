import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud import in_entry_window  # noqa: E402


def ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


def test_friday_runs_stay_up_through_the_condor_entry():
    assert not in_entry_window(ts(2026, 11, 20, 16, 59))     # Friday, before the window
    assert in_entry_window(ts(2026, 11, 20, 17, 0))          # a run starting from 17:00 UTC stays up...
    assert in_entry_window(ts(2026, 11, 20, 18, 5))          # ...through the 18:00-18:10 UTC entry
    assert not in_entry_window(ts(2026, 11, 20, 18, 12))
    assert not in_entry_window(ts(2026, 11, 19, 17, 30))     # Thursday: no condor entry


def test_simple_subpath_has_all_assets_and_data():
    cloud = (ROOT / "cloud.py").read_text()
    assert '("simple.html", "simple.html")' in cloud
    assert '("simple.html", "simple/index.html")' in cloud
    assert 'SITE / "simple" / "static.js"' in cloud
    assert 'SITE / "simple" / "signals_core.js"' in cloud
    assert 'SITE / "simple" / "data"' in cloud
