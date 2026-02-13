import logging
import sys
from pathlib import Path
from typing import Optional

def setup_logging(
    level: str = "INFO",
    file: Optional[str] = None,
    console: bool = True
):
    """
    Setup logging configuration for PixieVeil.
    """
    # Clear existing handlers
    root = logging.getLogger()
    root.handlers.clear()

    # Set log level
    log_level = getattr(logging, level.upper())
    root.setLevel(log_level)

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # File handler
    if file:
        log_file = Path(file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    logging.info("Logging configured successfully")
