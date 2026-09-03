from datetime import datetime
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.common.code_utils import normalize_code
from app.common.decimal_utils import quantize_money
from app.common.exceptions import (
    BusinessRuleError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from app.common.time_utils import utc_now
from app.core.config import settings
from app.enums.account_enums import AccountType
from app.enums.order_enums import (
    OrderDirection,
    OrderStatus,
    OrderSubmitStatus,
)
from app.models.order_fee_component_snapshot import OrderFeeComponentSnapshot
from app.repositories.account_repository import AccountRepository
from app.repositories.fee_rule_item_repository import FeeRuleItemRepository
from app.repositories.instrument_repository import InstrumentRepository
from app.repositories.order_fee_component_snapshot_repository import (
    OrderFeeComponentSnapshotRepository,
)
from app.repositories.order_repository import OrderRepository
from app.repositories.outbox_repository import OutboxRepository
from app.repositories.position_repository import PositionRepository
from app.schemas.order_schema import StockOrderCreateRequest
from app.services.account_access_scope import AccountAccessScope
from app.services.cash_security_fee_service import (
    CashSecurityFeeComponent,
    CashSecurityFeeService,
)
from app.services.cash_security_funds_service import CashSecurityFundsService
from app.services.order_idempotency_service import OrderIdempotencyService
from app.services.realtime_fact_event_service import RealtimeFactEventService
from app.services.stock_order_validation_service import (
    CASH_SECURITY_POSITION_DIRECTION,
    StockTradingPolicy,
)
from app.services.trading_day_service import TradingDayService, get_trading_day_service


def _order_id() -> str:
    return f"SO-{uuid4().hex.upper()}"


def _event_id() -> str:
    return f"SE-{uuid4().hex.upper()}"


class CashSecurityOrderService:
    """现金证券订单受理：仅冻结资源和持久化，不直接创建成交。"""

    def __init__(
        self,
        *,
        account_repository: AccountRepository | None = None,
        instrument_repository: InstrumentRepository | None = None,
        order_repository: OrderRepository | None = None,
        position_repository: PositionRepository | None = None,
        fee_item_repository: FeeRuleItemRepository | None = None,
        snapshot_repository: OrderFeeComponentSnapshotRepository | None = None,
        outbox_repository: OutboxRepository | None = None,
        validation_service: StockTradingPolicy | None = None,
        instrument_type: str = "STOCK",
        accepted_event_type: str = "STOCK_ORDER_ACCEPTED",
        entry_enabled_setting: str = "stock_order_entry_enabled",
        trading_day_service: TradingDayService | None = None,
        trading_day_provider: Callable[[], object] | None = None,
        time_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self.account_repository = account_repository or AccountRepository()
        self.instrument_repository = instrument_repository or InstrumentRepository()
        self.order_repository = order_repository or OrderRepository()
        self.position_repository = position_repository or PositionRepository()
        self.fee_item_repository = fee_item_repository or FeeRuleItemRepository()
        self.snapshot_repository = snapshot_repository or OrderFeeComponentSnapshotRepository()
        self.outbox_repository = outbox_repository or OutboxRepository()
        self.validation_service = validation_service or StockTradingPolicy()
        self.instrument_type = instrument_type
        self.accepted_event_type = accepted_event_type
        self.entry_enabled_setting = entry_enabled_setting
        self.trading_day_service = trading_day_service or get_trading_day_service()
        self.trading_day_provider = trading_day_provider
        self.time_provider = time_provider

    def _require_stock_account(self, account) -> None:
        if account.account_type not in {AccountType.STOCK.value, "SECURITIES_CASH"}:
            raise BusinessRuleError(
                f"{self.instrument_type}订单只能使用证券现金账户",
                error_code=f"{self.instrument_type}_ACCOUNT_REQUIRED",
            )

    @staticmethod
    def _not_found(scope: AccountAccessScope) -> ResourceNotFoundError:
        return ResourceNotFoundError(
            "目标资源不存在" if scope.conceal_resource_existence else "账户不存在",
            error_code="RESOURCE_NOT_FOUND" if scope.conceal_resource_existence else "ACCOUNT_NOT_FOUND",
        )

    @staticmethod
    def _components(items, *, multiplier: Decimal) -> tuple[CashSecurityFeeComponent, ...]:
        return tuple(
            CashSecurityFeeComponent(
                fee_type=item.fee_type,
                rule_item_id=item.id,
                rule_version=item.rule_version,
                direction=item.direction,
                calculation_type=item.commission_type,
                commission_parameter=Decimal(item.commission_parameter),
                minimum_fee=Decimal(item.minimum_fee),
                aggregation_scope=item.aggregation_scope,
                contract_multiplier=multiplier,
                data_source=item.data_source,
            )
            for item in items
        )

    @staticmethod
    def _snapshot_rows(order_id: str, components, created_at: datetime):
        return [
            OrderFeeComponentSnapshot(
                order_id=order_id,
                fee_type=item.fee_type,
                rule_item_id=item.rule_item_id,
                rule_version=item.rule_version,
                direction=item.direction,
                calculation_type=item.calculation_type,
                commission_parameter=item.commission_parameter,
                minimum_fee=item.minimum_fee,
                aggregation_scope=item.aggregation_scope,
                contract_multiplier=item.contract_multiplier,
                data_source=item.data_source,
                created_at=created_at,
            )
            for item in components
        ]

    def _resolve_instrument(self, db: Session, request: StockOrderCreateRequest):
        """统一将网页输入和行情标准代码解析为参考数据中的合约。

        现金证券历史接口使用 ``exchange_id + symbol``，而行情、订阅和
        持仓展示使用 ``order_book_id``（例如 ``110075.XSHG``）。这里优先
        识别标准行情代码，并兼容前端仍提交 XSHG/XSHE 交易所别名的旧格式。
        """

        exchange_id = normalize_code(request.exchange_id)
        symbol = normalize_code(request.symbol)

        # 用户直接粘贴行情代码时，避免把 ``110075.XSHG`` 当成数据库 symbol。
        if "." in symbol:
            return self.instrument_repository.get_by_order_book_id(db, symbol)

        exchange_aliases = {
            "XSHG": "SSE",
            "XSHE": "SZSE",
        }
        return self.instrument_repository.get(
            db, exchange_aliases.get(exchange_id, exchange_id), symbol
        )

    def _raise_instrument_not_found(self) -> None:
        """在访问合约属性前返回产品对应的可预期业务错误。"""

        if self.instrument_type == "CONVERTIBLE_BOND":
            raise BusinessRuleError(
                "可转债合约不存在",
                error_code="CONVERTIBLE_BOND_INSTRUMENT_NOT_FOUND",
            )
        if self.instrument_type == "ETF":
            raise BusinessRuleError(
                "ETF合约不存在", error_code="ETF_INSTRUMENT_NOT_FOUND"
            )
        raise BusinessRuleError("股票合约不存在", error_code="STOCK_INSTRUMENT_NOT_FOUND")

    def create_order(
        self,
        *,
        db: Session,
        request: StockOrderCreateRequest,
        access_scope: AccountAccessScope,
    ):
        if not getattr(settings, self.entry_enabled_setting):
            raise BusinessRuleError(
                f"{self.instrument_type}订单受理尚未启用",
                error_code=f"{self.instrument_type}_ORDER_ENTRY_DISABLED",
            )
        try:
            authorized = (
                self.account_repository.get_by_account_id(db, request.account_id)
                if access_scope.is_admin
                else self.account_repository.get_owned_account(
                    db, account_id=request.account_id, user_id=access_scope.user_id
                )
            )
            if authorized is None:
                raise self._not_found(access_scope)
            self._require_stock_account(authorized)
            existing = (
                self.order_repository.get_by_client_order_id(
                    db, request.account_id, request.client_order_id
                )
                if access_scope.is_admin
                else self.order_repository.get_by_client_order_id_for_user(
                    db,
                    account_id=request.account_id,
                    client_order_id=request.client_order_id,
                    user_id=access_scope.user_id,
                )
            )
            if existing is not None:
                OrderIdempotencyService.validate(
                    existing_order=existing, request=request
                )
                if existing.instrument_type != self.instrument_type:
                    raise ResourceConflictError(
                        "client_order_id 已被非股票订单使用",
                        error_code="IDEMPOTENCY_KEY_REUSED",
                    )
                db.expunge(existing)
                db.commit()
                return existing

            instrument = self._resolve_instrument(db, request)
            if instrument is None:
                self._raise_instrument_not_found()
            trading_day = (
                self.trading_day_provider()
                if self.trading_day_provider is not None
                else self.trading_day_service.resolve_for_cash_security_order(
                    db, instrument=instrument
                )
            )
            reference = self.validation_service.resolve_and_validate(
                db, instrument=instrument, request=request, trading_day=trading_day
            )
            fee_items = self.fee_item_repository.resolve_cash_security_components(
                db,
                instrument_id=reference.instrument.id,
                product_id=reference.instrument.product_id,
                exchange_id=reference.instrument.exchange_id,
                instrument_type=self.instrument_type,
                direction=request.direction.value,
                trading_day=trading_day,
            )
            if not fee_items:
                if self.instrument_type == "CONVERTIBLE_BOND":
                    raise BusinessRuleError(
                        "当前交易日缺少可转债手续费规则",
                        error_code="CONVERTIBLE_BOND_FEE_COMPONENT_MISSING",
                    )
                if self.instrument_type == "ETF":
                    raise BusinessRuleError(
                        "当前交易日缺少ETF手续费规则",
                        error_code="ETF_FEE_COMPONENT_MISSING",
                    )
                raise BusinessRuleError(
                    "当前交易日缺少股票手续费规则",
                    error_code="STOCK_FEE_COMPONENT_MISSING",
                )
            components = self._components(
                fee_items, multiplier=Decimal(reference.instrument.contract_multiplier)
            )
            estimated_fee = CashSecurityFeeService.calculate_components(
                price=request.limit_price, volume=request.volume, components=components
            )

            account = (
                self.account_repository.get_by_account_id_for_update(db, request.account_id)
                if access_scope.is_admin
                else self.account_repository.get_owned_account_for_update(
                    db, account_id=request.account_id, user_id=access_scope.user_id
                )
            )
            if account is None:
                raise self._not_found(access_scope)
            self._require_stock_account(account)
            existing = self.order_repository.get_by_client_order_id(
                db, request.account_id, request.client_order_id
            )
            if existing is not None:
                OrderIdempotencyService.validate(
                    existing_order=existing, request=request
                )
                if existing.instrument_type != self.instrument_type:
                    raise ResourceConflictError(
                        "client_order_id 已被非股票订单使用",
                        error_code="IDEMPOTENCY_KEY_REUSED",
                    )
                db.expunge(existing)
                db.commit()
                return existing

            frozen_cash = Decimal("0")
            frozen_commission = Decimal("0")
            frozen_position_volume = 0
            if request.direction == OrderDirection.BUY:
                self.validation_service.validate_buy(request=request, rule=reference.rule)
                frozen_cash = quantize_money(
                    request.limit_price
                    * Decimal(request.volume)
                    * Decimal(reference.instrument.contract_multiplier)
                )
                frozen_commission = estimated_fee
                CashSecurityFundsService.freeze_buy(
                    account=account,
                    frozen_cash=frozen_cash,
                    frozen_commission=frozen_commission,
                )
            else:
                CashSecurityFundsService.validate_account_tradable(account)
                position = self.position_repository.get_for_update(
                    db,
                    account_id=account.account_id,
                    exchange_id=reference.instrument.exchange_id,
                    symbol=reference.instrument.symbol,
                    direction=CASH_SECURITY_POSITION_DIRECTION,
                )
                self.validation_service.validate_sell(
                    request=request, rule=reference.rule, position=position
                )
                position.frozen_volume += request.volume
                position.available_volume = (
                    position.total_volume
                    - position.frozen_volume
                    - position.settlement_locked_volume
                )
                position.updated_at = self.time_provider()
                frozen_position_volume = request.volume

            accepted_at = self.time_provider()
            order = self.order_repository.create(
                db,
                order_id=_order_id(),
                client_order_id=request.client_order_id,
                account_id=account.account_id,
                order_book_id=reference.instrument.order_book_id,
                symbol=reference.instrument.symbol,
                exchange_id=reference.instrument.exchange_id,
                trading_day=trading_day,
                instrument_type=self.instrument_type,
                direction=request.direction.value,
                offset_flag=None,
                order_type=request.order_type.value,
                commission_type=None,
                commission_parameter=None,
                commission_contract_multiplier=None,
                limit_price=request.limit_price,
                submitted_limit_price=request.limit_price,
                resolved_price=request.limit_price,
                total_volume=request.volume,
                status=OrderStatus.ACCEPTED.value,
                submit_status=OrderSubmitStatus.ACCEPTED.value,
                frozen_margin=Decimal("0"),
                frozen_cash=frozen_cash,
                frozen_commission=frozen_commission,
                frozen_position_volume=frozen_position_volume,
                created_at=accepted_at,
                accepted_at=accepted_at,
            )
            self.snapshot_repository.add_all(
                db, self._snapshot_rows(order.order_id, components, accepted_at)
            )
            event_id = _event_id()
            outbox_event = self.outbox_repository.create_event(
                db,
                event_id=event_id,
                aggregate_type="ORDER",
                aggregate_id=order.order_id,
                event_type=self.accepted_event_type,
                created_at=accepted_at,
                payload={
                    "event_type": self.accepted_event_type,
                    "event_id": event_id,
                    "account_id": account.account_id,
                    "account_type": "SECURITIES_CASH",
                    "order_id": order.order_id,
                    "instrument_type": self.instrument_type,
                    "order_book_id": order.order_book_id,
                    "exchange_id": order.exchange_id,
                    "symbol": order.symbol,
                    "status": order.status,
                    "direction": order.direction,
                    "offset_flag": None,
                    "trading_day": trading_day.isoformat(),
                    "created_at": accepted_at.isoformat(),
                },
            )
            # Outbox 自增主键是同一订单事件的稳定业务版本；在提交前写回
            # payload，使重放和实时投影都能读到相同版本。
            db.flush()
            outbox_event.payload = {
                **outbox_event.payload,
                "business_version": str(outbox_event.id),
            }
            account.updated_at = accepted_at
            realtime_events = RealtimeFactEventService(
                repository=self.outbox_repository
            )
            if request.direction == OrderDirection.BUY:
                realtime_events.create_account_updated(
                    db,
                    account=account,
                    occurred_at=accepted_at,
                    account_type="SECURITIES_CASH",
                    fact_reason="CASH_SECURITY_BUY_FROZEN",
                )
            else:
                realtime_events.create_position_updated(
                    db,
                    position=position,
                    occurred_at=accepted_at,
                    fact_reason="CASH_SECURITY_SELL_FROZEN",
                )
            db.commit()
            return order
        except Exception:
            db.rollback()
            raise


_stock_order_service = CashSecurityOrderService()


def get_stock_order_service() -> CashSecurityOrderService:
    return _stock_order_service


# 保持阶段一、二已经发布的 Python 入口兼容。
StockOrderService = CashSecurityOrderService
