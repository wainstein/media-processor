"""
处理管道 - 串联所有任务
"""
import os
import logging
import uuid
from typing import Optional, Dict, Any
from celery import shared_task
import requests

# 直接导入底层函数模块
from media_processor.tasks import download as download_module
from media_processor.tasks import transcribe as transcribe_module
from media_processor.tasks import translate as translate_module
from media_processor.tasks import encode as encode_module

logger = logging.getLogger(__name__)


def _do_download(self_task, url: str, task_id: str) -> dict:
    """直接执行下载（绕过 Celery）"""
    import os
    import json
    import subprocess
    import tempfile

    OUTPUT_DIR = os.getenv("OUTPUT_DIR", tempfile.gettempdir())

    logger.info(f"[{task_id}] 开始下载: {url}")

    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    output_template = os.path.join(task_dir, "video.%(ext)s")

    format_fallbacks = [
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "bestvideo+bestaudio/best",
        "best",
        None
    ]

    last_error = None
    result = None

    for fmt in format_fallbacks:
        try:
            for f in os.listdir(task_dir):
                try:
                    os.remove(os.path.join(task_dir, f))
                except:
                    pass
        except:
            pass

        try:
            cmd = [
                "yt-dlp",
                "--output", output_template,
                "--write-thumbnail",
                "--convert-thumbnails", "jpg",
                "--merge-output-format", "mp4",
                "--no-playlist",
                "--no-check-formats",
                "--extractor-retries", "3",
                "--print-json",
            ]

            if fmt:
                cmd.extend(["--format", fmt])
            cmd.append(url)

            logger.info(f"[{task_id}] 尝试格式: {fmt or 'auto'}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

            if result.returncode == 0:
                logger.info(f"[{task_id}] 格式 {fmt or 'auto'} 下载成功")
                break

            last_error = result.stderr
            logger.warning(f"[{task_id}] 格式 {fmt or 'auto'} 失败")

        except subprocess.TimeoutExpired:
            last_error = "下载超时"
            continue
        except Exception as e:
            last_error = str(e)
            continue

    if result is None or result.returncode != 0:
        raise Exception(f"yt-dlp 所有格式都失败: {last_error}")

    try:
        info = json.loads(result.stdout.strip().split('\n')[-1])
    except:
        info = {}

    video_path = None
    for ext in ["mp4", "mkv", "webm", "mov"]:
        potential_path = os.path.join(task_dir, f"video.{ext}")
        if os.path.exists(potential_path):
            video_path = potential_path
            break

    if not video_path:
        for f in os.listdir(task_dir):
            if f.endswith((".mp4", ".mkv", ".webm", ".mov")):
                video_path = os.path.join(task_dir, f)
                break

    if not video_path:
        raise Exception("下载完成但找不到视频文件")

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
    }


def _do_transcribe(self_task, video_path: str, task_id: str) -> dict:
    """直接执行转录（绕过 Celery）"""
    import whisper
    import torch

    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "turbo")

    logger.info(f"[{task_id}] 开始转录: {video_path}")

    # 使用 solo 池时可以启用 MPS
    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    logger.info(f"[{task_id}] 加载 Whisper 模型 ({WHISPER_MODEL}) 到 {device}")

    try:
        model = whisper.load_model(WHISPER_MODEL, device=device)
    except Exception as e:
        logger.warning(f"[{task_id}] {device} 加载失败，回退到 CPU: {e}")
        device = "cpu"
        model = whisper.load_model(WHISPER_MODEL, device=device)

    logger.info(f"[{task_id}] 开始转录...")
    result = model.transcribe(video_path, language=None, task="transcribe")

    detected_language = result.get("language", "unknown")

    segments = []
    for seg in result.get("segments", []):
        segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
            "language": detected_language,  # 每个 segment 都需要语言信息
        })

    logger.info(f"[{task_id}] 转录完成: {len(segments)} 片段, 语言: {detected_language}")

    return {
        "segments": segments,
        "language": detected_language,
        "text": result.get("text", ""),
    }


