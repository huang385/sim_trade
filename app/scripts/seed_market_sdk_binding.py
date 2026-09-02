"""把当前服务器的两个行情SDK token绑定到指定客户端IP（测试用）。

用法：
    python -m app.scripts.seed_market_sdk_binding --client-ip 192.168.11.100 \
        --domain futures \
        [--remark 备注] [--mode lan] [--live-url ...] [--data-url ...]

token读取.env（对应行情域的 MARKET_DATA_API_TOKEN / YMM_DATA_SDK_TOKEN），
不通过命令行传递，避免凭证进入shell历史。
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from app.common.time_utils import utc_now
from app.core.database import SessionLocal
from app.models.market_sdk_token_binding import MarketSdkTokenBinding


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="绑定客户端IP与行情SDK凭证")
    parser.add_argument("--client-ip", required=True, help="客户端来源IP")
    parser.add_argument(
        "--domain",
        required=True,
        choices=("futures", "securities"),
        help="向终端发放期货域或证券域实时行情Token",
    )
    parser.add_argument("--remark", default="", help="备注，例如机器名或使用者")
    parser.add_argument(
        "--mode",
        default=os.getenv("REMOTE_MARKET_DATA_MODE", "lan"),
        help="连接模式：lan、TS或local",
    )
    parser.add_argument("--live-url", default="", help="实时行情WSS地址覆盖")
    parser.add_argument("--data-url", default="", help="数据库行情HTTP地址覆盖")
    parser.add_argument("--upsert", action="store_true", help="同IP已存在时覆盖更新")
    return parser


def main() -> None:
    load_dotenv(override=False)
    args = build_parser().parse_args()

    token_variable = f"{args.domain.upper()}_MARKET_DATA_API_TOKEN"
    live_token = os.getenv(token_variable, "").strip()
    data_token = os.getenv("YMM_DATA_SDK_TOKEN", "").strip()
    if not live_token:
        raise SystemExit(f"缺少 {token_variable}")
    if not data_token:
        raise SystemExit("缺少 YMM_DATA_SDK_TOKEN")
    if args.mode.strip().lower() not in {"lan", "ts", "local"}:
        raise SystemExit("--mode 必须是 lan、TS 或 local")

    db = SessionLocal()
    try:
        existing = (
            db.query(MarketSdkTokenBinding)
            .filter(MarketSdkTokenBinding.client_ip == args.client_ip.strip())
            .first()
        )
        if existing is not None:
            if not args.upsert:
                raise SystemExit(
                    f"{args.client_ip} 已存在绑定（id={existing.id}）；"
                    "加 --upsert 覆盖"
                )
            existing.live_sdk_token = live_token
            existing.data_sdk_token = data_token
            existing.mode = args.mode.strip().lower()
            existing.live_server_url = args.live_url.strip() or None
            existing.data_server_url = args.data_url.strip() or None
            existing.remark = args.remark.strip() or None
            existing.updated_at = utc_now()
            row = existing
        else:
            row = MarketSdkTokenBinding(
                client_ip=args.client_ip.strip(),
                live_sdk_token=live_token,
                data_sdk_token=data_token,
                mode=args.mode.strip().lower(),
                live_server_url=args.live_url.strip() or None,
                data_server_url=args.data_url.strip() or None,
                remark=args.remark.strip() or None,
            )
            db.add(row)
        db.commit()
        print(
            f"已绑定 client_ip={row.client_ip} mode={row.mode} "
            f"live_token={row.live_sdk_token[:12]}... "
            f"data_token={row.data_sdk_token[:8]}... remark={row.remark or ''}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
