import ast
import importlib
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
        "InstrumentType": "option_enums.py",
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


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
        elif isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
    return result


def test_required_business_modules_have_public_entry_points():
    names = {
        "auth",
        "accounts",
        "instruments",
        "orders",
        "trades",
        "futures",
        "options",
        "risk",
        "daily_settlement",
        "market_data",
        "realtime",
    }

    for name in names:
        package = PROJECT_ROOT / "app" / "modules" / name
        assert (package / "__init__.py").is_file()
        assert (package / "facade.py").is_file()
        module = importlib.import_module(f"app.modules.{name}")
        assert module.__all__
        for public_name in module.__all__:
            assert getattr(module, public_name) is not None


def test_shared_never_depends_on_business_modules():
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{module}"
        for path in _python_files("app/shared")
        for module in _imports(path)
        if module.startswith("app.modules")
    ]

    assert violations == []


def test_api_and_workers_use_public_boundaries_not_legacy_repositories():
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{module}"
        for directory in ("app/api", "app/workers")
        for path in _python_files(directory)
        for module in _imports(path)
        if module.startswith("app.repositories")
    ]

    assert violations == []


def test_workers_do_not_import_or_mutate_orm_models_directly():
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{module}"
        for path in _python_files("app/workers")
        for module in _imports(path)
        if module == "app.models" or module.startswith("app.models.")
    ]

    assert violations == []


def test_infrastructure_does_not_depend_on_product_module_internals():
    forbidden = (
        "app.modules.futures.",
        "app.modules.options.",
        "app.modules.orders.",
    )
    violations = [
        f"{path.relative_to(PROJECT_ROOT)}:{module}"
        for path in _python_files("app/infrastructure")
        for module in _imports(path)
        if module.startswith(forbidden)
    ]

    assert violations == []


def test_product_modules_do_not_import_each_other():
    futures_imports = {
        module
        for path in _python_files("app/modules/futures")
        for module in _imports(path)
    }
    options_imports = {
        module
        for path in _python_files("app/modules/options")
        for module in _imports(path)
    }

    assert not any(
        module.startswith("app.modules.options")
        for module in futures_imports
    )
    assert not any(
        module.startswith("app.modules.futures")
        for module in options_imports
    )


def test_business_module_import_graph_has_no_cycles():
    module_names = {
        path.name
        for path in (PROJECT_ROOT / "app" / "modules").iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }
    graph = {name: set() for name in module_names}
    for name in module_names:
        for path in _python_files(f"app/modules/{name}"):
            for imported in _imports(path):
                parts = imported.split(".")
                if len(parts) >= 3 and parts[:2] == ["app", "modules"]:
                    target = parts[2]
                    if target in module_names and target != name:
                        graph[name].add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        assert name not in visiting, f"module dependency cycle: {name}"
        if name in visited:
            return
        visiting.add(name)
        for target in graph[name]:
            visit(target)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


def test_model_registry_loads_every_exported_orm_model_once():
    from app import models
    from app.infrastructure.database.model_registry import metadata

    table_names = list(metadata.tables)
    assert len(table_names) == len(set(table_names))
    for name in models.__all__:
        model = getattr(models, name)
        assert model.__table__.key in metadata.tables
