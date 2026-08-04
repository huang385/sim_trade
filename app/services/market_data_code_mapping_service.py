from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.enums.option_enums import InstrumentType
from app.repositories.instrument_market_data_mapping_repository import (
    InstrumentMarketDataMappingRepository,
)


OPTION_INSTRUMENT_TYPES = {
    InstrumentType.FUTURES_OPTION.value,
    InstrumentType.INDEX_OPTION.value,
}


@dataclass(frozen=True)
class MarketDataCodeMappingSnapshot:
    """
    单次行情订阅使用的不可变双向代码映射。

    internal_to_source用于向YMM Live Data发起订阅；source_to_internal用于把回调
    中的外部代码还原成订单、持仓和Redis统一使用的内部order_book_id。
    快照随订阅回调闭包保存，避免每条Tick查询数据库，也避免重连期间新旧
    映射互相污染。
    """

    internal_to_source: Mapping[str, str]
    source_to_internal: Mapping[str, str]

    @property
    def source_codes(self) -> frozenset[str]:
        return frozenset(self.source_to_internal)

    def to_source(self, internal_code: str) -> str:
        normalized = normalize_code(internal_code)
        return self.internal_to_source.get(normalized, normalized)

    def to_internal(self, source_code: str) -> str:
        normalized = normalize_code(source_code)
        return self.source_to_internal.get(normalized, normalized)

    @classmethod
    def identity(
        cls,
        codes: set[str] | frozenset[str] | list[str],
    ) -> "MarketDataCodeMappingSnapshot":
        normalized_codes = {normalize_code(code) for code in codes}
        mapping = {code: code for code in normalized_codes}
        return cls(
            internal_to_source=MappingProxyType(mapping),
            source_to_internal=MappingProxyType(dict(mapping)),
        )


class MarketDataCodeMappingService:
    """在内部合约代码与指定行情源代码之间建立订阅期内映射。"""

    DATA_SOURCE = "YMM_LIVE_DATA"

    def __init__(
        self,
        repository: InstrumentMarketDataMappingRepository,
    ) -> None:
        self.repository = repository

    @staticmethod
    def _default_source_code(*, order_book_id: str, instrument_type: str) -> str:
        """
        生成YMM Live Data默认代码。

        项目内部期权代码使用JD2609-C-4000形式，而源端使用
        JD2609C4000形式；商品期权和股指期权都统一移除分隔符。普通期货
        和指数默认保持原代码。特殊行情源代码仍可由映射表显式覆盖。
        """

        if instrument_type in OPTION_INSTRUMENT_TYPES:
            return order_book_id.replace("-", "")
        return order_book_id

    def build_snapshot(
        self,
        db: Session,
        internal_codes: set[str] | frozenset[str] | list[str],
    ) -> MarketDataCodeMappingSnapshot:
        normalized_codes = {normalize_code(code) for code in internal_codes}
        rows = self.repository.list_instruments_with_mapping(
            db,
            data_source=self.DATA_SOURCE,
            order_book_ids=normalized_codes,
        )
        by_internal_code = {
            normalize_code(instrument.order_book_id): (instrument, mapping)
            for instrument, mapping in rows
        }

        internal_to_source: dict[str, str] = {}
        source_to_internal: dict[str, str] = {}
        for internal_code in sorted(normalized_codes):
            row = by_internal_code.get(internal_code)
            if row is None:
                # 保留原有不存在合约的订阅失败诊断行为，不能在这里静默丢弃。
                source_code = internal_code
            else:
                instrument, mapping = row
                configured_code = (
                    mapping.market_data_code if mapping is not None else None
                )
                source_code = normalize_code(
                    configured_code
                    or self._default_source_code(
                        order_book_id=internal_code,
                        instrument_type=instrument.instrument_type,
                    )
                )

            existing_internal = source_to_internal.get(source_code)
            if existing_internal is not None and existing_internal != internal_code:
                raise ValueError(
                    "同一行情源代码不能映射到多个内部合约: "
                    f"{source_code}"
                )
            internal_to_source[internal_code] = source_code
            source_to_internal[source_code] = internal_code

        return MarketDataCodeMappingSnapshot(
            internal_to_source=MappingProxyType(internal_to_source),
            source_to_internal=MappingProxyType(source_to_internal),
        )
