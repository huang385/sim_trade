class AppError(Exception):
    """
    系统业务异常基类。

    Service层抛出该异常，
    HTTP层负责将它转换成JSON响应。
    """

    status_code = 400
    error_code = "APP_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
    ):
        self.message = message

        if error_code is not None:
            self.error_code = error_code

        super().__init__(message)


class BusinessValidationError(AppError):
    """
    业务参数校验失败。
    """

    status_code = 400
    error_code = "BUSINESS_VALIDATION_ERROR"


class ResourceNotFoundError(AppError):
    """
    查询的数据不存在。
    """

    status_code = 404
    error_code = "RESOURCE_NOT_FOUND"


class ResourceConflictError(AppError):
    """
    数据冲突。

    例如：
    1. 账户已经存在；
    2. 唯一键冲突；
    3. 数据版本冲突。
    """

    status_code = 409
    error_code = "RESOURCE_CONFLICT"


class BusinessRuleError(AppError):
    """
    业务规则不允许执行。

    例如：
    1. 合约停止交易；
    2. 保证金规则交易日不匹配；
    3. 账户状态禁止下单。
    """

    status_code = 422
    error_code = "BUSINESS_RULE_ERROR"


class DataAccessError(AppError):
    """
    数据库访问异常。
    """

    status_code = 500
    error_code = "DATA_ACCESS_ERROR"