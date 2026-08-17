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
    PositionDirection,
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
from app.services.fee_calculator import FeeCalculator, StockFeeComponent
from app.services.order_freeze_service import OrderFreezeService
from app.services.order_validation_service import OrderValidationService
from app.services.stock_order_validation_service import StockOrderValidationService
from app.services.trading_day_service import TradingDayService, get_trading_day_service


def _order_id() -> str:
    return f"SO-{uuid4().hex.upper()}"


def _event_id() -> str:
    return f"SE-{uuid4().hex.upper()}"


class StockOrderService:
    """股票订单受理：仅冻结资源和持久化，绝不创建成交或进入撮合。"""

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
        validation_service: StockOrderValidationService | None = None,
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
        self.validation_service = validation_service or StockOrderValidationService()
        self.trading_day_service = trading_day_service or get_trading_day_service()
        self.trading_day_provider = trading_day_provider
        self.time_provider = time_provider

    @staticmethod
    def _require_stock_account(account) -> None:
        if account.account_type != AccountType.STOCK.value:
            raise BusinessRuleError(
                "股票订单只能使用 STOCK 账户",
                error_code="STOCK_ACCOUNT_REQUIRED",
            )

    @staticmethod
    def _not_found(scope: AccountAccessScope) -> ResourceNotFoundError:
        return ResourceNotFoundError(
            "目标资源不存在" if scope.conceal_resource_existence else "账户不存在",
            error_code="RESOURCE_NOT_FOUND" if scope.conceal_resource_existence else "ACCOUNT_NOT_FOUND",
        )

    @staticmethod
    def _components(items, *, multiplier: Decimal) -> tuple[StockFeeComponent, ...]:
        return tuple(
            StockFeeComponent(
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

    def create_order(
        self,
        *,
        db: Session,
        request: StockOrderCreateRequest,
        access_scope: AccountAccessScope,
    ):
        if not settings.stock_order_entry_enabled:
            raise BusinessRuleError(
                "股票订单受理尚未启用",
                error_code="STOCK_ORDER_ENTRY_DISABLED",
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
                OrderValidationService.validate_idempotent_order_request(
                    existing_order=existing, request=request
                )
                if existing.instrument_type != "STOCK":
                    raise ResourceConflictError(
                        "client_order_id 已被非股票订单使用",
                        error_code="IDEMPOTENCY_KEY_REUSED",
                    )
                db.expunge(existing)
                db.commit()
                return existing

            instrument = self.instrument_repository.get(
                db, normalize_code(request.exchange_id), normalize_code(request.symbol)
            )
            trading_day = (
                self.trading_day_provider()
                if self.trading_day_provider is not None
                else self.trading_day_service.resolve_for_order(
                    db, instrument=instrument, offset_flag="OPEN"
                )
            )
            reference = self.validation_service.resolve_and_validate(
                db, instrument=instrument, request=request, trading_day=trading_day
            )
            fee_items = self.fee_item_repository.resolve_stock_components(
                db,
                instrument_id=reference.instrument.id,
                product_id=reference.instrument.product_id,
                exchange_id=reference.instrument.exchange_id,
                direction=request.direction.value,
                trading_day=trading_day,
            )
            if not fee_items:
                raise BusinessRuleError(
                    "当前交易日缺少股票手续费规则",
                    error_code="STOCK_FEE_COMPONENT_MISSING",
                )
            components = self._components(
                fee_items, multiplier=Decimal(reference.instrument.contract_multiplier)
            )
            estimated_fee = FeeCalculator.calculate_stock_components(
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
                OrderValidationService.validate_idempotent_order_request(
                    existing_order=existing, request=request
                )
                if existing.instrument_type != "STOCK":
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
                OrderFreezeService.freeze_stock_buy(
                    account=account,
                    frozen_cash=frozen_cash,
                    frozen_commission=frozen_commission,
                )
            else:
                OrderFreezeService.validate_account_tradable(account)
                position = self.position_repository.get_for_update(
                    db,
                    account_id=account.account_id,
                    exchange_id=reference.instrument.exchange_id,
                    symbol=reference.instrument.symbol,
                    direction=PositionDirection.LONG.value,
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
                instrument_type="STOCK",
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
            self.outbox_repository.create_event(
                db,
                event_id=_event_id(),
                aggregate_type="ORDER",
                aggregate_id=order.order_id,
                event_type="STOCK_ORDER_ACCEPTED",
                created_at=accepted_at,
                payload={
                    "event_type": "STOCK_ORDER_ACCEPTED",
                    "account_id": account.account_id,
                    "order_id": order.order_id,
                    "instrument_type": "STOCK",
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
            account.updated_at = accepted_at
            db.commit()
            return order
        except Exception:
            db.rollback()
            raise


_stock_order_service = StockOrderService()


def get_stock_order_service() -> StockOrderService:
    return _stock_order_service
