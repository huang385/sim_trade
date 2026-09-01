from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_active_user, require_admin_user
from app.schemas.instrument_schema import (
    InstrumentResponse,
    InstrumentCreate,
    InstrumentCatalogItem,
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

catalog_router = APIRouter(
    prefix="/api/instruments",
    tags=["合约查询"],
    dependencies=[Depends(require_active_user)],
)


@catalog_router.get("/search", response_model=list[InstrumentCatalogItem])
def search_tradeable_derivatives(
    q: str = Query(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db, scope="function"),
    service: InstrumentService = Depends(get_instrument_service),
):
    """搜索可交易期货与期权，供桌面端合约下拉列表按需加载。"""

    return service.search_tradeable_derivatives(
        db,
        query=q,
        limit=limit,
    )


@catalog_router.get("/stocks/search", response_model=list[InstrumentCatalogItem])
def search_tradeable_stocks(
    q: str = Query(min_length=2, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db, scope="function"),
    service: InstrumentService = Depends(get_instrument_service),
):
    """搜索可交易股票和可转债，供桌面端证券代码输入提示使用。"""

    return service.search_tradeable_stocks(db, query=q, limit=limit)


@catalog_router.get("", response_model=list[InstrumentCatalogItem])
def list_tradeable_futures(
    db: Session = Depends(get_db, scope="function"),
    service: InstrumentService = Depends(get_instrument_service),
):
    """查询当前允许交易的期货合约，供桌面端选择和订阅。"""

    return service.list_tradeable_futures(db)


@router.put(
    "",
    response_model=InstrumentResponse,
)
def upsert_instrument(
    request: InstrumentCreate,
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
    db: Session = Depends(get_db, scope="function"),
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
