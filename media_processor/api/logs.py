"""
Log API Endpoints

Provides:
- GET /api/logs - Static query from JSON Lines file
- GET /api/logs/stream - SSE real-time streaming from Redis Stream
- GET /api/logs/task/{task_id} - Task-specific logs
"""
import os
import json
import asyncio
from datetime import datetime
from typing import Optional, AsyncGenerator

from fastapi import APIRouter, Query, HTTPException
from sse_starlette.sse import EventSourceResponse
import redis.asyncio as aioredis

from media_processor.logging.structured_logger import (
    LOG_FILE,
    REDIS_URL,
    REDIS_STREAM_KEY,
)

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _parse_log_line(line: str) -> Optional[dict]:
    """Parse a JSON log line, return None if invalid"""
    try:
        return json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _filter_log(
    log: dict,
    task_id: Optional[str] = None,
    level: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> bool:
    """Check if log entry matches filters"""
    # Task ID filter
    if task_id and log.get("task_id") != task_id:
        return False

    # Level filter
    if level:
        log_level = log.get("level", "").upper()
        filter_level = level.upper()
        level_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        if level_order.get(log_level, 0) < level_order.get(filter_level, 0):
            return False

    # Time filters
    log_time = log.get("timestamp", "")
    if since and log_time < since:
        return False
    if until and log_time > until:
        return False

    return True


@router.get("")
async def get_logs(
    lines: int = Query(default=100, ge=1, le=10000, description="Number of lines to return"),
    task_id: Optional[str] = Query(default=None, description="Filter by task ID"),
    level: Optional[str] = Query(default=None, description="Minimum log level (DEBUG, INFO, WARNING, ERROR)"),
    since: Optional[str] = Query(default=None, description="Start time (ISO format)"),
    until: Optional[str] = Query(default=None, description="End time (ISO format)"),
):
    """
    Query historical logs from JSON Lines file.

    Examples:
    - /api/logs?lines=50
    - /api/logs?task_id=550e8400-e29b-41d4-a716-446655440000
    - /api/logs?level=ERROR
    - /api/logs?since=2026-01-22T10:00:00
    """
    if not os.path.exists(LOG_FILE):
        return {"logs": [], "total": 0, "file": LOG_FILE, "error": "Log file not found"}

    # Read all matching logs (reverse order for newest first)
    matching_logs = []

    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        # Process in reverse order (newest first)
        for line in reversed(all_lines):
            if len(matching_logs) >= lines:
                break

            log = _parse_log_line(line)
            if log and _filter_log(log, task_id, level, since, until):
                matching_logs.append(log)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading log file: {e}")

    return {
        "logs": matching_logs,
        "total": len(matching_logs),
        "filters": {
            "task_id": task_id,
            "level": level,
            "since": since,
            "until": until,
        }
    }


@router.get("/task/{task_id}")
async def get_task_logs(
    task_id: str,
    lines: int = Query(default=500, ge=1, le=10000),
    level: Optional[str] = Query(default=None),
):
    """
    Get logs for a specific task.

    Convenience endpoint that filters by task_id.
    """
    return await get_logs(lines=lines, task_id=task_id, level=level)


async def _log_stream_generator(
    task_id: Optional[str] = None,
    level: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """
    Generate log events from Redis Stream.

    Uses XREAD with blocking to wait for new logs.
    """
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)

        # Start from current time (only new logs)
        last_id = "$"

        while True:
            try:
                # Block for 5 seconds waiting for new logs
                result = await client.xread(
                    {REDIS_STREAM_KEY: last_id},
                    count=100,
                    block=5000,
                )

                if result:
                    stream_name, messages = result[0]
                    for msg_id, fields in messages:
                        last_id = msg_id

                        # Apply filters
                        if task_id and fields.get("task_id") != task_id:
                            continue

                        if level:
                            log_level = fields.get("level", "").upper()
                            filter_level = level.upper()
                            level_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
                            if level_order.get(log_level, 0) < level_order.get(filter_level, 0):
                                continue

                        yield fields
                else:
                    # Send heartbeat to keep connection alive
                    yield {"type": "heartbeat", "timestamp": datetime.now().isoformat()}

            except asyncio.CancelledError:
                break
            except Exception as e:
                yield {"type": "error", "message": str(e)}
                await asyncio.sleep(1)

    except Exception as e:
        yield {"type": "error", "message": f"Redis connection failed: {e}"}
    finally:
        try:
            await client.close()
        except:
            pass


@router.get("/stream")
async def stream_logs(
    task_id: Optional[str] = Query(default=None, description="Filter by task ID"),
    level: Optional[str] = Query(default=None, description="Minimum log level"),
):
    """
    Stream logs in real-time using Server-Sent Events (SSE).

    Similar to `tail -f` - shows new logs as they arrive.

    Examples:
    - curl -N "http://localhost:8000/api/logs/stream"
    - curl -N "http://localhost:8000/api/logs/stream?task_id=xxx"
    - curl -N "http://localhost:8000/api/logs/stream?level=ERROR"
    """
    async def event_generator():
        async for log_entry in _log_stream_generator(task_id, level):
            yield {
                "event": log_entry.get("type", "log"),
                "data": json.dumps(log_entry, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())
