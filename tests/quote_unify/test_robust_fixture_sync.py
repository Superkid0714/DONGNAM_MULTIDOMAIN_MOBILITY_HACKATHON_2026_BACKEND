import json
import runpy
from pathlib import Path


def test_backend_fixture_matches_current_seeds():
    root = Path(__file__).resolve().parents[2]
    fixture = root / "tests/quote_unify/fixtures/robust_savings.json"
    snapshots = runpy.run_path(str(root / "scripts/sync_robust_fixture.py"))["snapshots"]()
    assert json.loads(fixture.read_text(encoding="utf-8")) == snapshots
    assert round(float(snapshots["17"]["CA-01:jinju"]["break_even_threshold_container"])) == 5342070
    assert round(float(snapshots["15"]["CA-01:jinju"]["break_even_threshold_container"])) == 4555670
