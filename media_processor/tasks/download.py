"""
下载任务 - 使用 yt-dlp 下载视频
"""
import os
import json
import logging
import tempfile
import subprocess
from typing import Optional
from celery import shared_task

logger = logging.getLogger(__name__)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", tempfile.gettempdir())


@shared_task(bind=True, name="media_processor.tasks.download.download_video")
def download_video(
    self,
    url: str,
    task_id: str,
    format_spec: str = None
) -> dict:
    """
    下载视频任务

    Args:
        url: 视频 URL (YouTube, Twitter, Instagram 等)
        task_id: 任务 ID (用于文件命名)
        format_spec: yt-dlp 格式规格 (可选，默认自动选择最佳)

    Returns:
        {
            "video_path": "/path/to/video.mp4",
            "title": "Video Title",
            "description": "Video description",
            "duration": 180,
            "thumbnail_path": "/path/to/thumb.jpg"
        }
    """
    logger.info(f"[{task_id}] 开始下载: {url}")

    # 创建任务专属目录
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    output_template = os.path.join(task_dir, "video.%(ext)s")

    # 更新任务状态
    self.update_state(state="DOWNLOADING", meta={"progress": 0, "stage": "downloading"})

    # 格式优先级链 - 从最优到最兼容
    format_fallbacks = [
        format_spec,  # 用户指定的格式
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "bestvideo+bestaudio/best",
        "best",
        None  # 让 yt-dlp 自动选择
    ]

    last_error = None
    result = None

    for fmt in format_fallbacks:
        if fmt is None and last_error is None:
            continue  # 跳过第一次的 None

        try:
            # 清理之前失败的文件
            for f in os.listdir(task_dir):
                os.remove(os.path.join(task_dir, f))
        except:
            pass

        try:
            # 使用 yt-dlp 下载
            cmd = [
                "yt-dlp",
                "--output", output_template,
                "--write-thumbnail",
                "--convert-thumbnails", "jpg",
                "--merge-output-format", "mp4",
                "--no-playlist",
                "--no-check-formats",  # 跳过格式检查，加快速度
                "--extractor-retries", "3",
                "--print-json",
            ]

            # 添加格式选择
            if fmt:
                cmd.extend(["--format", fmt])

            cmd.append(url)

            logger.info(f"[{task_id}] 尝试格式: {fmt or 'auto'}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600  # 10分钟超时
            )

            if result.returncode == 0:
                logger.info(f"[{task_id}] 格式 {fmt or 'auto'} 下载成功")
                break  # 成功，跳出循环

            last_error = result.stderr
            logger.warning(f"[{task_id}] 格式 {fmt or 'auto'} 失败: {last_error[:200]}...")

        except subprocess.TimeoutExpired:
            last_error = "下载超时"
            logger.warning(f"[{task_id}] 格式 {fmt or 'auto'} 超时")
            continue
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{task_id}] 格式 {fmt or 'auto'} 异常: {e}")
            continue

    # 检查是否成功
    if result is None or result.returncode != 0:
        logger.error(f"[{task_id}] 所有格式都失败: {last_error}")
        raise Exception(f"yt-dlp 所有格式都失败: {last_error}")

    # 解析输出
    try:
        info = json.loads(result.stdout.strip().split('\n')[-1])
    except json.JSONDecodeError:
        info = {}
        logger.warning(f"[{task_id}] 无法解析 yt-dlp JSON 输出")

    # 查找下载的视频文件
    video_path = None
    for ext in ["mp4", "mkv", "webm", "mov"]:
        potential_path = os.path.join(task_dir, f"video.{ext}")
        if os.path.exists(potential_path):
            video_path = potential_path
            break

    if not video_path:
        # 尝试查找任何视频文件
        for f in os.listdir(task_dir):
            if f.endswith((".mp4", ".mkv", ".webm", ".mov")):
                video_path = os.path.join(task_dir, f)
                break

    if not video_path:
        raise Exception("下载完成但找不到视频文件")

    # 查找缩略图
    thumb_found = None
    for f in os.listdir(task_dir):
        if f.endswith((".jpg", ".png", ".webp")):
            thumb_found = os.path.join(task_dir, f)
            break

    logger.info(f"[{task_id}] 下载完成: {video_path}")

    return {
        "video_path": video_path,
        "title": info.get("title", ""),
        "description": info.get("description", ""),
        "duration": info.get("duration", 0),
        "thumbnail_path": thumb_found,
        "uploader": info.get("uploader", ""),
        "upload_date": info.get("upload_date", ""),
    }
