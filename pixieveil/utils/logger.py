"""
Logging Utilities Module

This module provides centralized logging configuration utilities for the PixieVeil application.
It offers flexible logging setup with support for file and console output, custom log levels,
and consistent formatting across all application components.

Functions:
    setup_logging: Configure logging with customizable output handlers and levels
"""

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
    
    This function provides a centralized logging configuration utility that sets up
    Python's logging system with customizable output handlers, log levels, and
    consistent formatting. It supports both file and console output with flexible
    configuration options.
    
    The logging setup includes:
    - Configurable log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    - Optional file output with automatic directory creation
    - Optional console output to stdout
    - Consistent timestamped format for all log messages
    - Clearing of existing handlers to prevent duplicate logging
    
    Args:
        level (str): Log level as string (default: "INFO")
                     Valid values: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
        file (Optional[str]): Path to log file. If None, no file logging is enabled.
                            The parent directory will be created if it doesn't exist.
        console (bool): Whether to enable console logging to stdout (default: True)
                       If False, only file logging will be enabled (if file is specified)
    
    Returns:
        None: This function configures the logging system in-place
        
    Note:
        The function clears all existing logging handlers to prevent duplicate
        log messages, which is particularly useful when the application is restarted
        or when logging needs to be reconfigured during runtime.
        
    Example:
        # Basic console logging only
        setup_logging(level="INFO", console=True)
        
        # File and console logging
        setup_logging(level="DEBUG", file="logs/pixieveil.log", console=True)
        
        # File logging only
        setup_logging(level="WARNING", file="logs/errors.log", console=False)
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
