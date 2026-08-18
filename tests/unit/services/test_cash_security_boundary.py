import ast
from pathlib import Path


def test_stock_core_modules_do_not_import_derivative_business_services():
    root = Path(__file__).resolve().parents[3]
    modules = (
        "app/services/stock_order_service.py",
        "app/services/stock_order_cancellation_service.py",
        "app/services/stock_order_validation_service.py",
        "app/services/cash_security_funds_service.py",
        "app/services/cash_security_fee_service.py",
        "app/services/cash_security_order_event_service.py",
        "app/services/cash_security_matching_service.py",
        "app/services/cash_security_market_tick_matching_service.py",
        "app/services/cash_security_settlement_service.py",
        "app/services/cash_security_position_service.py",
        "app/services/convertible_bond_order_service.py",
    )
    forbidden_modules = {
        "app.services.futures_order_service",
        "app.services.derivative_order_validator",
        "app.services.futures_margin_service",
        "app.services.derivative_trade_settlement_service",
        "app.services.position_close_allocator",
        "app.services.order_freeze_service",
        "app.services.fee_calculator",
        "app.services.order_validation_service",
    }

    for relative_path in modules:
        source = (root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert not (imported_modules & forbidden_modules), relative_path
        assert "OffsetFlag" not in imported_names, relative_path


def test_stock_create_schema_has_no_offset_flag_field():
    from app.schemas.order_schema import (
        ConvertibleBondOrderCreateRequest,
        StockOrderCreateRequest,
    )

    assert "offset_flag" not in StockOrderCreateRequest.model_fields
    assert "offset_flag" not in ConvertibleBondOrderCreateRequest.model_fields
