import ymm_data_sdk

ymm_data_sdk.init(token="dVKQ2TlgsU9CT6XZP2A6B0z1EGc4wp2f2V0Yf6R9zZ8")

ticks = ymm_data_sdk.get_price(
    "JD2608",
    start_date="2026-08-07",
    end_date="2026-08-07",
    frequency="tick",
)

if ticks is None or ticks.empty:
    print("08-07数据尚未入库")
else:
    print("记录数：", len(ticks))
    print(ticks.tail(5).to_string())