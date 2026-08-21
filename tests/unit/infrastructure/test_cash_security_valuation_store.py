from app.infrastructure.cash_security_valuation_store import (
    CashSecurityValuationStore,
)


class _FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._calls = []

    def scard(self, key):
        self._calls.append(key)

    def execute(self):
        return [len(self._redis.sets.get(key, set())) for key in self._calls]


class _FakeRedis:
    def __init__(self, sets=None):
        self.sets = {key: set(values) for key, values in (sets or {}).items()}

    def smembers(self, key):
        return self.sets.get(key, set())

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


def test_list_active_contract_codes_parses_nonempty_index_members():
    store = CashSecurityValuationStore(
        _FakeRedis(
            {
                "cash_valuation:index_keys": {
                    "cash_valuation:instrument_positions:SSE:600033.XSHG",
                    "cash_valuation:instrument_positions:SSE:110075.XSHG",
                    "cash_valuation:instrument_positions:DCE:JD2609",
                },
                "cash_valuation:instrument_positions:SSE:600033.XSHG": {"P1"},
                "cash_valuation:instrument_positions:SSE:110075.XSHG": set(),
                "cash_valuation:instrument_positions:DCE:JD2609": {"P2", "P3"},
            }
        )
    )

    # 空集合成员表示合约已全部平仓，不应保持订阅。
    assert store.list_active_contract_codes() == {"600033.XSHG", "JD2609"}


def test_list_active_contract_codes_empty_without_index():
    store = CashSecurityValuationStore(_FakeRedis({}))

    assert store.list_active_contract_codes() == set()


def test_list_margin_dependency_codes_is_empty():
    assert CashSecurityValuationStore.list_margin_dependency_codes() == set()
