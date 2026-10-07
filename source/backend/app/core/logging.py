from logging.config import dictConfig


LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(log_level: str) -> None:
    level = log_level.upper()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": LOG_FORMAT,
                    "datefmt": LOG_DATE_FORMAT,
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "handlers": ["default"],
                "level": level,
            },
            "loggers": {
                "uvicorn": {
                    "handlers": ["default"],
                    "level": level,
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["default"],
                    "level": level,
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["default"],
                    "level": level,
                    "propagate": False,
                },
            },
        }
    )
