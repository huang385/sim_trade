from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin_user
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import (
    UserCreateRequest,
    UserResponse,
    UserStatusUpdateRequest,
)
from app.services.admin_user_service import AdminUserService
from app.services.password_service import PasswordService


router = APIRouter(
    prefix="/api/admin/users",
    tags=["用户管理"],
    dependencies=[Depends(require_admin_user)],
)
_service = AdminUserService(
    repository=UserRepository(),
    password_service=PasswordService(),
)


def get_admin_user_service() -> AdminUserService:
    return _service


@router.post("", response_model=UserResponse)
def create_user(
    request: UserCreateRequest,
    db: Session = Depends(get_db),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return service.create_user(db, request)


@router.get("", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return service.list_users(db)


@router.patch("/{user_id}/status", response_model=UserResponse)
def update_user_status(
    user_id: str,
    request: UserStatusUpdateRequest,
    db: Session = Depends(get_db),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return service.update_status(
        db, user_id=user_id, status=request.status
    )
