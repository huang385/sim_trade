import argparse
from getpass import getpass

from app.common.exceptions import AppError
from app.core.database import SessionLocal
from app.enums.auth_enums import UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.user_schema import UserCreateRequest
from app.services.admin_user_service import AdminUserService
from app.services.password_service import PasswordService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="创建sim_trade初始管理员（密码通过终端隐藏输入）"
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    password = getpass("管理员密码（至少12个字符）: ")
    confirmation = getpass("再次输入管理员密码: ")
    if password != confirmation:
        print("创建失败：两次密码输入不一致")
        return 1

    try:
        request = UserCreateRequest(
            user_id=args.user_id,
            username=args.username,
            display_name=args.display_name,
            password=password,
            role=UserRole.ADMIN,
        )
        with SessionLocal() as db:
            user = AdminUserService(
                repository=UserRepository(),
                password_service=PasswordService(),
            ).create_user(db, request)
        print(
            f"管理员创建成功 user_id={user.user_id} "
            f"username={user.username}"
        )
        return 0
    except (AppError, ValueError) as exc:
        # 输出只包含业务错误，不打印原始密码或密码哈希。
        print(f"创建失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
