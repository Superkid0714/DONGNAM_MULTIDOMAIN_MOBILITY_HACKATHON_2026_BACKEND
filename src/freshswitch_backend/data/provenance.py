"""Audit recorded provenance; a citation is not independently verified evidence."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.integrations.kma_forecast import (
    FORECAST_DATASET,
    FORECAST_DATASET_URL,
    KMA_SNAPSHOT_PATH,
    WARNING_DATASET,
    WARNING_DATASET_URL,
)

DATA_ROOT = Path(__file__).parent
ASSET_SOURCES = {
    KMA_SNAPSHOT_PATH.relative_to(DATA_ROOT).as_posix(): {
        "source": f"{FORECAST_DATASET}·{WARNING_DATASET} (공공데이터포털, 부산신항 격자·부산 특보구역)",
        "source_url": [FORECAST_DATASET_URL, WARNING_DATASET_URL],
    },
}


def source_kind(source: str) -> str:
    if not source.strip():
        return "missing"
    if any(word in source for word in ("시드", "목업", "제안서", "팀 확정", "시연값")):
        return "demo_or_business_rule"
    if any(word in source for word in ("가정", "모의", "미확인", "보정값", "환산")):
        return "assumption_or_derived"
    return "cited_unverified"


def source_entries(value, path: str = "scenario") -> list[dict]:
    entries = []
    if isinstance(value, dict):
        if "source" in value:
            source = value.get("source") or ""
            entries.append(
                {
                    "path": path,
                    "kind": source_kind(source),
                    "recorded_source": source,
                    "verification_status": "not_independently_verified",
                    "values": {
                        key: item
                        for key, item in value.items()
                        if key != "source" and not isinstance(item, (dict, list))
                    },
                    "recorded_dates": re.findall(r"20\d{2}[./-]\d{1,2}(?:[./-]\d{1,2})?", source),
                    "recorded_urls": re.findall(r"https?://[^\s)]+", source),
                }
            )
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                entries.extend(source_entries(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            label = item.get("id", index) if isinstance(item, dict) else index
            entries.extend(source_entries(item, f"{path}[{label}]"))
    return entries


def asset_inventory() -> list[dict]:
    assets = []
    for path in sorted(DATA_ROOT.rglob("*.json")):
        content = path.read_bytes()
        document = json.loads(content)
        relative = path.relative_to(DATA_ROOT).as_posix()
        simulated = relative.startswith(("scenarios/", "quality/", "snapshots/demo/"))
        metadata = dict(ASSET_SOURCES.get(relative, {})) | {
            key: document[key]
            for key in (
                "source",
                "label",
                "source_url",
                "fetched_at",
                "updated_at",
                "start",
                "end",
                "count",
            )
            if key in document
        }
        assets.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "kind": "simulation" if simulated else "external_snapshot",
                "metadata": metadata,
                "verification_status": "not_independently_verified",
            }
        )
    return assets


def provenance_catalog(scenario_id: str = "default") -> dict:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise KeyError(scenario_id)
    entries = source_entries(scenario.model_dump(mode="json"))
    return {
        "scenario_id": scenario_id,
        "scope": "Sources recorded in repository data; not external source verification",
        "counts": dict(Counter(entry["kind"] for entry in entries)),
        "entries": entries,
        "assets": asset_inventory(),
        "limitations": [
            "Scenario shipments, coordinates, schedules and contract prices are demonstration inputs.",
            "Quality seed observations are not actual arrival inspection records.",
            "Published figures, converted estimates and contractual assumptions must not be presented as one measured dataset.",
            "Recorded dates are publication/reference dates, not proof that a price is currently valid.",
            "Terminal berth snapshots are published plans at fetch time, not actual departures.",
            "KMA weather snapshots are forecasts and warnings as issued at fetch time, not observed conditions.",
        ],
    }
