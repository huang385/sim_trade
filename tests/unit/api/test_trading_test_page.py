from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_trading_test_page_and_assets_are_available() -> None:
    """本地联调页面及其静态资源应由FastAPI同源提供。"""

    page_response = client.get("/test-trading")
    css_response = client.get("/static/trading_test.css")
    js_response = client.get("/static/trading_test.js")

    assert page_response.status_code == 200
    assert "Sim Trade 实时交易测试台" in page_response.text
    assert "500ms 实时刷新" in page_response.text
    assert 'id="order-form"' in page_response.text
    assert 'id="metric-daily-close"' in page_response.text
    assert 'id="metric-daily-commission"' in page_response.text
    assert 'id="detail-used-commission"' in page_response.text
    assert "当日平仓盈亏" in page_response.text
    assert 'id="trade-detail-dialog"' in page_response.text
    assert 'id="login-form"' in page_response.text
    assert 'id="logout-button"' in page_response.text

    assert css_response.status_code == 200
    assert "--cyan:" in css_response.text
    assert ".detail-grid" in css_response.text
    assert ".trade-dialog" in css_response.text

    assert js_response.status_code == 200
    assert "REFRESH_INTERVAL_MS = 500" in js_response.text
    assert 'apiFetch("/api/orders"' in js_response.text
    assert "account.daily_close_pnl" in js_response.text
    assert 'event.event_type === "RISK_STATE_CHANGED"' in js_response.text
    assert "risk_realtime_version" in js_response.text
    assert "account.daily_commission" in js_response.text
    assert "trade.daily_close_pnl" in js_response.text
    assert "/position-allocations" in js_response.text
    assert "/api/auth/login" in js_response.text
    assert "/api/auth/refresh" in js_response.text
    assert "/api/market-data/subscriptions/prepare" in js_response.text
    assert "/api/market-data/subscriptions/status" in js_response.text
    assert "ensureDerivativeMarketReady(payload)" in js_response.text
    assert "Authorization: `Bearer ${state.accessToken}`" in js_response.text
    assert "localStorage.setItem" not in js_response.text