def _do_translate(self_task, segments: list, task_id: str, target_lang: str, context: str = "") -> list:
    """直接执行翻译（绕过 Celery）"""
    import openai

    if not segments:
        return segments

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        logger.warning(f"[{task_id}] 没有 OpenAI API Key，跳过翻译")
        return segments

    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    logger.info(f"[{task_id}] 开始翻译 {len(segments)} 个片段到 {target_lang}")

    # 批量翻译
    batch_size = 20
    translated_segments = []

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        texts = [seg["text"] for seg in batch]
        numbered_text = "\n".join([f"{j+1}. {t}" for j, t in enumerate(texts)])

        prompt = f"""翻译以下字幕到{target_lang}。保持编号格式，每行一个翻译。只输出翻译结果，不要其他内容。

{numbered_text}"""

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "你是专业的字幕翻译。翻译要自然流畅，符合目标语言习惯。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
            )

            translated_text = response.choices[0].message.content.strip()
            lines = translated_text.split("\n")

            for j, seg in enumerate(batch):
                new_seg = seg.copy()
                if j < len(lines):
                    line = lines[j].strip()
                    # 移除编号
                    if line and line[0].isdigit():
                        parts = line.split(".", 1)
                        if len(parts) > 1:
                            line = parts[1].strip()
                    new_seg["translated"] = line
                else:
                    new_seg["translated"] = seg["text"]
                translated_segments.append(new_seg)

        except Exception as e:
            logger.error(f"[{task_id}] 翻译批次失败: {e}")
            for seg in batch:
                new_seg = seg.copy()
                new_seg["translated"] = seg["text"]
                translated_segments.append(new_seg)

    logger.info(f"[{task_id}] 翻译完成")
    return translated_segments


