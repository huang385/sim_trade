"""订阅一个 YMM 行情代码并直接打印回调数据。"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

from dotenv import load_dotenv
from ymm_live_data_sdk import (
    LiveMarketDataClient,
    YMMMessageHubTokenInUseError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="打印 YMM 实时行情回调")
    parser.add_argument("--code", default="JD2609", help="标准行情代码")
    parser.add_argument("--seconds", type=float, default=30.0, help="订阅时长")
    parser.add_argument(
        "--mode",
        default=os.getenv("REMOTE_MARKET_DATA_MODE", "lan"),
        help="连接模式：lan、TS 或 local",
    )
    parser.add_argument(
        "--server-url",
        default=os.getenv("REMOTE_MARKET_DATA_BASE_URL") or None,
        help="可选 WSS 地址",
    )
    parser.add_argument(
        "--ca-file",
        default=os.getenv("REMOTE_MARKET_DATA_CA_FILE") or None,
        help="可选 CA 证书路径",
    )
    return parser


def main() -> None:
    load_dotenv(override=False)
    args = build_parser().parse_args()
    if args.seconds <= 0:
        raise SystemExit("--seconds 必须大于 0")

    code = args.code.strip().upper()
    token = os.getenv("REMOTE_MARKET_DATA_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 REMOTE_MARKET_DATA_API_TOKEN")

    def on_tick(messages: Any) -> None:
        # SDK 0.7 一次回调传入一批 tuple；逐条打印便于直接查看内容。
        batch = messages if isinstance(messages, tuple) else (messages,)
        for message in batch:
            if not isinstance(message, dict):
                continue
            if str(message.get("channel") or "").upper() != f"TICK_{code}":
                continue
            print(json.dumps(message, ensure_ascii=False, default=str))

    try:
        client = LiveMarketDataClient(
            token=token,
            mode=args.mode,
            server_url=args.server_url,
            ca_file=args.ca_file,
        )
    except YMMMessageHubTokenInUseError as exc:
        raise SystemExit(
            "当前 Token 已被行情 Worker 占用。请先停止 Worker，"
            "或使用另一个 Token 测试。"
        ) from exc

    try:
        client.listen(on_tick)
        client.subscribe([f"tick_{code}"])
        print(f"已订阅 tick_{code}，正在打印 {args.seconds:g} 秒回调数据…")
        time.sleep(args.seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
