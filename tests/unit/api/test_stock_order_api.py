from app.main import app


def test_stock_order_routes_are_loaded_into_openapi():
    schema = app.openapi()

    assert "/api/stock/orders" in schema["paths"]
    assert "/api/stock/orders/{order_id}/cancel" in schema["paths"]
    assert "/api/stock/orders/{order_id}/fee-components" in schema["paths"]
