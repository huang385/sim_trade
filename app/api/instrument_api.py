from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin_user
from app.schemas.instrument_schema import (
    InstrumentResponse,
    InstrumentCreate,
)
from app.services.instrument_service import (
    InstrumentService,
    get_instrument_service,
)


router = APIRouter(
    prefix="/api/admin/instruments",
    tags=["合约管理"],
    dependencies=[Depends(require_admin_user)],
)


@router.put(
    "",
    response_model=InstrumentResponse,
)
def upsert_instrument(
    request: InstrumentCreate,
    db: Session = Depends(get_db),
    service: InstrumentService = Depends(
        get_instrument_service
    ),
):
    """
    人工创建或更新合约。

    正常情况下合约由同步程序写入。
    """

    return service.upsert_manual(
        db=db,
        request=request,
    )


@router.get(
    "/{exchange_id}/{symbol}",
    response_model=InstrumentResponse,
)
def get_instrument(
    exchange_id: str,
    symbol: str,
    db: Session = Depends(get_db),
    service: InstrumentService = Depends(
        get_instrument_service
    ),
):
    """
    查询合约。
    """

    return service.get_instrument(
        db=db,
        exchange_id=exchange_id,
        symbol=symbol,
    )


@router.get(
    "",
    response_model=list[InstrumentResponse],
)
def list_instruments(
    exchange_id: str | None = Query(
        default=None,
    ),
    only_active: bool | None = Query(
        default=None,
    ),
    db: Session = Depends(get_db),
    service: InstrumentService = Depends(
        get_instrument_service
    ),
):
    """
    查询合约列表。
    """

    return service.list_instruments(
        db=db,
        exchange_id=exchange_id,
        only_active=only_active,
    )
