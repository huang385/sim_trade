import pytest

from app.matching.base import MatchingEngine
from app.matching.engines.vn import VnMatchingEngine
from app.matching.registry import create_matching_engine


def test_registry_creates_vn_engine():
    engine = create_matching_engine("VN")

    assert isinstance(engine, VnMatchingEngine)
    assert isinstance(engine, MatchingEngine)


@pytest.mark.parametrize("name", ["vn", "Vn", "  VN  "])
def test_registry_normalizes_engine_name(name):
    assert isinstance(create_matching_engine(name), VnMatchingEngine)


def test_registry_rejects_unknown_engine_with_clear_error():
    with pytest.raises(ValueError, match="未知撮合引擎名称.*UNKNOWN.*VN"):
        create_matching_engine("UNKNOWN")
