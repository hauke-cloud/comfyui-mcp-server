"""Entry point: ``python -m comfyui_mcp_server`` or ``comfyui-mcp-server``."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.log_level != "debug":
        # The HTTP client logs every request at info, i.e. each status poll.
        for name in ("httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        # Probes and MCP polling would otherwise flood the log at info level.
        access_log=settings.log_level == "debug",
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
