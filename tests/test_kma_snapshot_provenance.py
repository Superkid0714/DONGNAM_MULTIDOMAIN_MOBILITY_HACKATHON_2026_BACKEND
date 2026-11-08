from datetime import UTC, datetime

from freshswitch_backend.data import provenance
from freshswitch_backend.integrations.kma_forecast import (
    FORECAST_DATASET_URL,
    KMA_SNAPSHOT_PATH,
    WARNING_DATASET_URL,
    ForecastPoint,
    KmaSnapshot,
    load_snapshot,
    save_snapshot,
)


def test_kma_snapshot_is_listed_as_external_snapshot_with_source(tmp_path, monkeypatch):
    relative = KMA_SNAPSHOT_PATH.relative_to(provenance.DATA_ROOT)
    fetched = datetime(2026, 11, 7, 1, 0, tzinfo=UTC)
    snapshot = KmaSnapshot(
        fetched_at=fetched,
        points=[ForecastPoint(at=fetched, wind_speed_ms=6.2, wave_height_m=1.0)],
    )
    save_snapshot(snapshot, tmp_path / relative)
    monkeypatch.setattr(provenance, "DATA_ROOT", tmp_path)

    assets = {asset["path"]: asset for asset in provenance.asset_inventory()}

    asset = assets[relative.as_posix()]
    assert asset["kind"] == "external_snapshot"
    assert asset["verification_status"] == "not_independently_verified"
    assert "기상청_단기예보 조회서비스" in asset["metadata"]["source"]
    assert asset["metadata"]["source_url"] == [FORECAST_DATASET_URL, WARNING_DATASET_URL]
    assert asset["metadata"]["fetched_at"].startswith("2026-11-07T01:00:00")
    assert load_snapshot(tmp_path / relative) == snapshot


def test_no_kma_snapshot_is_listed_or_loaded_until_one_is_fetched(tmp_path, monkeypatch):
    relative = KMA_SNAPSHOT_PATH.relative_to(provenance.DATA_ROOT)
    monkeypatch.setattr(provenance, "DATA_ROOT", tmp_path)

    assert load_snapshot(tmp_path / relative) is None
    paths = {asset["path"] for asset in provenance.asset_inventory()}
    assert relative.as_posix() not in paths


def test_repository_kma_snapshot_is_listed_only_as_a_fetched_external_snapshot():
    relative = KMA_SNAPSHOT_PATH.relative_to(provenance.DATA_ROOT).as_posix()
    assets = {asset["path"]: asset for asset in provenance.asset_inventory()}
    snapshot = load_snapshot()

    if snapshot is None:
        assert relative not in assets
        return
    asset = assets[relative]
    assert asset["kind"] == "external_snapshot"
    assert asset["verification_status"] == "not_independently_verified"
    assert asset["metadata"]["source_url"] == [FORECAST_DATASET_URL, WARNING_DATASET_URL]
    assert datetime.fromisoformat(asset["metadata"]["fetched_at"]) == snapshot.fetched_at
