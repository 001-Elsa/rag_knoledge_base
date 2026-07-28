"""Start a thread-pool Celery worker with a Prometheus metrics endpoint."""

import sys
from pathlib import Path

from prometheus_client import start_http_server

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.tasks import celery_app


def main() -> None:
    start_http_server(settings.worker_metrics_port)
    celery_app.worker_main(
        [
            "worker",
            "--loglevel=info",
            "--pool=threads",
            "--concurrency=2",
        ]
    )


if __name__ == "__main__":
    main()
