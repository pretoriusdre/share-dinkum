import logging
import sys
import os
from pathlib import Path

def setup_logging(level=logging.INFO, log_dir=None, log_file="app.log"):
    """
    Basic logging configuration.
    Writes logs to both console and a file under log_dir.
    """

    # Default to BASE_DIR/logs if not provided
    if log_dir is None:
        base_dir = Path(__file__).resolve().parent.parent  # adjust as needed
        log_dir = base_dir / "logs"
    
    log_dir.mkdir(parents=True, exist_ok=True)  # create if doesn't exist
    log_path = log_dir / log_file

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),   # console
            logging.FileHandler(log_path, mode="a"),  # file
        ],
    )

    # Quiet noisy loggers
    logging.getLogger("django.db.backends").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # During tests, expected API errors are mocked; suppress INFO/ERROR from yfinanceinterface
    if "test" in sys.argv:
        logging.getLogger("share_dinkum_app.yfinanceinterface").setLevel(logging.CRITICAL)

    return logging.getLogger(__name__)