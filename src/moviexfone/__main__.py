"""Run the local demo: python -m moviexfone"""

from __future__ import annotations

import uvicorn

from moviexfone.api.security import enforce_deployment
from moviexfone.config import Settings


def main() -> None:
    settings = Settings.from_env()
    enforce_deployment(settings)
    uvicorn.run(
        "moviexfone.api.app:create_app",
        factory=True,
        host=settings.bind,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
