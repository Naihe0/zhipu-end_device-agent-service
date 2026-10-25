import logging

NOISY_LOGGERS = ("httpx", "httpcore", "mcp", "a2a", "google_genai", "sse_starlette", "aiosqlite")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("device_agent").setLevel(level.upper())
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
