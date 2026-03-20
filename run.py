import uvicorn
import sys
import logging
import signal
import os
from app.main import app


def signal_handler(signum, frame):
    print("\nShutting down gracefully...")
    sys.exit(0)


def run_dev():
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8001,          # default 8001 so it doesn't clash with tutoria-api on 8000
        reload=True,
        reload_dirs=["app"],
        log_level="debug",
        access_log=True,
        use_colors=True,
    )


def run_prod():
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    port = int(os.environ.get("PORT", 8001))

    # Worker is CPU/memory heavy — keep concurrency low (1-2 workers max).
    # The daily jobs are asyncio tasks, not thread-based, so they share one process fine.
    workers = int(os.environ.get("WEB_CONCURRENCY", 1))

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        loop="uvloop",
        http="httptools",
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dev":
        run_dev()
    else:
        run_prod()
