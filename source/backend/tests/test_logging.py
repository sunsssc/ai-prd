import logging
import re

from app.core.logging import configure_logging


def test_logging_format_includes_timestamp() -> None:
    configure_logging("INFO")
    logger = logging.getLogger("uvicorn.error")
    formatter = logger.handlers[0].formatter
    record = logger.makeRecord(
        name="uvicorn.error",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="服务启动",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO \[uvicorn\.error\] 服务启动", rendered)
