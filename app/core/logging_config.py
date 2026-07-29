from logging.config import dictConfig


def setup_logging() -> None:
    """
    初始化系统日志配置。
    """

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": (
                    "%(asctime)s "
                    "%(levelname)s "
                    "%(name)s "
                    "%(message)s"
                ),
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": "INFO",
            },
        },
        "root": {
            "handlers": ["console"],
            "level": "INFO",
        },
    }

    dictConfig(logging_config)
