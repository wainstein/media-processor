"""
Media Processor REST API
"""
import os
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from celery.result import AsyncResult

from media_processor.celery_app import celery_app
from media_processor.tasks.pipeline import process_video_pipeline

# 配置
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/media_processor")

app = FastAPI(
    title="Media Processor API",
    description="视频下载、转录、翻译、编码微服务",
    version="0.1.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 数据模型 ====================

class TaskOptions(BaseModel):
    translate: bool = True
    target_language: str = "zh"
    embed_subtitles: bool = True
    video_bitrate: str = "500k"
    audio_bitrate: str = "64k"
    max_width: int = 720


class TaskSubmit(BaseModel):
    url: str
    options: Optional[TaskOptions] = None
    callback_url: Optional[str] = None


class TaskStatus(BaseModel):
    task_id: str
    status: str
    stage: Optional[str] = None
    progress: Optional[int] = None
    created_at: Optional[str] = None
    error: Optional[str] = None


class TaskResult(BaseModel):
    task_id: str
    status: str
    output_path: Optional[str] = None
    subtitle_path: Optional[str] = None
    file_size: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# ==================== API 路由 ====================

@app.get("/")
async def root():
    """健康检查"""
    return {
        "service": "Media Processor",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/health")
async def health():
    """健康检查 (含队列状态)"""
    try:
        # 检查 Celery 连接
        inspect = celery_app.control.inspect()
        stats = inspect.stats()
        active = inspect.active()

        return {
            "status": "healthy",
            "redis": "connected",
            "workers": len(stats) if stats else 0,
            "active_tasks": sum(len(tasks) for tasks in (active or {}).values())
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)}
        )


@app.post("/api/tasks", response_model=TaskStatus)
async def submit_task(task: TaskSubmit):
    """
    提交视频处理任务

    - url: 视频 URL (YouTube, Twitter, Instagram 等)
    - options: 处理选项
    - callback_url: 完成后回调地址
    """
    task_id = str(uuid.uuid4())

    options_dict = task.options.model_dump() if task.options else {}

    # 提交到 Celery
    result = process_video_pipeline.apply_async(
        args=[task.url, options_dict, task.callback_url],
        task_id=task_id
    )

    return TaskStatus(
        task_id=task_id,
        status="queued",
        stage="pending",
        progress=0,
        created_at=datetime.now().isoformat()
    )


@app.get("/api/tasks/{task_id}", response_model=TaskStatus)
async def get_task_status(task_id: str):
    """
    查询任务状态
    """
    result = AsyncResult(task_id, app=celery_app)

    if result.state == "PENDING":
        return TaskStatus(
            task_id=task_id,
            status="queued",
            stage="pending",
            progress=0
        )

    elif result.state in ("DOWNLOADING", "TRANSCRIBING", "TRANSLATING", "ENCODING"):
        meta = result.info or {}
        return TaskStatus(
            task_id=task_id,
            status="processing",
            stage=meta.get("stage", result.state.lower()),
            progress=meta.get("progress", 0)
        )

    elif result.state == "SUCCESS":
        return TaskStatus(
            task_id=task_id,
            status="completed",
            stage="done",
            progress=100
        )

    elif result.state == "FAILURE":
        return TaskStatus(
            task_id=task_id,
            status="failed",
            error=str(result.info) if result.info else "Unknown error"
        )

    else:
        return TaskStatus(
            task_id=task_id,
            status=result.state.lower()
        )


@app.get("/api/tasks/{task_id}/result", response_model=TaskResult)
async def get_task_result(task_id: str):
    """
    获取任务结果
    """
    result = AsyncResult(task_id, app=celery_app)

    if result.state == "PENDING":
        raise HTTPException(status_code=404, detail="任务不存在或尚未开始")

    if result.state == "FAILURE":
        return TaskResult(
            task_id=task_id,
            status="failed",
            error=str(result.info) if result.info else "Unknown error"
        )

    if result.state != "SUCCESS":
        raise HTTPException(status_code=400, detail=f"任务尚未完成: {result.state}")

    data = result.get()
    return TaskResult(
        task_id=task_id,
        status="completed",
        output_path=data.get("output_path"),
        subtitle_path=data.get("subtitle_path"),
        file_size=data.get("file_size"),
        metadata=data.get("metadata")
    )


@app.get("/api/tasks/{task_id}/download")
async def download_output(task_id: str):
    """
    下载处理后的视频文件
    """
    result = AsyncResult(task_id, app=celery_app)

    if result.state != "SUCCESS":
        raise HTTPException(status_code=400, detail="任务尚未完成")

    data = result.get()
    output_path = data.get("output_path")

    if not output_path or not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="输出文件不存在")

    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=f"{task_id}.mp4"
    )


@app.get("/api/tasks/{task_id}/subtitle")
async def download_subtitle(task_id: str):
    """
    下载字幕文件
    """
    result = AsyncResult(task_id, app=celery_app)

    if result.state != "SUCCESS":
        raise HTTPException(status_code=400, detail="任务尚未完成")

    data = result.get()
    subtitle_path = data.get("subtitle_path")

    if not subtitle_path or not os.path.exists(subtitle_path):
        raise HTTPException(status_code=404, detail="字幕文件不存在")

    return FileResponse(
        subtitle_path,
        media_type="text/plain",
        filename=f"{task_id}.ass"
    )


@app.delete("/api/tasks/{task_id}")
async def cancel_task(task_id: str):
    """
    取消任务
    """
    result = AsyncResult(task_id, app=celery_app)

    if result.state in ("SUCCESS", "FAILURE"):
        raise HTTPException(status_code=400, detail="任务已完成，无法取消")

    celery_app.control.revoke(task_id, terminate=True)

    return {"task_id": task_id, "status": "cancelled"}


@app.get("/api/queue/stats")
async def queue_stats():
    """
    获取队列统计信息
    """
    try:
        inspect = celery_app.control.inspect()

        stats = inspect.stats() or {}
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        scheduled = inspect.scheduled() or {}

        return {
            "workers": list(stats.keys()),
            "active_tasks": {k: len(v) for k, v in active.items()},
            "reserved_tasks": {k: len(v) for k, v in reserved.items()},
            "scheduled_tasks": {k: len(v) for k, v in scheduled.items()},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
