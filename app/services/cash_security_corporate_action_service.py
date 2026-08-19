"""PostgreSQL-authoritative execution for cash-security corporate actions."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.decimal_utils import quantize_money
from app.common.exceptions import BusinessRuleError, ResourceConflictError, ResourceNotFoundError
from app.common.time_utils import utc_now
from app.enums.corporate_action_enums import (
    CorporateActionComponentType, CorporateActionEntitlementStatus,
    CorporateActionStatus,
)
from app.enums.instrument_enums import InstrumentType
from app.models.account import Account
from app.models.cash_security_corporate_action import CashSecurityCorporateAction
from app.models.cash_security_corporate_action_component import CashSecurityCorporateActionComponent
from app.models.cash_security_corporate_action_entitlement import CashSecurityCorporateActionEntitlement
from app.models.cash_security_corporate_action_ledger import CashSecurityCorporateActionLedger
from app.models.cash_security_corporate_action_subscription import (
    CashSecurityCorporateActionSubscription,
)
from app.models.cash_security_corporate_action_position_adjustment import (
    CashSecurityCorporateActionPositionAdjustment,
)
from app.models.instrument import Instrument
from app.models.position import Position
from app.models.trade import Trade
from app.models.cash_security_price_adjustment_factor import CashSecurityPriceAdjustmentFactor
from app.repositories.outbox_repository import OutboxRepository
from app.services.account_access_scope import AccountAccessScope
from app.services.realtime_fact_event_service import RealtimeFactEventService


ZERO = Decimal("0")
CASH_TYPES = {InstrumentType.STOCK.value, InstrumentType.CONVERTIBLE_BOND.value}
UNEXECUTED_ACTION_STATUSES = {
    CorporateActionStatus.DRAFT.value,
    CorporateActionStatus.CONFIRMED.value,
}
STOCK_COMPONENTS = {
    CorporateActionComponentType.CASH_DIVIDEND.value,
    CorporateActionComponentType.STOCK_DIVIDEND.value,
    CorporateActionComponentType.CAPITALIZATION_ISSUE.value,
    CorporateActionComponentType.RIGHTS_ISSUE.value,
    CorporateActionComponentType.STOCK_SPLIT.value,
    CorporateActionComponentType.REVERSE_SPLIT.value,
}
BOND_COMPONENTS = {
    CorporateActionComponentType.BOND_COUPON.value,
    CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value,
}


@dataclass(frozen=True)
class CorporateActionResult:
    action_id: str
    status: str
    created_entitlements: int = 0


class CashSecurityCorporateActionService:
    """Fixed lock order: action → components → entitlements → account → position."""

    def __init__(self, *, outbox_repository: OutboxRepository | None = None) -> None:
        self.outbox = outbox_repository or OutboxRepository()

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex.upper()}"

    def _event(self, db: Session, *, event_type: str, action_id: str, payload: dict) -> None:
        event_id = self._id("CAE")
        self.outbox.create_event(
            db=db, event_id=event_id, aggregate_type="CORPORATE_ACTION",
            aggregate_id=action_id, event_type=event_type, created_at=utc_now(),
            payload={"event_id": event_id, "event_type": event_type, "action_id": action_id, **payload},
        )

    def _publish_changed_facts(self, db: Session, *, action_id: str) -> None:
        """Emit authoritative account/position facts for the normal dirty and
        WebSocket chain; corporate-action notifications alone carry no account
        route and therefore cannot refresh valuation snapshots."""
        entitlements = db.scalars(
            select(CashSecurityCorporateActionEntitlement).where(
                CashSecurityCorporateActionEntitlement.action_id == action_id
            )
        ).all()
        account_ids = sorted({row.account_id for row in entitlements})
        position_ids = sorted({row.position_id for row in entitlements})
        if not account_ids:
            return
        events = RealtimeFactEventService(repository=self.outbox)
        now = utc_now()
        accounts = db.scalars(select(Account).where(Account.account_id.in_(account_ids))).all()
        positions = db.scalars(select(Position).where(Position.position_id.in_(position_ids))).all()
        for position in positions:
            position.updated_at = now
            events.create_position_updated(db, position=position, occurred_at=now, fact_reason="CORPORATE_ACTION")
        for account in accounts:
            account.updated_at = now
            events.create_account_updated(db, account=account, occurred_at=now, fact_reason="CORPORATE_ACTION", include_valuation_fields=True)

    @staticmethod
    def _units(quantity: int, base: Decimal, ratio: Decimal) -> tuple[int, Decimal]:
        raw = Decimal(quantity) / base * ratio
        whole = int(raw.to_integral_value(rounding=ROUND_FLOOR))
        return whole, raw - Decimal(whole)

    def import_action(self, db: Session, *, payload: dict, components: list[dict]) -> CashSecurityCorporateAction:
        source_hash = sha256(
            json.dumps(
                {"payload": payload, "components": components},
                default=str,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        source_id = payload["source_action_id"].strip()
        version = int(payload.get("action_version", 1))
        existing = db.scalar(select(CashSecurityCorporateAction).where(
            CashSecurityCorporateAction.source_action_id == source_id,
            CashSecurityCorporateAction.action_version == version,
        ).with_for_update())
        if existing is not None:
            if existing.source_payload_hash != source_hash:
                raise ResourceConflictError("同一来源版本内容不一致", error_code="CORPORATE_ACTION_SOURCE_CONFLICT")
            return existing
        newer_revision = db.scalar(
            select(CashSecurityCorporateAction)
            .where(
                CashSecurityCorporateAction.source_action_id == source_id,
                CashSecurityCorporateAction.action_version > version,
            )
            .order_by(CashSecurityCorporateAction.action_version.desc())
            .with_for_update()
        )
        instrument = db.scalar(select(Instrument).where(Instrument.id == payload["instrument_id"]))
        if instrument is None or instrument.instrument_type not in CASH_TYPES:
            raise BusinessRuleError("公司行为仅支持股票和可转债", error_code="CORPORATE_ACTION_INSTRUMENT_INVALID")
        record_date, ex_date = payload.get("record_date"), payload.get("ex_date")
        if record_date and ex_date and ex_date < record_date:
            raise BusinessRuleError("除权日不能早于登记日", error_code="CORPORATE_ACTION_DATE_INVALID")
        action = CashSecurityCorporateAction(
            action_id=self._id("CA"), instrument_id=instrument.id, exchange_id=instrument.exchange_id,
            order_book_id=instrument.order_book_id, action_version=version, status=CorporateActionStatus.DRAFT.value,
            announcement_date=payload.get("announcement_date"), record_date=record_date, ex_date=ex_date,
            payment_date=payload.get("payment_date"), listing_date=payload.get("listing_date"),
            subscription_start_date=payload.get("subscription_start_date"), subscription_end_date=payload.get("subscription_end_date"),
            source_action_id=source_id, data_source=payload["data_source"], source_payload_hash=source_hash,
            source_payload=json.loads(json.dumps(payload, default=str)), created_at=utc_now(), updated_at=utc_now(),
        )
        db.add(action)
        db.flush()
        for item in components:
            kind = item["component_type"]
            if kind not in STOCK_COMPONENTS | BOND_COMPONENTS or Decimal(item["base_quantity"]) <= ZERO:
                raise BusinessRuleError("公司行为组成部分无效", error_code="CORPORATE_ACTION_COMPONENT_INVALID")
            if instrument.instrument_type == InstrumentType.STOCK.value and kind not in STOCK_COMPONENTS:
                raise BusinessRuleError("股票不能使用可转债公司行为", error_code="CORPORATE_ACTION_COMPONENT_PRODUCT_MISMATCH")
            if instrument.instrument_type == InstrumentType.CONVERTIBLE_BOND.value and kind not in BOND_COMPONENTS:
                raise BusinessRuleError("可转债不能使用股票公司行为", error_code="CORPORATE_ACTION_COMPONENT_PRODUCT_MISMATCH")
            if any(Decimal(item.get(name, ZERO)) < ZERO for name in ("cash_amount", "share_ratio", "rights_ratio", "subscription_price", "withholding_tax_rate", "cash_in_lieu_price")):
                raise BusinessRuleError("公司行为金额或比例不能为负", error_code="CORPORATE_ACTION_COMPONENT_NEGATIVE")
            db.add(CashSecurityCorporateActionComponent(
                component_id=self._id("CAC"), action_id=action.action_id, component_type=kind,
                base_quantity=Decimal(item["base_quantity"]), cash_amount=Decimal(item.get("cash_amount", ZERO)),
                share_ratio=Decimal(item.get("share_ratio", ZERO)), rights_ratio=Decimal(item.get("rights_ratio", ZERO)),
                subscription_price=Decimal(item.get("subscription_price", ZERO)), withholding_tax_rate=Decimal(item.get("withholding_tax_rate", ZERO)),
                cash_in_lieu_price=Decimal(item.get("cash_in_lieu_price", ZERO)), rounding_rule=item.get("rounding_rule", "FLOOR"), currency=item.get("currency", "CNY"), created_at=utc_now(),
            ))
        # Revisions may replace only an event that has not yet frozen a holder
        # snapshot.  Once an entitlement exists, the original fact remains
        # auditable and a correction needs an explicit manual decision.
        prior_revisions = db.scalars(
            select(CashSecurityCorporateAction)
            .where(
                CashSecurityCorporateAction.source_action_id == source_id,
                CashSecurityCorporateAction.action_version < version,
                CashSecurityCorporateAction.status.not_in((
                    CorporateActionStatus.CANCELLED.value,
                    CorporateActionStatus.SUPERSEDED.value,
                )),
            )
            .with_for_update()
        ).all()
        has_executed_predecessor = any(
            row.status not in UNEXECUTED_ACTION_STATUSES
            for row in prior_revisions
        )
        if newer_revision is not None:
            action.status = CorporateActionStatus.SUPERSEDED.value
            action.superseded_by_action_id = newer_revision.action_id
        elif has_executed_predecessor:
            action.status = CorporateActionStatus.MANUAL_REVIEW_REQUIRED.value
        else:
            for prior in prior_revisions:
                prior.status = CorporateActionStatus.SUPERSEDED.value
                prior.superseded_by_action_id = action.action_id
                prior.updated_at = utc_now()
                self._event(
                    db,
                    event_type="CORPORATE_ACTION_SUPERSEDED",
                    action_id=prior.action_id,
                    payload={
                        "superseded_by_action_id": action.action_id,
                        "business_version": str(prior.action_version),
                    },
                )
        self._event(db, event_type="CORPORATE_ACTION_IMPORTED", action_id=action.action_id, payload={"business_version": str(version)})
        if action.status == CorporateActionStatus.MANUAL_REVIEW_REQUIRED.value:
            self._event(
                db,
                event_type="CORPORATE_ACTION_MANUAL_REVIEW_REQUIRED",
                action_id=action.action_id,
                payload={"reason": "EXECUTED_SOURCE_REVISION_EXISTS", "business_version": str(version)},
            )
        return action

    def confirm(self, db: Session, *, action_id: str) -> CorporateActionResult:
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is not None and action.status == CorporateActionStatus.SUPERSEDED.value:
            raise BusinessRuleError(
                "Corporate-action revision has been superseded",
                error_code="CORPORATE_ACTION_REVISION_SUPERSEDED",
            )
        if action is not None and action.status == CorporateActionStatus.MANUAL_REVIEW_REQUIRED.value:
            raise BusinessRuleError(
                "Corporate-action revision requires manual review",
                error_code="CORPORATE_ACTION_MANUAL_REVIEW_REQUIRED",
            )
        if action is None:
            raise ResourceNotFoundError("公司行为不存在")
        if action.status == CorporateActionStatus.DRAFT.value:
            newer_revision = db.scalar(
                select(CashSecurityCorporateAction.action_id).where(
                    CashSecurityCorporateAction.source_action_id == action.source_action_id,
                    CashSecurityCorporateAction.action_version > action.action_version,
                    CashSecurityCorporateAction.status.not_in((
                        CorporateActionStatus.CANCELLED.value,
                        CorporateActionStatus.SUPERSEDED.value,
                    )),
                )
            )
            if newer_revision is not None:
                raise ResourceConflictError(
                    "A newer corporate-action revision exists",
                    error_code="CORPORATE_ACTION_REVISION_SUPERSEDED",
                )
            action.status, action.confirmed_at, action.updated_at = CorporateActionStatus.CONFIRMED.value, utc_now(), utc_now()
            self._event(db, event_type="CORPORATE_ACTION_CONFIRMED", action_id=action_id, payload={"business_version": str(action.action_version)})
        return CorporateActionResult(action_id, action.status)

    def capture_entitlements(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.status not in {CorporateActionStatus.CONFIRMED.value, CorporateActionStatus.ENTITLEMENT_CAPTURED.value}:
            raise BusinessRuleError("公司行为尚未确认", error_code="CORPORATE_ACTION_NOT_CONFIRMED")
        if action.record_date != trading_day:
            raise BusinessRuleError("权益只能在登记日日结屏障内生成", error_code="CORPORATE_ACTION_RECORD_DAY_INVALID")
        instrument = db.get(Instrument, action.instrument_id)
        components = db.scalars(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.action_id == action_id).order_by(CashSecurityCorporateActionComponent.id).with_for_update()).all()
        positions = db.scalars(select(Position).where(Position.order_book_id == action.order_book_id, Position.exchange_id == action.exchange_id, Position.instrument_type == instrument.instrument_type, Position.total_volume > 0).order_by(Position.id).with_for_update()).all()
        created = 0
        for position in positions:
            for component in components:
                existing = db.scalar(select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id, CashSecurityCorporateActionEntitlement.component_id == component.component_id, CashSecurityCorporateActionEntitlement.account_id == position.account_id, CashSecurityCorporateActionEntitlement.position_id == position.position_id))
                if existing is not None:
                    continue
                gross = quantize_money(Decimal(position.total_volume) / Decimal(component.base_quantity) * Decimal(component.cash_amount))
                tax = quantize_money(gross * Decimal(component.withholding_tax_rate))
                shares, fraction = self._units(position.total_volume, Decimal(component.base_quantity), Decimal(component.share_ratio or component.rights_ratio))
                db.add(CashSecurityCorporateActionEntitlement(
                    entitlement_id=self._id("CAE"), action_id=action_id, component_id=component.component_id, account_id=position.account_id, position_id=position.position_id,
                    record_quantity=position.total_volume, entitled_cash_gross=gross, withholding_tax=tax, entitled_cash_net=quantize_money(gross-tax), entitled_share_volume=shares, fractional_share=fraction,
                    cash_in_lieu=quantize_money(fraction * Decimal(component.cash_in_lieu_price)), status=CorporateActionEntitlementStatus.CONFIRMED.value,
                    record_position_version=position.updated_at.isoformat(), created_at=utc_now(), updated_at=utc_now(),
                ))
                created += 1
        action.status, action.updated_at = CorporateActionStatus.ENTITLEMENT_CAPTURED.value, utc_now()
        self._event(db, event_type="CORPORATE_ACTION_ENTITLEMENT_CREATED", action_id=action_id, payload={"count": created, "business_version": str(action.action_version)})
        return CorporateActionResult(action_id, action.status, created)

    def _ledger(self, db: Session, *, entitlement, component, entry_type: str, action_version: int, effective_trading_day: date, cash: Decimal = ZERO, receivable: Decimal = ZERO, pending: int = 0, volume: int = 0, cost: Decimal = ZERO, income: Decimal = ZERO, idempotency_token: str | None = None) -> bool:
        key = f"{entitlement.entitlement_id}:{entry_type}:{idempotency_token}" if idempotency_token else f"{entitlement.entitlement_id}:{entry_type}"
        if db.scalar(select(CashSecurityCorporateActionLedger.id).where(CashSecurityCorporateActionLedger.idempotency_key == key)) is not None:
            return False
        db.add(CashSecurityCorporateActionLedger(
            ledger_id=self._id("CAL"), action_id=entitlement.action_id, component_id=component.component_id,
            entitlement_id=entitlement.entitlement_id, account_id=entitlement.account_id, position_id=entitlement.position_id,
            entry_type=entry_type, cash_delta=cash, receivable_delta=receivable, position_volume_delta=volume,
            pending_volume_delta=pending, position_cost_delta=cost, corporate_action_income_delta=income,
            business_version=str(action_version), idempotency_key=key,
            effective_trading_day=effective_trading_day, created_at=utc_now(),
        ))
        return True

    def _position_adjustment(
        self,
        db: Session,
        *,
        action: CashSecurityCorporateAction,
        entitlement: CashSecurityCorporateActionEntitlement,
        component: CashSecurityCorporateActionComponent,
        adjustment_type: str,
        effective_trading_day: date,
        total_volume: int = 0,
        today_volume: int = 0,
        yesterday_volume: int = 0,
        pending_volume: int = 0,
        available_volume: int = 0,
        frozen_volume: int = 0,
        settlement_locked_volume: int = 0,
        position_cost: Decimal = ZERO,
        daily_pnl_base_cost: Decimal = ZERO,
        average_open_price_after: Decimal | None = None,
        position_detail_id: str | None = None,
        replay_payload: dict | None = None,
        idempotency_token: str | None = None,
    ) -> bool:
        """Append a replayable business fact before changing Position."""
        key = (
            f"{entitlement.entitlement_id}:{adjustment_type}:{idempotency_token}"
            if idempotency_token
            else f"{entitlement.entitlement_id}:{adjustment_type}"
        )
        if db.scalar(
            select(CashSecurityCorporateActionPositionAdjustment.id).where(
                CashSecurityCorporateActionPositionAdjustment.idempotency_key == key
            )
        ) is not None:
            return False
        db.add(
            CashSecurityCorporateActionPositionAdjustment(
                adjustment_id=self._id("CAPA"),
                action_id=action.action_id,
                action_version=action.action_version,
                component_id=component.component_id,
                entitlement_id=entitlement.entitlement_id,
                account_id=entitlement.account_id,
                position_id=entitlement.position_id,
                position_detail_id=position_detail_id,
                adjustment_type=adjustment_type,
                effective_trading_day=effective_trading_day,
                business_version=str(action.action_version),
                idempotency_key=key,
                total_volume_delta=total_volume,
                today_volume_delta=today_volume,
                yesterday_volume_delta=yesterday_volume,
                pending_volume_delta=pending_volume,
                available_volume_delta=available_volume,
                frozen_volume_delta=frozen_volume,
                settlement_locked_volume_delta=settlement_locked_volume,
                position_cost_delta=quantize_money(position_cost),
                daily_pnl_base_cost_delta=quantize_money(daily_pnl_base_cost),
                average_open_price_after=average_open_price_after,
                replay_payload=replay_payload or {},
                created_at=utc_now(),
            )
        )
        return True

    def _ensure_replay_opening_balance(
        self,
        db: Session,
        *,
        action: CashSecurityCorporateAction,
        entitlement: CashSecurityCorporateActionEntitlement,
        component: CashSecurityCorporateActionComponent,
        position: Position,
        effective_trading_day: date,
    ) -> None:
        """Capture a legacy no-Trade holding before its first action mutates it.

        New cash positions are reproducible from Trades and never get this
        baseline.  The baseline is solely an explicit bridge for imported
        opening positions; it prevents an action from making their original
        quantity depend on a later mutable Position row.
        """
        existing = db.scalar(
            select(CashSecurityCorporateActionPositionAdjustment.id).where(
                CashSecurityCorporateActionPositionAdjustment.position_id
                == position.position_id
            )
        )
        has_trade = db.scalar(
            select(Trade.id)
            .where(
                Trade.account_id == position.account_id,
                Trade.exchange_id == position.exchange_id,
                Trade.symbol == position.symbol,
                Trade.instrument_type == position.instrument_type,
            )
            .limit(1)
        )
        if existing is not None or has_trade is not None:
            return
        key = f"{position.position_id}:REPLAY_OPENING_BALANCE"
        db.add(
            CashSecurityCorporateActionPositionAdjustment(
                adjustment_id=self._id("CAPA"),
                action_id=action.action_id,
                action_version=action.action_version,
                component_id=component.component_id,
                entitlement_id=entitlement.entitlement_id,
                account_id=position.account_id,
                position_id=position.position_id,
                position_detail_id=None,
                adjustment_type="REPLAY_OPENING_BALANCE",
                effective_trading_day=effective_trading_day,
                business_version=str(action.action_version),
                idempotency_key=key,
                total_volume_delta=position.total_volume,
                today_volume_delta=position.today_volume,
                yesterday_volume_delta=position.yesterday_volume,
                pending_volume_delta=position.pending_share_volume,
                available_volume_delta=position.available_volume,
                frozen_volume_delta=position.frozen_volume,
                settlement_locked_volume_delta=position.settlement_locked_volume,
                position_cost_delta=quantize_money(position.position_cost),
                daily_pnl_base_cost_delta=quantize_money(position.daily_pnl_base_cost),
                average_open_price_after=position.average_open_price,
                replay_payload={
                    "yesterday_pnl_base_cost": format(position.yesterday_pnl_base_cost, "f"),
                    "today_pnl_base_cost": format(position.today_pnl_base_cost, "f"),
                    "daily_pnl_base_established": bool(position.daily_pnl_base_established),
                },
                created_at=utc_now(),
            )
        )

    def _maybe_mark_completed(
        self, db: Session, *, action: CashSecurityCorporateAction, trading_day: date
    ) -> bool:
        """Finish only when every entitled component has reached its terminal fact."""
        if action.status not in {
            CorporateActionStatus.ENTITLEMENT_CAPTURED.value,
            CorporateActionStatus.PROCESSING.value,
        }:
            return False
        components = db.scalars(select(CashSecurityCorporateActionComponent).where(
            CashSecurityCorporateActionComponent.action_id == action.action_id
        )).all()
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).where(
            CashSecurityCorporateActionEntitlement.action_id == action.action_id
        )).all()
        ledgers = {
            (row.entitlement_id, row.entry_type)
            for row in db.scalars(select(CashSecurityCorporateActionLedger).where(
                CashSecurityCorporateActionLedger.action_id == action.action_id
            )).all()
        }
        by_component: dict[str, list[CashSecurityCorporateActionEntitlement]] = {}
        for item in entitlements:
            by_component.setdefault(item.component_id, []).append(item)
        for component in components:
            rows = by_component.get(component.component_id, [])
            kind = component.component_type
            if kind in {
                CorporateActionComponentType.CASH_DIVIDEND.value,
                CorporateActionComponentType.BOND_COUPON.value,
                CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value,
            }:
                if action.payment_date is None or trading_day < action.payment_date:
                    return False
                if any((row.entitlement_id, "CASH_PAID") not in ledgers for row in rows):
                    return False
            elif kind in {
                CorporateActionComponentType.STOCK_DIVIDEND.value,
                CorporateActionComponentType.CAPITALIZATION_ISSUE.value,
            }:
                if action.listing_date is None or trading_day < action.listing_date:
                    return False
                if any(row.pending_share_volume or row.credited_share_volume < row.entitled_share_volume for row in rows):
                    return False
            elif kind == CorporateActionComponentType.RIGHTS_ISSUE.value:
                if action.subscription_end_date is None or trading_day <= action.subscription_end_date:
                    return False
                if any(row.subscribed_volume and (row.pending_share_volume or row.credited_share_volume < row.subscribed_volume) for row in rows):
                    return False
            elif kind in {
                CorporateActionComponentType.STOCK_SPLIT.value,
                CorporateActionComponentType.REVERSE_SPLIT.value,
            }:
                if action.ex_date is None or trading_day < action.ex_date:
                    return False
                if any(row.cash_in_lieu and (row.entitlement_id, "CASH_PAID") not in ledgers for row in rows):
                    return False
        action.status = CorporateActionStatus.COMPLETED.value
        action.completed_at = utc_now()
        action.updated_at = action.completed_at
        self._event(
            db,
            event_type="CORPORATE_ACTION_COMPLETED",
            action_id=action.action_id,
            payload={"business_version": str(action.action_version)},
        )
        return True

    def apply_ex_date(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.status not in {CorporateActionStatus.ENTITLEMENT_CAPTURED.value, CorporateActionStatus.PROCESSING.value}:
            raise BusinessRuleError("权益尚未冻结", error_code="CORPORATE_ACTION_ENTITLEMENT_REQUIRED")
        if action.ex_date != trading_day:
            raise BusinessRuleError("公司行为尚未到除权除息日", error_code="CORPORATE_ACTION_EX_DATE_INVALID")
        components = {item.component_id: item for item in db.scalars(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.action_id == action_id).with_for_update())}
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id).order_by(CashSecurityCorporateActionEntitlement.id).with_for_update()).all()
        for item in entitlements:
            component = components[item.component_id]
            account = db.scalar(select(Account).where(Account.account_id == item.account_id).with_for_update())
            position = db.scalar(select(Position).where(Position.position_id == item.position_id).with_for_update())
            self._ensure_replay_opening_balance(
                db, action=action, entitlement=item, component=component,
                position=position, effective_trading_day=trading_day,
            )
            kind = component.component_type
            if kind in {CorporateActionComponentType.CASH_DIVIDEND.value, CorporateActionComponentType.BOND_COUPON.value}:
                if self._ledger(db, entitlement=item, component=component, entry_type="RECEIVABLE_CREATED", action_version=action.action_version, effective_trading_day=trading_day, receivable=item.entitled_cash_net, income=item.entitled_cash_net):
                    account.corporate_action_receivable = quantize_money(account.corporate_action_receivable + item.entitled_cash_net)
                    account.corporate_action_income = quantize_money(account.corporate_action_income + item.entitled_cash_net)
                    item.status, item.processed_at = CorporateActionEntitlementStatus.PENDING.value, utc_now()
            elif kind in {CorporateActionComponentType.STOCK_DIVIDEND.value, CorporateActionComponentType.CAPITALIZATION_ISSUE.value, CorporateActionComponentType.RIGHTS_ISSUE.value}:
                if kind != CorporateActionComponentType.RIGHTS_ISSUE.value:
                    ledger_created = self._ledger(
                        db, entitlement=item, component=component,
                        entry_type="SHARES_PENDING", action_version=action.action_version,
                        effective_trading_day=trading_day,
                        pending=item.entitled_share_volume,
                    )
                    adjustment_created = self._position_adjustment(
                        db, action=action, entitlement=item, component=component,
                        adjustment_type="SHARES_PENDING",
                        effective_trading_day=trading_day,
                        pending_volume=item.entitled_share_volume,
                    )
                    if ledger_created and adjustment_created:
                        position.pending_share_volume += item.entitled_share_volume
                        item.pending_share_volume += item.entitled_share_volume
                        pending_value = quantize_money(
                            Decimal(item.entitled_share_volume)
                            * Decimal(position.mark_price or ZERO)
                        )
                        account.pending_security_value = quantize_money(
                            Decimal(account.pending_security_value) + pending_value
                        )
                        item.status, item.processed_at = CorporateActionEntitlementStatus.PENDING.value, utc_now()
            elif kind in {
                CorporateActionComponentType.STOCK_SPLIT.value,
                CorporateActionComponentType.REVERSE_SPLIT.value,
            }:
                multiplier = Decimal(component.share_ratio) / Decimal(component.base_quantity)
                if multiplier <= ZERO:
                    raise BusinessRuleError("拆并股比例无效", error_code="CORPORATE_ACTION_SPLIT_INVALID")
                entry_type = "STOCK_SPLIT" if kind == CorporateActionComponentType.STOCK_SPLIT.value else "REVERSE_SPLIT"
                if self._ledger(db, entitlement=item, component=component, entry_type=entry_type, action_version=action.action_version, effective_trading_day=trading_day):
                    # The historical buckets are monetary bases, not quantities:
                    # a mechanical split must never change them or position_cost.
                    old_total = position.total_volume
                    old_cost = Decimal(position.position_cost)
                    new_today = int(Decimal(position.today_volume) * multiplier)
                    new_yesterday = int(Decimal(position.yesterday_volume) * multiplier)
                    new_total = new_today + new_yesterday
                    fraction = Decimal(old_total) * multiplier - Decimal(new_total)
                    new_frozen = int(Decimal(position.frozen_volume) * multiplier)
                    new_locked = int(
                        Decimal(position.settlement_locked_volume) * multiplier
                    )
                    new_pending = int(
                        Decimal(position.pending_share_volume) * multiplier
                    )
                    new_available = new_total - new_frozen - new_locked
                    if fraction and Decimal(component.cash_in_lieu_price) <= ZERO:
                        raise BusinessRuleError("并股尾股缺少现金补偿规则", error_code="CORPORATE_ACTION_FRACTIONAL_SHARE_UNRESOLVED")
                    if not self._position_adjustment(
                        db, action=action, entitlement=item, component=component,
                        adjustment_type=entry_type,
                        effective_trading_day=trading_day,
                        total_volume=new_total - old_total,
                        today_volume=new_today - position.today_volume,
                        yesterday_volume=new_yesterday - position.yesterday_volume,
                        pending_volume=new_pending - position.pending_share_volume,
                        available_volume=new_available - position.available_volume,
                        frozen_volume=new_frozen - position.frozen_volume,
                        settlement_locked_volume=(
                            new_locked - position.settlement_locked_volume
                        ),
                        average_open_price_after=(
                            quantize_money(old_cost / Decimal(new_total))
                            if new_total else ZERO
                        ),
                        replay_payload={
                            "multiplier": format(multiplier, "f"),
                            "volume_before": old_total,
                            "volume_after": new_total,
                            "fractional_volume": format(fraction, "f"),
                        },
                    ):
                        continue
                    position.today_volume = new_today
                    position.yesterday_volume = new_yesterday
                    position.total_volume = new_total
                    position.frozen_volume = new_frozen
                    position.settlement_locked_volume = new_locked
                    position.pending_share_volume = new_pending
                    position.available_volume = new_available
                    position.average_open_price = quantize_money(old_cost / Decimal(new_total)) if new_total else ZERO
                    if fraction:
                        cash_in_lieu = quantize_money(fraction * Decimal(component.cash_in_lieu_price))
                        # No fractional unit is silently discarded.  The receivable is
                        # an asset conversion rather than dividend income.
                        item.cash_in_lieu = cash_in_lieu
                        account.corporate_action_receivable = quantize_money(account.corporate_action_receivable + cash_in_lieu)
                        self._ledger(db, entitlement=item, component=component, entry_type=f"{entry_type}_CASH_IN_LIEU", action_version=action.action_version, effective_trading_day=trading_day, receivable=cash_in_lieu)
                    position.average_open_price = quantize_money(position.position_cost / Decimal(position.total_volume)) if position.total_volume else ZERO
            elif kind == CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value:
                # Principal redemption is handled after every component has
                # been snapshotted by the dedicated maturity operation.
                continue
            else:
                raise BusinessRuleError("该组成部分需在专用到期流程执行", error_code="CORPORATE_ACTION_COMPONENT_DEFERRED")
        action.status, action.updated_at = CorporateActionStatus.PROCESSING.value, utc_now()
        self._publish_changed_facts(db, action_id=action_id)
        self._event(db, event_type="CORPORATE_ACTION_RECEIVABLE_UPDATED", action_id=action_id, payload={"business_version": str(action.action_version)})
        self._maybe_mark_completed(db, action=action, trading_day=trading_day)
        return CorporateActionResult(action_id, action.status)

    def list_pending_shares(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        """Move already entitled shares to the normal position only on listing day."""
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.listing_date != trading_day:
            raise BusinessRuleError("公司行为尚未到股份上市日", error_code="CORPORATE_ACTION_LISTING_DATE_INVALID")
        components = {row.component_id: row for row in db.scalars(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.action_id == action_id).with_for_update())}
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id).order_by(CashSecurityCorporateActionEntitlement.id).with_for_update()).all()
        for item in entitlements:
            component = components[item.component_id]
            volume = item.pending_share_volume
            if volume <= 0 or component.component_type not in {
                CorporateActionComponentType.STOCK_DIVIDEND.value,
                CorporateActionComponentType.CAPITALIZATION_ISSUE.value,
                CorporateActionComponentType.RIGHTS_ISSUE.value,
            }:
                continue
            account = db.scalar(select(Account).where(Account.account_id == item.account_id).with_for_update())
            position = db.scalar(select(Position).where(Position.position_id == item.position_id).with_for_update())
            self._ensure_replay_opening_balance(
                db, action=action, entitlement=item, component=component,
                position=position, effective_trading_day=trading_day,
            )
            ledger_created = self._ledger(
                db, entitlement=item, component=component,
                entry_type="SHARES_LISTED", action_version=action.action_version,
                effective_trading_day=trading_day,
                volume=volume, pending=-volume,
            )
            adjustment_created = self._position_adjustment(
                db, action=action, entitlement=item, component=component,
                adjustment_type="SHARES_LISTED",
                effective_trading_day=trading_day,
                total_volume=volume,
                yesterday_volume=volume,
                pending_volume=-volume,
                available_volume=volume,
                position_cost=item.subscription_cash,
            )
            if ledger_created and adjustment_created:
                position.pending_share_volume -= volume
                # Listing takes place before the next open; stock shares become
                # carried shares, convertible bonds remain T+0 available.
                position.total_volume += volume
                position.yesterday_volume += volume
                position.available_volume += volume
                position.position_cost = quantize_money(position.position_cost + item.subscription_cash)
                position.average_open_price = quantize_money(position.position_cost / Decimal(position.total_volume)) if position.total_volume else ZERO
                if position.daily_pnl_base_established:
                    position.yesterday_pnl_base_cost = quantize_money(Decimal(position.yesterday_pnl_base_cost) + item.subscription_cash)
                    position.daily_pnl_base_cost = quantize_money(Decimal(position.yesterday_pnl_base_cost) + Decimal(position.today_pnl_base_cost))
                pending_value = (
                    item.subscription_cash
                    if component.component_type == CorporateActionComponentType.RIGHTS_ISSUE.value
                    else quantize_money(Decimal(volume) * Decimal(position.mark_price or ZERO))
                )
                account.pending_security_value = quantize_money(max(ZERO, Decimal(account.pending_security_value) - pending_value))
                item.credited_share_volume += volume
                item.pending_share_volume = 0
                item.status, item.processed_at = CorporateActionEntitlementStatus.CREDITED.value, utc_now()
        self._publish_changed_facts(db, action_id=action_id)
        self._event(db, event_type="CORPORATE_ACTION_SHARES_LISTED", action_id=action_id, payload={"business_version": str(action.action_version)})
        self._maybe_mark_completed(db, action=action, trading_day=trading_day)
        return CorporateActionResult(action_id, action.status)

    def expire_rights(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.subscription_end_date is None or trading_day <= action.subscription_end_date:
            raise BusinessRuleError("配股认购期尚未结束", error_code="RIGHTS_SUBSCRIPTION_NOT_EXPIRED")
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).join(CashSecurityCorporateActionComponent, CashSecurityCorporateActionComponent.component_id == CashSecurityCorporateActionEntitlement.component_id).where(CashSecurityCorporateActionEntitlement.action_id == action_id, CashSecurityCorporateActionComponent.component_type == CorporateActionComponentType.RIGHTS_ISSUE.value).with_for_update()).all()
        changed = False
        for item in entitlements:
            if (
                item.subscribed_volume < item.entitled_share_volume
                and item.status != CorporateActionEntitlementStatus.EXPIRED.value
            ):
                item.status, item.updated_at = CorporateActionEntitlementStatus.EXPIRED.value, utc_now()
                changed = True
        if changed:
            self._event(db, event_type="RIGHTS_SUBSCRIPTION_EXPIRED", action_id=action_id, payload={"business_version": str(action.action_version)})
        self._maybe_mark_completed(db, action=action, trading_day=trading_day)
        return CorporateActionResult(action_id, action.status)

    def apply_bond_maturity(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        """Create principal receivables and retire convertible-bond positions once."""
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.ex_date != trading_day:
            raise BusinessRuleError("可转债尚未到期兑付日", error_code="BOND_MATURITY_DATE_INVALID")
        components = {row.component_id: row for row in db.scalars(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.action_id == action_id).with_for_update())}
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id).with_for_update()).all()
        for item in entitlements:
            component = components[item.component_id]
            if component.component_type != CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value:
                continue
            account = db.scalar(select(Account).where(Account.account_id == item.account_id).with_for_update())
            position = db.scalar(select(Position).where(Position.position_id == item.position_id).with_for_update())
            self._ensure_replay_opening_balance(
                db, action=action, entitlement=item, component=component,
                position=position, effective_trading_day=trading_day,
            )
            if position.frozen_volume or position.settlement_locked_volume:
                raise BusinessRuleError("到期兑付前仍有冻结持仓", error_code="BOND_MATURITY_POSITION_RESERVED")
            retired_volume = position.total_volume
            ledger_created = self._ledger(
                db, entitlement=item, component=component,
                entry_type="BOND_PRINCIPAL_RECEIVABLE",
                action_version=action.action_version,
                effective_trading_day=trading_day,
                receivable=item.entitled_cash_net, volume=-retired_volume,
            )
            adjustment_created = self._position_adjustment(
                db, action=action, entitlement=item, component=component,
                adjustment_type="BOND_MATURITY_RETIRED",
                effective_trading_day=trading_day,
                total_volume=-retired_volume,
                today_volume=-position.today_volume,
                yesterday_volume=-position.yesterday_volume,
                available_volume=-position.available_volume,
                position_cost=-Decimal(position.position_cost),
                daily_pnl_base_cost=-Decimal(position.daily_pnl_base_cost),
                average_open_price_after=ZERO,
                replay_payload={"volume_before": retired_volume, "volume_after": 0},
            )
            if ledger_created and adjustment_created:
                account.corporate_action_receivable = quantize_money(account.corporate_action_receivable + item.entitled_cash_net)
                position.total_volume = position.today_volume = position.yesterday_volume = position.available_volume = 0
                position.position_cost = position.average_open_price = ZERO
                position.daily_pnl_base_cost = position.yesterday_pnl_base_cost = position.today_pnl_base_cost = ZERO
                item.status, item.processed_at = CorporateActionEntitlementStatus.PENDING.value, utc_now()
        self._publish_changed_facts(db, action_id=action_id)
        self._event(db, event_type="BOND_MATURITY_REDEMPTION_CREATED", action_id=action_id, payload={"business_version": str(action.action_version)})
        self._maybe_mark_completed(db, action=action, trading_day=trading_day)
        return CorporateActionResult(action_id, action.status)

    def record_price_adjustment_factor(self, db: Session, *, action_id: str, trading_day: date, raw_previous_close: Decimal, official_ex_reference_price: Decimal, source_event_id: str, data_source: str) -> CashSecurityPriceAdjustmentFactor:
        if raw_previous_close <= ZERO or official_ex_reference_price <= ZERO:
            raise BusinessRuleError("除权参考价格必须为正", error_code="CORPORATE_ACTION_REFERENCE_PRICE_INVALID")
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.ex_date != trading_day:
            raise BusinessRuleError("公司行为尚未到除权日", error_code="CORPORATE_ACTION_EX_DATE_INVALID")
        existing = db.scalar(select(CashSecurityPriceAdjustmentFactor).where(CashSecurityPriceAdjustmentFactor.instrument_id == action.instrument_id, CashSecurityPriceAdjustmentFactor.trading_day == trading_day, CashSecurityPriceAdjustmentFactor.action_id == action_id))
        if existing is not None:
            return existing
        # A same-day official ex-reference price already incorporates all
        # notices effective on that day.  Persist one factor only; multiplying
        # one factor per announcement would adjust the same price twice.
        same_day = db.scalar(
            select(CashSecurityPriceAdjustmentFactor)
            .where(
                CashSecurityPriceAdjustmentFactor.instrument_id
                == action.instrument_id,
                CashSecurityPriceAdjustmentFactor.trading_day == trading_day,
            )
            .order_by(CashSecurityPriceAdjustmentFactor.id)
        )
        if same_day is not None:
            if Decimal(same_day.official_ex_reference_price) != Decimal(
                official_ex_reference_price
            ):
                raise ResourceConflictError(
                    "Same-day corporate actions require one authoritative ex-reference price",
                    error_code="CORPORATE_ACTION_REFERENCE_PRICE_CONFLICT",
                )
            return same_day
        factor = Decimal(official_ex_reference_price) / Decimal(raw_previous_close)
        row = CashSecurityPriceAdjustmentFactor(instrument_id=action.instrument_id, trading_day=trading_day, action_id=action_id, raw_previous_close=raw_previous_close, official_ex_reference_price=official_ex_reference_price, forward_adjustment_factor=factor, backward_adjustment_factor=Decimal("1") / factor, source_event_id=source_event_id, data_source=data_source, created_at=utc_now())
        db.add(row)
        self._event(db, event_type="CORPORATE_ACTION_PRICE_FACTOR_RECORDED", action_id=action_id, payload={"source_event_id": source_event_id, "business_version": str(action.action_version)})
        return row

    def run_due_actions(self, db: Session, *, trading_day: date) -> int:
        """EOD-owned orchestration.  It deliberately commits nowhere."""
        actions = db.scalars(
            select(CashSecurityCorporateAction)
            .where(CashSecurityCorporateAction.status.not_in((
                CorporateActionStatus.CANCELLED.value,
                CorporateActionStatus.SUPERSEDED.value,
                CorporateActionStatus.COMPLETED.value,
                CorporateActionStatus.MANUAL_REVIEW_REQUIRED.value,
            )))
            .order_by(CashSecurityCorporateAction.id)
        ).all()
        processed = 0
        for row in actions:
            # An imported event after its record day cannot safely infer a
            # historical holding from today's position; retain it for review.
            if row.status == CorporateActionStatus.CONFIRMED.value and row.record_date and row.record_date < trading_day:
                row.status, row.updated_at = CorporateActionStatus.MANUAL_REVIEW_REQUIRED.value, utc_now()
                self._event(db, event_type="CORPORATE_ACTION_MANUAL_REVIEW_REQUIRED", action_id=row.action_id, payload={"reason": "RECORD_DATE_SNAPSHOT_UNAVAILABLE", "business_version": str(row.action_version)})
                processed += 1
                continue
            if row.status == CorporateActionStatus.CONFIRMED.value and row.record_date == trading_day:
                self.capture_entitlements(db, action_id=row.action_id, trading_day=trading_day)
                processed += 1
            current = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == row.action_id))
            if current is None:
                continue
            processable = current.status in {
                CorporateActionStatus.ENTITLEMENT_CAPTURED.value,
                CorporateActionStatus.PROCESSING.value,
            }
            if current.ex_date == trading_day and processable:
                self.apply_ex_date(db, action_id=current.action_id, trading_day=trading_day)
                processed += 1
            # Principal must become a receivable before a same-day payment can
            # convert it to cash.
            if current.ex_date == trading_day and processable:
                components = db.scalars(select(CashSecurityCorporateActionComponent.component_type).where(CashSecurityCorporateActionComponent.action_id == current.action_id)).all()
                if CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value in components:
                    self.apply_bond_maturity(db, action_id=current.action_id, trading_day=trading_day)
                    processed += 1
            if current.payment_date == trading_day and processable:
                self.pay_cash(db, action_id=current.action_id, trading_day=trading_day)
                processed += 1
            if current.listing_date == trading_day and processable:
                self.list_pending_shares(db, action_id=current.action_id, trading_day=trading_day)
                processed += 1
            if processable and current.subscription_end_date and trading_day > current.subscription_end_date:
                self.expire_rights(db, action_id=current.action_id, trading_day=trading_day)
                processed += 1
            self._maybe_mark_completed(db, action=current, trading_day=trading_day)
        return processed

    def pay_cash(self, db: Session, *, action_id: str, trading_day: date) -> CorporateActionResult:
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or action.payment_date != trading_day:
            raise BusinessRuleError("公司行为尚未到支付日", error_code="CORPORATE_ACTION_PAYMENT_DATE_INVALID")
        components = {item.component_id: item for item in db.scalars(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.action_id == action_id).with_for_update())}
        entitlements = db.scalars(select(CashSecurityCorporateActionEntitlement).where(CashSecurityCorporateActionEntitlement.action_id == action_id).with_for_update()).all()
        for item in entitlements:
            component = components[item.component_id]
            if component.component_type not in {
                CorporateActionComponentType.CASH_DIVIDEND.value,
                CorporateActionComponentType.BOND_COUPON.value,
                CorporateActionComponentType.BOND_MATURITY_REDEMPTION.value,
                CorporateActionComponentType.STOCK_SPLIT.value,
                CorporateActionComponentType.REVERSE_SPLIT.value,
            }:
                continue
            account = db.scalar(select(Account).where(Account.account_id == item.account_id).with_for_update())
            amount = (
                item.cash_in_lieu
                if component.component_type in {CorporateActionComponentType.STOCK_SPLIT.value, CorporateActionComponentType.REVERSE_SPLIT.value}
                else item.entitled_cash_net
            )
            if self._ledger(db, entitlement=item, component=component, entry_type="CASH_PAID", action_version=action.action_version, effective_trading_day=trading_day, cash=amount, receivable=-amount):
                account.corporate_action_receivable = quantize_money(account.corporate_action_receivable - amount)
                account.cash_balance = quantize_money(account.cash_balance + amount)
                account.available_cash = quantize_money(account.available_cash + amount)
                item.status, item.processed_at = CorporateActionEntitlementStatus.PAID.value, utc_now()
        self._publish_changed_facts(db, action_id=action_id)
        self._event(db, event_type="CORPORATE_ACTION_CASH_PAID", action_id=action_id, payload={"business_version": str(action.action_version)})
        self._maybe_mark_completed(db, action=action, trading_day=trading_day)
        return CorporateActionResult(action_id, action.status)

    def subscribe_rights(self, db: Session, *, action_id: str, account_id: str, volume: int, client_request_id: str, access_scope: AccountAccessScope, trading_day: date) -> CashSecurityCorporateActionEntitlement:
        if volume <= 0:
            raise BusinessRuleError("认购数量必须为正", error_code="RIGHTS_SUBSCRIPTION_VOLUME_INVALID")
        # Access is checked before the idempotency result, then lock in the
        # documented order action -> component -> entitlement -> account -> position.
        account = db.scalar(select(Account).where(Account.account_id == account_id))
        if account is None or (not access_scope.is_admin and account.user_id != access_scope.user_id):
            raise ResourceNotFoundError("账户不存在")
        action = db.scalar(select(CashSecurityCorporateAction).where(CashSecurityCorporateAction.action_id == action_id).with_for_update())
        if action is None or not action.subscription_start_date or not action.subscription_end_date or not action.subscription_start_date <= trading_day <= action.subscription_end_date:
            raise BusinessRuleError("不在配股认购期间", error_code="RIGHTS_SUBSCRIPTION_WINDOW_INVALID")
        entitlement = db.scalar(select(CashSecurityCorporateActionEntitlement).join(CashSecurityCorporateActionComponent, CashSecurityCorporateActionComponent.component_id == CashSecurityCorporateActionEntitlement.component_id).where(CashSecurityCorporateActionEntitlement.action_id == action_id, CashSecurityCorporateActionEntitlement.account_id == account_id, CashSecurityCorporateActionComponent.component_type == CorporateActionComponentType.RIGHTS_ISSUE.value).with_for_update())
        if entitlement is None:
            raise ResourceNotFoundError("配股权益不存在")
        component = db.scalar(select(CashSecurityCorporateActionComponent).where(CashSecurityCorporateActionComponent.component_id == entitlement.component_id).with_for_update())
        account = db.scalar(select(Account).where(Account.account_id == account_id).with_for_update())
        existing_request = db.scalar(select(CashSecurityCorporateActionSubscription).where(
            CashSecurityCorporateActionSubscription.entitlement_id == entitlement.entitlement_id,
            CashSecurityCorporateActionSubscription.client_request_id == client_request_id,
        ))
        if existing_request is not None:
            if existing_request.volume != volume:
                raise ResourceConflictError(
                    "Same rights request id has a different volume",
                    error_code="RIGHTS_SUBSCRIPTION_IDEMPOTENCY_CONFLICT",
                )
            return entitlement
        if entitlement.client_request_id == client_request_id:
            return entitlement
        # client_request_id is a legacy one-request field.  It must not block
        # another valid partial subscription; the journal above is now the
        # idempotency authority.
        if entitlement.client_request_id is not None and entitlement.subscribed_volume >= entitlement.entitled_share_volume:
            raise ResourceConflictError("配股权益已被其他请求处理", error_code="RIGHTS_SUBSCRIPTION_IDEMPOTENCY_CONFLICT")
        if volume > entitlement.entitled_share_volume - entitlement.subscribed_volume:
            raise BusinessRuleError("认购数量超过可认购权益", error_code="RIGHTS_SUBSCRIPTION_EXCEEDED")
        cash = quantize_money(Decimal(volume) * component.subscription_price)
        if account.available_cash < cash or account.cash_balance < cash:
            raise BusinessRuleError("配股认购资金不足", error_code="RIGHTS_SUBSCRIPTION_CASH_INSUFFICIENT")
        position = db.scalar(select(Position).where(Position.position_id == entitlement.position_id).with_for_update())
        self._ensure_replay_opening_balance(
            db, action=action, entitlement=entitlement, component=component,
            position=position, effective_trading_day=trading_day,
        )
        request_token = sha256(client_request_id.encode()).hexdigest()[:32]
        ledger_created = self._ledger(
            db, entitlement=entitlement, component=component,
            entry_type="RIGHTS_SUBSCRIBED", action_version=action.action_version,
            effective_trading_day=trading_day,
            cash=-cash, pending=volume, cost=cash, idempotency_token=request_token,
        )
        adjustment_created = self._position_adjustment(
            db, action=action, entitlement=entitlement, component=component,
            adjustment_type="RIGHTS_SUBSCRIBED",
            effective_trading_day=trading_day,
            pending_volume=volume,
            position_cost=cash,
            idempotency_token=request_token,
        )
        if ledger_created and adjustment_created:
            account.cash_balance = quantize_money(account.cash_balance - cash)
            account.available_cash = quantize_money(account.available_cash - cash)
            position.pending_share_volume += volume
            account.pending_security_value = quantize_money(
                Decimal(account.pending_security_value) + cash
            )
            entitlement.subscribed_volume += volume
            entitlement.pending_share_volume += volume
            entitlement.subscription_cash = quantize_money(entitlement.subscription_cash + cash)
            entitlement.client_request_id = entitlement.client_request_id or client_request_id
            entitlement.status = (
                CorporateActionEntitlementStatus.SUBSCRIBED.value
                if entitlement.subscribed_volume == entitlement.entitled_share_volume
                else CorporateActionEntitlementStatus.PARTIALLY_SUBSCRIBED.value
            )
            entitlement.updated_at = utc_now()
            db.add(CashSecurityCorporateActionSubscription(
                subscription_id=self._id("CAS"), entitlement_id=entitlement.entitlement_id,
                action_id=action_id, account_id=account_id,
                client_request_id=client_request_id, volume=volume,
                cash_amount=cash, created_at=utc_now(),
            ))
            self._publish_changed_facts(db, action_id=action_id)
            self._event(db, event_type="RIGHTS_SUBSCRIPTION_CONFIRMED", action_id=action_id, payload={"account_id": account_id, "component_id": component.component_id, "business_version": str(action.action_version)})
        return entitlement
