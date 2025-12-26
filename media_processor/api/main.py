"""
Media Processor REST API
"""
import os
import uuid
import shutil
import tempfile
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from celery.result import AsyncResult

from media_processor.celery_app import celery_app
from media_processor.tasks.pipeline import process_video_pipeline, process_file_pipeline

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
    embed_logo: bool = True
    video_bitrate: str = "500k"
    audio_bitrate: str = "64k"
    max_width: int = 720


class TaskSubmit(BaseModel):
    url: str
    options: Optional[TaskOptions] = None
    callback_url: Optional[str] = None
    logo_base64: Optional[str] = None  # Logo 图片的 base64 编码


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


@app.get("/test", response_class=HTMLResponse)
async def test_page():
    """测试页面"""
    # 获取项目根目录
    script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    test_page_path = os.path.join(script_dir, "test_page.html")

    if os.path.exists(test_page_path):
        with open(test_page_path, "r", encoding="utf-8") as f:
            return f.read()
    else:
        return HTMLResponse(content="<h1>Test page not found</h1>", status_code=404)


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
    - logo_base64: Logo 图片的 base64 编码（可选）
    """
    task_id = str(uuid.uuid4())

    options_dict = task.options.model_dump() if task.options else {}

    # 如果有 logo，添加到 options
    if task.logo_base64:
        options_dict["logo_base64"] = task.logo_base64

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


# ==================== 文件上传相关 ====================

@app.post("/api/transcribe")
async def transcribe_file(
    file: UploadFile = File(...),
    model: str = Form(default="base"),
    language: Optional[str] = Form(default=None)
):
    """
    快速转录 - 上传音频/视频文件，返回转录文本

    - file: 音频或视频文件
    - model: Whisper 模型 (tiny/base/small/medium/large/turbo)，默认 base
    - language: 指定语言代码（可选，如 en/zh/ja）

    返回: 转录文本和分段信息
    """
    import whisper
    import torch

    # 保存上传的文件
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保留原始扩展名
    ext = os.path.splitext(file.filename)[1] if file.filename else ".mp4"
    file_path = os.path.join(task_dir, f"input{ext}")

    try:
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # 选择设备
        if torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"

        # 加载模型
        try:
            whisper_model = whisper.load_model(model, device=device)
        except Exception:
            device = "cpu"
            whisper_model = whisper.load_model(model, device=device)

        # 转录
        result = whisper_model.transcribe(
            file_path,
            language=language,
            task="transcribe"
        )

        # 提取结果
        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "text": seg["text"].strip()
            })

        return {
            "task_id": task_id,
            "text": result.get("text", "").strip(),
            "language": result.get("language", "unknown"),
            "segments": segments,
            "model": model,
            "device": device
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # 清理临时文件
        try:
            shutil.rmtree(task_dir)
        except:
            pass


@app.post("/api/tasks/upload", response_model=TaskStatus)
async def submit_upload_task(
    file: UploadFile = File(...),
    translate: bool = Form(default=True),
    target_language: str = Form(default="zh"),
    embed_subtitles: bool = Form(default=True),
    embed_logo: bool = Form(default=True),
    video_bitrate: str = Form(default="500k"),
    max_width: int = Form(default=720),
    logo_base64: Optional[str] = Form(default=None),
    callback_url: Optional[str] = Form(default=None)
):
    """
    上传文件进行完整处理（转录+翻译+编码）

    - file: 视频文件
    - translate: 是否翻译
    - target_language: 目标语言
    - embed_subtitles: 是否嵌入字幕
    - embed_logo: 是否嵌入 logo
    - video_bitrate: 视频码率
    - max_width: 最大宽度
    - logo_base64: Logo 图片 base64
    - callback_url: 回调地址
    """
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 保存上传的文件
    ext = os.path.splitext(file.filename)[1] if file.filename else ".mp4"
    file_path = os.path.join(task_dir, f"input{ext}")

    try:
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存文件失败: {e}")

    options = {
        "translate": translate,
        "target_language": target_language,
        "embed_subtitles": embed_subtitles,
        "embed_logo": embed_logo,
        "video_bitrate": video_bitrate,
        "max_width": max_width,
    }

    if logo_base64:
        options["logo_base64"] = logo_base64

    # 提交到 Celery（使用文件路径而非 URL）
    result = process_file_pipeline.apply_async(
        args=[file_path, options, callback_url],
        task_id=task_id
    )

    return TaskStatus(
        task_id=task_id,
        status="queued",
        stage="pending",
        progress=0,
        created_at=datetime.now().isoformat()
    )


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=API_HOST, port=API_PORT)
