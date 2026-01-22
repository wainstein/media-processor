"""
Structured Logger with JSON file + Redis Stream support

Dual storage architecture:
- JSON Lines file: Historical queries, 7-day retention with rotation
- Redis Stream: Real-time SSE streaming, auto-trimmed to 10000 entries
"""
import os
import json
import logging
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Optional
from contextvars import ContextVar

import redis

# Context variables for task info
_current_task_id: ContextVar[Optional[str]] = ContextVar("task_id", default=None)
_current_stage: ContextVar[Optional[str]] = ContextVar("stage", default=None)

# Redis configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_STREAM_KEY = "media_processor:logs"
REDIS_STREAM_MAXLEN = 10000

# Log file configuration
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs"))
LOG_FILE = os.path.join(LOG_DIR, "structured.jsonl")


class JsonFormatter(logging.Formatter):
    """Format log records as JSON Lines"""

    def format(self, record: logging.LogRecord) -> str:
        # Get timestamp with timezone
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        local_dt = dt.astimezone()

        log_entry = {
            "timestamp": local_dt.isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "source": record.name,
        }

        # Add task context if available
        task_id = _current_task_id.get()
        if task_id:
            log_entry["task_id"] = task_id

        stage = _current_stage.get()
        if stage:
            log_entry["stage"] = stage

        # Add extra fields from record
        if hasattr(record, "task_id") and record.task_id:
            log_entry["task_id"] = record.task_id
        if hasattr(record, "stage") and record.stage:
            log_entry["stage"] = record.stage

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


class RedisStreamHandler(logging.Handler):
    """Publish logs to Redis Stream for real-time SSE"""

    def __init__(self, redis_url: str = REDIS_URL, stream_key: str = REDIS_STREAM_KEY):
        super().__init__()
        self.stream_key = stream_key
        self.maxlen = REDIS_STREAM_MAXLEN
        self._redis_client: Optional[redis.Redis] = None
        self._redis_url = redis_url
        self._lock = threading.Lock()
        self._failed = False

    def _get_client(self) -> Optional[redis.Redis]:
        """Lazy initialization of Redis client"""
        if self._failed:
            return None

        if self._redis_client is None:
            with self._lock:
                if self._redis_client is None:
                    try:
                        self._redis_client = redis.from_url(
                            self._redis_url,
                            decode_responses=True,
                            socket_connect_timeout=2,
                            socket_timeout=2,
                        )
                        # Test connection
                        self._redis_client.ping()
                    except Exception:
                        self._failed = True
                        self._redis_client = None

        return self._redis_client

    def emit(self, record: logging.LogRecord):
        """Publish log to Redis Stream"""
        try:
            client = self._get_client()
            if client is None:
                return

            # Get timestamp
            dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
            local_dt = dt.astimezone()

            # Build log entry
            entry = {
                "timestamp": local_dt.isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "source": record.name,
            }

            # Add task context
            task_id = _current_task_id.get()
            if task_id:
                entry["task_id"] = task_id
            if hasattr(record, "task_id") and record.task_id:
                entry["task_id"] = record.task_id

            stage = _current_stage.get()
            if stage:
                entry["stage"] = stage
            if hasattr(record, "stage") and record.stage:
                entry["stage"] = record.stage

            # Publish to stream with auto-trim
            client.xadd(
                self.stream_key,
                entry,
                maxlen=self.maxlen,
                approximate=True,
            )
        except Exception:
            # Silently ignore Redis errors to not affect main application
            self._failed = True


class TaskLogger:
    """
    Task-aware logger that automatically includes task_id and stage in logs.

    Usage:
        logger = get_task_logger(__name__)
        logger.set_task("task-123")
        logger.set_stage("downloading")
        logger.info("Starting download")  # Will include task_id and stage
    """

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        self._task_id: Optional[str] = None
        self._stage: Optional[str] = None

    def set_task(self, task_id: str):
        """Set current task ID (also sets context var for nested loggers)"""
        self._task_id = task_id
        _current_task_id.set(task_id)

    def set_stage(self, stage: str):
        """Set current processing stage"""
        self._stage = stage
        _current_stage.set(stage)

    def clear(self):
        """Clear task context"""
        self._task_id = None
        self._stage = None
        _current_task_id.set(None)
        _current_stage.set(None)

    def _log(self, level: int, msg: str, *args, **kwargs):
        """Internal log method that adds task context"""
        extra = kwargs.get("extra", {})
        if self._task_id:
            extra["task_id"] = self._task_id
        if self._stage:
            extra["stage"] = self._stage
        kwargs["extra"] = extra
        self._logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs):
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)


# Global registry of task loggers
_task_loggers: dict[str, TaskLogger] = {}


def get_task_logger(name: str) -> TaskLogger:
    """
    Get or create a TaskLogger for the given module name.

    This is the main entry point - use it instead of logging.getLogger()
    """
    if name not in _task_loggers:
        _task_loggers[name] = TaskLogger(name)
    return _task_loggers[name]


def setup_structured_logging(level: int = logging.INFO):
    """
    Initialize structured logging with JSON file + Redis Stream handlers.

    Call this once at application startup (e.g., in main.py).
    """
    # Ensure log directory exists
    os.makedirs(LOG_DIR, exist_ok=True)

    # Get root logger for media_processor
    root_logger = logging.getLogger("media_processor")
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # 1. JSON Lines file handler with rotation (7 days)
    json_formatter = JsonFormatter()

    file_handler = TimedRotatingFileHandler(
        LOG_FILE,
        when="D",  # Daily rotation
        interval=1,
        backupCount=7,  # Keep 7 days
        encoding="utf-8",
    )
    file_handler.setFormatter(json_formatter)
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    # 2. Redis Stream handler for real-time SSE
    redis_handler = RedisStreamHandler()
    redis_handler.setFormatter(json_formatter)
    redis_handler.setLevel(level)
    root_logger.addHandler(redis_handler)

    # 3. Console handler for development (human-readable)
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    root_logger.info("Structured logging initialized", extra={"stage": "startup"})
