"""启动服务：`uv run device-agent` 或 `uv run python -m device_agent`。"""

import uvicorn

from device_agent.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "device_agent.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
