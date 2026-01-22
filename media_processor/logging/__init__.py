"""
Structured Logging Module

Provides JSON logging to file and Redis Stream for real-time SSE.
"""
from .structured_logger import (
    setup_structured_logging,
    get_task_logger,
    TaskLogger,
)

__all__ = [
    "setup_structured_logging",
    "get_task_logger",
    "TaskLogger",
]
