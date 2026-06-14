import logging


def setup_logger(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="[%(asctime)s] %(levelname)s - %(message)s",
    )
    return logging.getLogger("llm-trading-bot")
