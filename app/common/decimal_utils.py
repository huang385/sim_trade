from decimal import Decimal, ROUND_HALF_UP


# 当前账户金额字段使用Numeric(24, 6)，
# 因此业务计算结果统一保留六位小数。
MONEY_QUANT = Decimal("0.000001")


def quantize_money(value: Decimal) -> Decimal:
    """
    将金额统一保留六位小数。

    使用ROUND_HALF_UP进行四舍五入。

    示例：
        Decimal("1.2345678")
        转换为
        Decimal("1.234568")
    """

    if not isinstance(value, Decimal):
        raise TypeError("资金计算结果必须是Decimal类型")

    return value.quantize(
        MONEY_QUANT,
        rounding=ROUND_HALF_UP,
    )