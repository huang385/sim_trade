import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _python_files(relative_directory: str):
    return sorted((PROJECT_ROOT / relative_directory).rglob("*.py"))


def test_repositories_do_not_control_transactions_or_depend_on_upper_layers():
    forbidden_import_prefixes = (
        "app.services",
        "app.infrastructure",
        "app.matching",
        "app.realtime",
        "app.workers",
    )
    violations: list[str] = []

    for path in _python_files("app/repositories"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"commit", "rollback"}
            ):
                violations.append(f"{path.name}:{node.lineno}:transaction")
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_import_prefixes):
                    violations.append(f"{path.name}:{node.lineno}:{module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_import_prefixes):
                        violations.append(
                            f"{path.name}:{node.lineno}:{alias.name}"
                        )

    assert violations == []


def test_core_product_enums_have_one_class_definition_each():
    expected_sources = {
        "AccountType": "account_enums.py",
        "InstrumentType": "instrument_enums.py",
        "MarketType": "market_enums.py",
        "ExchangeID": "market_enums.py",
        "OrderType": "order_enums.py",
    }
    definitions: dict[str, list[str]] = {
        name: [] for name in expected_sources
    }

    for path in _python_files("app"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in definitions:
                definitions[node.name].append(path.name)

    assert definitions == {
        name: [source] for name, source in expected_sources.items()
    }


def test_api_modules_do_not_import_authorization_dependency_from_auth_api():
    violations: list[str] = []
    for path in _python_files("app/api"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.api.auth_api":
                continue
            if any(
                alias.name == "get_account_authorization_service"
                for alias in node.names
            ):
                violations.append(f"{path.name}:{node.lineno}")

    assert violations == []
