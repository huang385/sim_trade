import os

import ymm_data_sdk
from dotenv import load_dotenv


load_dotenv()

ymm_data_sdk.init(
    token=os.environ["YMM_DATA_SDK_TOKEN"],
    mode=os.getenv("REMOTE_MARKET_DATA_MODE", "lan"),
)

data = ymm_data_sdk.get_price(
    "JD2608",
    start_date="2026-08-14",
    end_date="2026-08-1",
    frequency="tick",
)

print(data)
