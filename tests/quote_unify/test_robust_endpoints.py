import runpy
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from freshswitch_backend.api.auth import current_principal
from freshswitch_backend.planning.robust.api import router


def test_new_evaluate_endpoints_and_rates():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    path = "/api/v1/robust-savings/evaluate"
    assert client.get("/api/v1/robust-savings/rates").status_code == 401
    app.dependency_overrides[current_principal] = lambda: object()
    root = Path(__file__).resolve().parents[2]
    namespace = runpy.run_path(str(root / "scripts/sync_robust_fixture.py"))
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    from freshswitch_backend.planning.robust.rates import seed_inputs

    quote = SimpleNamespace(
        shipment_date=date(2026, 7, 20),
        cargo=SimpleNamespace(pallets=8, gross_weight_kg=Decimal(3320)),
    )
    for total, expected in ((17, 5342070), (15, 4555670)):
        body = seed_inputs(quote, "medium")
        body.container_pallets = total
        response = client.post(path, json=body.model_dump(mode="json"))
        assert response.status_code == 200
        result = response.json()
        assert result.pop("robust") == {k: v for k, v in result.items() if k != "estimate"}
        assert result.pop("estimate")["settlement_eligible"] is False
        assert round(Decimal(result["break_even_threshold_container"])) == expected
        assert result["settlement_eligible"] is False
        assert not any("pct" in key for key in result)
        assert result == {
            k: v
            for k, v in namespace["snapshots"]()[str(total)]["CA-01:jinju"].items()
            if k not in {"shipper_pallets", "container_pallets"}
        }
    assert client.get("/api/v1/robust-savings/rates").status_code == 200
