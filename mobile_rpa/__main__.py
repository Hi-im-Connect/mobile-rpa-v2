"""Run the dashboard: python -m mobile_rpa (MRPA_HOST / MRPA_PORT, default 127.0.0.1:8090)."""

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("MRPA_HOST", "127.0.0.1"),
        port=int(os.environ.get("MRPA_PORT", "8090")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="warning",
        timeout_graceful_shutdown=3,  # open SSE/video streams must not stall restarts
    )


if __name__ == "__main__":
    main()
