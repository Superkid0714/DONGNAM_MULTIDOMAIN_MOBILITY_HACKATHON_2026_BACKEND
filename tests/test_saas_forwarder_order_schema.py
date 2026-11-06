from saas_fixtures import saas_app


def test_saas_openapi_forwarder_orders_typed_with_nullable_money() -> None:
    spec = saas_app.openapi()
    schemas = spec["components"]["schemas"]
    orders = spec["paths"]["/api/saas/forwarder/plans/{plan_id}/orders"]["get"]
    items = orders["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert items["$ref"].endswith("/ForwarderOrder")
    confirm = spec["paths"]["/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm"]
    body = confirm["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert body["$ref"].endswith("/ForwarderOrder")

    confirmation = schemas["ForwarderShipperConfirmation"]["properties"]
    for name in ("cost_before", "cost_after", "cost_delta", "net_cost_delta"):
        assert {"type": "null"} in confirmation[name]["anyOf"]
    cancellation = schemas["ForwarderCancellation"]["properties"]
    assert {"type": "null"} in cancellation["fee"]["anyOf"]
    assert cancellation["cancelled_pallets"]["type"] == "integer"
