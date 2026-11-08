from datetime import date

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.data.provenance import asset_inventory
from freshswitch_backend.data.terminal import (
    TERMINAL_SNAPSHOT_DIR,
    find_sailing,
    hong_kong_sailings,
    load_terminal_snapshots,
    voyage_from_sailing,
)
from freshswitch_backend.forecast.history import load_history


def test_terminal_snapshots_load_with_source_and_fetch_time():
    snapshots = load_terminal_snapshots()
    assert {s.terminal.code for s in snapshots} == {"BNCT", "PNC"}
    for snapshot in snapshots:
        assert snapshot.source_url.startswith("https://")
        assert snapshot.fetched_at.tzinfo is not None
        for vessel in snapshot.vessels:
            assert vessel.call_sign
            assert vessel.port_rotation_url.startswith("https://")
            assert vessel.gaps
            assert vessel.etd.utcoffset().total_seconds() == 9 * 3600


def test_hong_kong_sailings_follow_busan_in_rotation():
    sailings = hong_kong_sailings(date(2026, 11, 18), date(2026, 11, 22))
    assert [s.vessel.vessel_name for s in sailings] == [
        "MAERSK NOTODDEN",
        "ASL HONG KONG",
        "OOCL MALAYSIA",
    ]
    for sailing in sailings:
        codes = [p.code for p in sailing.vessel.port_rotation]
        after = codes[codes.index("KRPUS") + 1 :]
        assert after[sailing.vessel.hong_kong_call.order_after_busan - 1] == "HKHKG"
        assert sailing.vessel.next_port == after[0]
    assert hong_kong_sailings(date(2026, 11, 20), date(2026, 11, 21)) == []


def test_call_signs_match_mof_history_records():
    history = {r.call_sign: r for r in load_history().records}
    for sailing in hong_kong_sailings():
        record = history[sailing.vessel.call_sign]
        assert record.vessel_name == sailing.vessel.vessel_name
    assert find_sailing(" VRME7 ").vessel.vessel_name == "OOCL MALAYSIA"
    assert find_sailing("NOPE") is None


def test_voyage_draft_keeps_scenario_untouched():
    original = get_scenario("default").model_dump(mode="json")
    sailing = find_sailing("3E9075")
    voyage = voyage_from_sailing(sailing, "V-REAL-A", transit_days=3)
    assert voyage.terminal == "부산신항 2부두"
    assert voyage.etd.isoformat() == "2026-11-18T22:00:00+09:00"
    assert voyage.cy_cutoff.isoformat() == "2026-11-17T12:00:00+09:00"
    assert "가정" in voyage.source and sailing.snapshot.source_url in voyage.source
    assert get_scenario("default").model_dump(mode="json") == original
    with pytest.raises(ValueError):
        voyage_from_sailing(
            sailing.model_copy(
                update={"vessel": sailing.vessel.model_copy(update={"cy_cutoff": None})}
            ),
            "V-X",
            transit_days=3,
        )


def test_terminal_snapshots_are_external_in_provenance():
    assets = {a["path"]: a for a in asset_inventory()}
    for path in TERMINAL_SNAPSHOT_DIR.glob("*.json"):
        asset = assets[f"snapshots/terminal/{path.name}"]
        assert asset["kind"] == "external_snapshot"
        assert asset["metadata"]["fetched_at"]
        assert asset["metadata"]["source_url"].startswith("https://")
        assert "servicekey" not in path.read_text(encoding="utf-8").lower()
