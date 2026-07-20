def normalize_code(value: str) -> str:
    """
    标准化合约代码、交易所代码。

    处理规则：
    1. 去除前后空格；
    2. 转换成大写。

    示例：
        " rb2610 " -> "RB2610"
        " shfe "   -> "SHFE"
    """

    if not isinstance(value, str):
        raise ValueError("代码必须是字符串")

    normalized_value = value.strip().upper()

    if not normalized_value:
        raise ValueError("代码不能为空")

    return normalized_value