def _do_encode(self_task, video_path: str, task_id: str, segments: list = None,
               video_bitrate: str = "500k", max_width: int = 720,
               embed_logo: bool = True, logo_base64: str = None) -> dict:
    """直接执行编码（绕过 Celery）"""
    import subprocess
    import tempfile
    import platform
    import base64

    OUTPUT_DIR = os.getenv("OUTPUT_DIR", tempfile.gettempdir())
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    output_path = os.path.join(task_dir, "output.mp4")
    subtitle_path = None

    logger.info(f"[{task_id}] 开始编码: {video_path}")

    # 生成 ASS 字幕（使用 encode 模块的完整版）
    if segments:
        subtitle_path = os.path.join(task_dir, "subtitles.ass")
        # 转换字段名：translated -> translation（encode 模块使用 translation）
        formatted_segments = []
        for seg in segments:
            formatted_seg = seg.copy()
            if "translated" in formatted_seg:
                formatted_seg["translation"] = formatted_seg.pop("translated")
            formatted_segments.append(formatted_seg)
        encode_module._generate_ass_file(formatted_segments, subtitle_path, video_path)
        logger.info(f"[{task_id}] 生成字幕: {subtitle_path}")

    # 检测编码器
    is_apple = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
    video_codec = "h264_videotoolbox" if is_apple else "libx264"

    # 获取视频尺寸用于 logo 计算
    width, height = 1280, 720
    try:
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            video_path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        if probe_result.returncode == 0 and probe_result.stdout.strip():
            parts = probe_result.stdout.strip().split(",")
            if len(parts) >= 2:
                width, height = int(parts[0]), int(parts[1])
    except:
        pass

    aspect_ratio = width / height if height > 0 else 1.78

    # 处理 Logo
    logo_path = None
    use_logo = False

    if embed_logo:
        if logo_base64:
            # 从 base64 解码 logo
            try:
                logo_data = base64.b64decode(logo_base64)
                logo_path = os.path.join(task_dir, "logo.png")
                with open(logo_path, "wb") as f:
                    f.write(logo_data)
                use_logo = True
                logger.info(f"[{task_id}] 使用客户端传入的 logo")
            except Exception as e:
                logger.warning(f"[{task_id}] 解码 logo 失败: {e}")

        # 如果没有传入 logo，尝试使用默认的
        if not use_logo:
            script_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            default_logo = os.path.join(script_dir, "assets", "logo.png")
            if os.path.exists(default_logo):
                logo_path = default_logo
                use_logo = True
                logger.info(f"[{task_id}] 使用默认 logo: {logo_path}")
            else:
                logger.warning(f"[{task_id}] 没有可用的 logo")

    # 构建 ffmpeg 命令
    cmd = ["ffmpeg", "-y", "-i", video_path]

    if use_logo:
        cmd.extend(["-i", logo_path])

    # 构建滤镜链
    filter_parts = []

    if use_logo:
        # 计算 logo 大小（视频宽度的 1/4）
        logo_target_width = int(width / 4) if aspect_ratio > 1 else int(height / 4)
        # logo 缩放 + 透明度
        filter_parts.append(f"[1]format=rgba,colorchannelmixer=aa=0.9,scale={logo_target_width}:-1[logo]")
        # 叠加 logo（右上角）
        filter_parts.append(f"[0][logo]overlay=W-w-15:15[v1]")
        video_input = "[v1]"
    else:
        video_input = "[0]"

    # 字幕滤镜
    if subtitle_path and os.path.exists(subtitle_path):
        escaped_path = subtitle_path.replace("'", "'\\''").replace(":", "\\:")
        filter_parts.append(f"{video_input}ass='{escaped_path}'[v2]")
        video_input = "[v2]"

    # 缩放滤镜
    filter_parts.append(f"{video_input}scale='min({max_width},iw)':-2")

    filter_complex = ",".join(filter_parts) if len(filter_parts) == 1 else ";".join(filter_parts[:-1]) + "," + filter_parts[-1].split("]")[-1]

    # 如果有多个滤镜需要用 filter_complex，否则用 vf
    if use_logo or (subtitle_path and os.path.exists(subtitle_path)):
        cmd.extend(["-filter_complex", filter_complex])
    else:
        cmd.extend(["-vf", f"scale='min({max_width},iw)':-2"])

    cmd.extend([
        "-c:v", video_codec,
        "-b:v", video_bitrate,
        "-c:a", "aac",
        "-b:a", "64k",
        "-movflags", "+faststart",
        output_path
    ])

    logger.info(f"[{task_id}] 编码命令: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    if result.returncode != 0:
        raise Exception(f"ffmpeg 编码失败: {result.stderr[-500:]}")

    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0

    logger.info(f"[{task_id}] 编码完成: {output_path} ({file_size} bytes)")

    return {
        "output_path": output_path,
        "subtitle_path": subtitle_path,
        "file_size": file_size,
    }


@shared_task(bind=True, name="media_processor.tasks.pipeline.process_video_pipeline")
def process_video_pipeline(
    self,
    url: str,
    options: Optional[Dict[str, Any]] = None,
    callback_url: Optional[str] = None
) -> dict:
    """
    视频处理完整管道

    Args:
        url: 视频 URL
        options: 处理选项
            - translate: bool = True  是否翻译
            - target_language: str = "zh"  目标语言
            - embed_subtitles: bool = True  是否嵌入字幕
            - video_bitrate: str = "500k"  视频码率
            - max_width: int = 720  最大宽度
        callback_url: 完成后回调 URL

    Returns:
        {
            "task_id": "xxx",
            "status": "completed",
            "output_path": "/path/to/output.mp4",
            "subtitle_path": "/path/to/subtitles.ass",
            "metadata": {...}
        }
    """
    task_id = self.request.id or str(uuid.uuid4())
    options = options or {}

    logger.info(f"[{task_id}] 开始处理管道: {url}")

    transcribe_result = {}

    try:
        # ==================== 阶段 1: 下载 ====================
        self.update_state(state="DOWNLOADING", meta={"stage": "downloading", "progress": 0})

        download_result = _do_download(self, url, task_id)

        video_path = download_result["video_path"]
        title = download_result.get("title", "")
        description = download_result.get("description", "")

        logger.info(f"[{task_id}] 下载完成: {video_path}")

        # ==================== 阶段 2: 转录 ====================
        segments = []
        if options.get("embed_subtitles", True):
            self.update_state(state="TRANSCRIBING", meta={"stage": "transcribing", "progress": 20})

            transcribe_result = _do_transcribe(self, video_path, task_id)

            segments = transcribe_result.get("segments", [])
            detected_language = transcribe_result.get("language", "unknown")

            logger.info(f"[{task_id}] 转录完成: {len(segments)} 片段, 语言: {detected_language}")

            # ==================== 阶段 3: 翻译 ====================
            if options.get("translate", True) and segments:
                self.update_state(state="TRANSLATING", meta={"stage": "translating", "progress": 50})

                target_lang = options.get("target_language", "zh")

                segments = _do_translate(self, segments, task_id, target_lang, description)

                logger.info(f"[{task_id}] 翻译完成")

        # ==================== 阶段 4: 编码 ====================
        self.update_state(state="ENCODING", meta={"stage": "encoding", "progress": 70})

        encode_result = _do_encode(
            self,
            video_path,
            task_id,
            segments=segments if options.get("embed_subtitles", True) else None,
            video_bitrate=options.get("video_bitrate", "500k"),
            max_width=options.get("max_width", 720),
            embed_logo=options.get("embed_logo", True),
            logo_base64=options.get("logo_base64"),
        )

        output_path = encode_result["output_path"]
        subtitle_path = encode_result.get("subtitle_path")

        logger.info(f"[{task_id}] 编码完成: {output_path}")

        # ==================== 完成 ====================
        result = {
            "task_id": task_id,
            "status": "completed",
            "output_path": output_path,
            "subtitle_path": subtitle_path,
            "file_size": encode_result.get("file_size", 0),
            "metadata": {
                "title": title,
                "description": description,
                "duration": download_result.get("duration", 0),
                "language": transcribe_result.get("language") if segments else None,
                "segment_count": len(segments),
            }
        }

        # 回调通知
        if callback_url:
            try:
                requests.post(callback_url, json=result, timeout=10)
                logger.info(f"[{task_id}] 回调成功: {callback_url}")
            except Exception as e:
                logger.warning(f"[{task_id}] 回调失败: {e}")

        logger.info(f"[{task_id}] 管道完成")
        return result

    except Exception as e:
        logger.error(f"[{task_id}] 管道失败: {e}")

        error_result = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e)
        }

        # 失败也回调
        if callback_url:
            try:
                requests.post(callback_url, json=error_result, timeout=10)
            except:
                pass

        raise
