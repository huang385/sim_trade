import os

import ymm_data_sdk
from dotenv import load_dotenv


load_dotenv()

ymm_data_sdk.init(
    token=os.environ["YMM_DATA_SDK_TOKEN"],
    mode=os.getenv("REMOTE_MARKET_DATA_MODE", "lan"),
)

data = ymm_data_sdk.get_price(
    "JD2609C3200",
    start_date="2026-08-18",
    end_date="2026-08-18",
    frequency="tick",
)

# get_price 的 tick 结果通常以 order_book_id、datetime 作为多级索引返回。
# 先还原为普通列，后续才能按四个展示字段筛选。
if "order_book_id" not in data.columns or "datetime" not in data.columns:
    data = data.reset_index()


def _first_existing_column(*candidates: str) -> str:
    for column in candidates:
        if column in data.columns:
            return column
    raise KeyError(
        f"未找到行情字段 {candidates}，实际字段为: {list(data.columns)}"
    )


bid_price_column = _first_existing_column("b1", "b1_v")
ask_price_column = _first_existing_column("a1", "a1_v")

result = data[
    ["order_book_id", "datetime", bid_price_column, ask_price_column]
].rename(
    columns={
        bid_price_column: "买一价",
        ask_price_column: "卖一价",
    }
)
print(result)
