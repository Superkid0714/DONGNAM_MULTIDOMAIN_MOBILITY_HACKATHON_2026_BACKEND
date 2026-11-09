from types import SimpleNamespace

from freshswitch_backend.planning.robust.quotes import QuoteInput, create, observations, read


def test_org_storage_and_demo_exclusion():
    principal = SimpleNamespace(org_id="demo", user_id="fixture", world_id=None)
    body = QuoteInput(
        item="sea_freight",
        origin="JINJU",
        destination="HKG",
        equipment="40ft",
        quoted_on="2026-11-09",
        minimum=100,
        maximum=200,
        basis="per_container",
        source="test-only",
        is_demo=True,
    )
    record = create(body, principal)
    assert read(principal, limit=100, offset=0)[0]["id"] == record["id"]
    assert read(SimpleNamespace(org_id="other"), limit=100, offset=0) == []
    assert observations("demo", body.quoted_on, "JINJU", "HKG", "40ft") == []
