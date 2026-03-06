"""
处理管道 - 串联所有任务
"""
import os
import uuid
from typing import Optional, Dict, Any
from celery import shared_task
import requests

# 直接导入底层函数模块
from media_processor.tasks import download as download_module
from media_processor.tasks import transcribe as transcribe_module
from media_processor.tasks import translate as translate_module
from media_processor.tasks import encode as encode_module
from media_processor.logging import get_task_logger

logger = get_task_logger(__name__)


def _is_twitter_url(url: str) -> bool:
    """检测是否为 Twitter/X URL"""
    import re
    return bool(re.search(r'(twitter\.com|x\.com)/\w+/status/\d+', url))


def _download_twitter(url: str, task_id: str, task_dir: str) -> dict:
    """
    使用 fxtwitter API 下载 Twitter 视频

    Returns:
        与 yt-dlp 相同的 dict 结构: {video_path, title, description, duration, thumbnail_path}
    """
    import re

    logger.info(f"[{task_id}] 尝试 fixupx 下载 Twitter 视频: {url}")

    # 解析 tweet ID
    match = re.search(r'(twitter\.com|x\.com)/(\w+)/status/(\d+)', url)
    if not match:
        raise ValueError(f"无法解析 Twitter URL: {url}")

    username = match.group(2)
    tweet_id = match.group(3)

    # 调用 fxtwitter API
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    resp = requests.get(api_url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    tweet = data.get("tweet", {})
    media_list = tweet.get("media", {}).get("all", [])

    # 找到最高码率的 mp4 视频
    best_video_url = None
    best_bitrate = 0
    for media in media_list:
        if media.get("type") == "video":
            # 直接使用 url 字段（最高质量）
            best_video_url = media.get("url")
            best_bitrate = media.get("bitrate", 0)
            break

    if not best_video_url:
        raise ValueError("推文中没有找到视频")

    # 下载视频
    video_path = os.path.join(task_dir, "video.mp4")
    logger.info(f"[{task_id}] 下载视频: {best_video_url[:80]}...")
    video_resp = requests.get(best_video_url, stream=True, timeout=120)
    video_resp.raise_for_status()
    with open(video_path, "wb") as f:
        for chunk in video_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        raise ValueError("视频下载失败：文件为空")

    # 下载缩略图
    thumbnail_path = None
    thumb_url = tweet.get("media", {}).get("all", [{}])[0].get("thumbnail_url")
    if thumb_url:
        try:
            thumb_path = os.path.join(task_dir, "video.jpg")
            thumb_resp = requests.get(thumb_url, timeout=15)
            thumb_resp.raise_for_status()
            with open(thumb_path, "wb") as f:
                f.write(thumb_resp.content)
            thumbnail_path = thumb_path
        except Exception as e:
            logger.warning(f"[{task_id}] 缩略图下载失败: {e}")

    logger.info(f"[{task_id}] fixupx 下载成功: {video_path}")

    return {
        "video_path": video_path,
        "title": tweet.get("text", "")[:100],
        "description": tweet.get("text", ""),
        "duration": media_list[0].get("duration", 0) if media_list else 0,
        "thumbnail_path": thumbnail_path,
    }


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

    # Twitter/X URL 优先使用 fixupx API
    if _is_twitter_url(url):
        try:
            return _download_twitter(url, task_id, task_dir)
        except Exception as e:
            logger.warning(f"[{task_id}] fixupx 下载失败: {e}，回退 yt-dlp")

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


def _detect_language_skip_silence(model, video_path: str, task_id: str) -> str | None:
    """用 ffmpeg 跳过开头静音，然后用 Whisper 检测语言"""
    import subprocess
    import whisper

    try:
        # 1. ffmpeg silencedetect 找到静音结束点
        cmd = [
            "ffmpeg", "-i", video_path, "-af",
            "silencedetect=noise=-30dB:d=1.0",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        # 解析 silence_end 时间戳，取第一个（即开头静音结束）
        speech_start = 0.0
        for line in result.stderr.split('\n'):
            if 'silence_end' in line:
                parts = line.split('silence_end:')
                if len(parts) > 1:
                    t = float(parts[1].split('|')[0].strip())
                    if t <= 60:
                        speech_start = t
                        break

        if speech_start < 1.0:
            return None  # 没有明显的开头静音，用默认行为

        # 2. 从 speech_start 处开始加载 30s 音频做语言检测
        audio = whisper.load_audio(video_path)
        sample_start = int(speech_start * 16000)
        sample_end = sample_start + 30 * 16000
        audio_segment = audio[sample_start:sample_end]
        audio_segment = whisper.pad_or_trim(audio_segment)

        mel = whisper.log_mel_spectrogram(audio_segment).to(model.device)
        _, probs = model.detect_language(mel)
        detected = max(probs, key=probs.get)

        logger.info(f"[{task_id}] 跳过 {speech_start:.1f}s 静音后检测语言: {detected}")
        return detected

    except Exception as e:
        logger.warning(f"[{task_id}] 静音检测/语言检测失败，回退默认行为: {e}")
        return None


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

    # 跳过开头静音检测语言，避免 Whisper 用静音段误判
    detected_lang = _detect_language_skip_silence(model, video_path, task_id)
    if detected_lang:
        logger.info(f"[{task_id}] 使用检测到的语言: {detected_lang}")
    else:
        logger.info(f"[{task_id}] 使用 Whisper 默认语言检测")

    logger.info(f"[{task_id}] 开始转录...")
    result = model.transcribe(video_path, language=detected_lang, task="transcribe")

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


def _do_correct_transcript(self_task, segments: list, task_id: str, context: str = "") -> list:
    """用 LLM 校正 Whisper 转录中的听写错误（如专有名词、同音词等）"""
    import openai

    if not segments:
        return segments

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen/qwen3-30b-a3b-2507")
    LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
    LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "lm-studio")

    if not OPENAI_API_KEY and not LOCAL_LLM_BASE_URL:
        logger.warning(f"[{task_id}] 没有 LLM API Key，跳过转录校正")
        return segments

    if LOCAL_LLM_BASE_URL:
        client = openai.OpenAI(base_url=LOCAL_LLM_BASE_URL, api_key=LOCAL_LLM_API_KEY)
    else:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)

    logger.info(f"[{task_id}] 开始 LLM 转录校正: {len(segments)} 个片段, 模型: {TEXT_MODEL}")

    # 分批处理，每批最多 30 个片段
    batch_size = 30
    corrected_segments = []

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]

        # 构建带编号的文本
        numbered_lines = []
        for j, seg in enumerate(batch):
            numbered_lines.append(f"==SEGMENT_{j}==\n{seg['text'].strip()}")
        numbered_text = "\n".join(numbered_lines)

        context_hint = ""
        if context:
            context_hint = f"\n\n视频标题/描述（供参考上下文）:\n{context[:500]}"

        system_prompt = (
            "你是一名专业的语音转录校正员。下面是 Whisper 语音识别的输出文本。\n"
            "请利用全文上下文，纠正明显的语音识别错误，包括但不限于：\n"
            "- 专有名词被误听为常见词（如 YOLO 被听成 no-go，Tesla 被听成 test la）\n"
            "- 同音词/近音词误用\n"
            "- 技术术语、品牌名、人名等被错误拼写\n"
            "- 不合理的断句导致的语义错误\n\n"
            "规则：\n"
            "1. 只修正明显的识别错误，不要改写原意或润色语句\n"
            "2. 如果某段文本没有问题，原样输出即可\n"
            "3. 保留每段前的 ==SEGMENT_N== 标记，格式不变\n"
            "4. 不要添加任何解释或注释"
        )

        try:
            response = client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": numbered_text + context_hint}
                ],
                temperature=0.3,
            )

            result_text = response.choices[0].message.content.strip()

            # 解析 ==SEGMENT_N== 标记
            corrected_map = {}
            parts = result_text.split("==SEGMENT_")
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                try:
                    idx_str, corrected_text = part.split("==", 1)
                    idx = int(idx_str.strip())
                    corrected_map[idx] = corrected_text.strip()
                except Exception:
                    continue

            # 应用校正结果
            for j, seg in enumerate(batch):
                new_seg = seg.copy()
                if j in corrected_map and corrected_map[j]:
                    original = seg["text"].strip()
                    corrected = corrected_map[j]
                    if original != corrected:
                        logger.info(f"[{task_id}] 校正: '{original}' -> '{corrected}'")
                    new_seg["text"] = corrected
                corrected_segments.append(new_seg)

        except Exception as e:
            logger.error(f"[{task_id}] 转录校正批次失败: {e}")
            # 失败时保留原文
            corrected_segments.extend(batch)

    logger.info(f"[{task_id}] 转录校正完成")
    return corrected_segments


def _do_translate(self_task, segments: list, task_id: str, target_lang: str, context: str = "") -> list:
    """直接执行翻译（绕过 Celery）"""
    import openai

    if not segments:
        return segments

    # 检查源语言是否与目标语言相同，如果相同则跳过翻译
    source_lang = segments[0].get("language", "").lower() if segments else ""
    # 处理语言代码变体 (zh, zh-cn, zh-tw, chinese 都算中文)
    source_lang_normalized = "zh" if source_lang in ("zh", "zh-cn", "zh-tw", "chinese") else source_lang
    target_lang_normalized = "zh" if target_lang.lower() in ("zh", "zh-cn", "zh-tw", "chinese") else target_lang.lower()

    if source_lang_normalized == target_lang_normalized:
        logger.info(f"源语言 ({source_lang}) 与目标语言 ({target_lang}) 相同，跳过翻译")
        # 返回原始 segments，translated 字段留空
        for seg in segments:
            seg["translated"] = ""
        return segments

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    TEXT_MODEL = os.getenv("TEXT_MODEL", "qwen/qwen3-30b-a3b-2507")
    LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "")
    LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "lm-studio")

    if not OPENAI_API_KEY and not LOCAL_LLM_BASE_URL:
        logger.warning(f"没有 OpenAI API Key，跳过翻译")
        return segments

    if LOCAL_LLM_BASE_URL:
        client = openai.OpenAI(base_url=LOCAL_LLM_BASE_URL, api_key=LOCAL_LLM_API_KEY)
    else:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)

    logger.info(f"开始翻译 {len(segments)} 个片段到 {target_lang}，使用模型: {TEXT_MODEL}")

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
                model=TEXT_MODEL,
                messages=[
                    {"role": "system", "content": "你是专业的字幕翻译。翻译要自然流畅，符合目标语言习惯。"},
                    {"role": "user", "content": prompt}
                ],
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

    # 检测是否使用复杂滤镜（logo 或字幕）
    has_complex_filters = embed_logo or (segments and len(segments) > 0)

    # 检测编码器
    # 注意：h264_videotoolbox 与复杂 filter_complex 链组合时会产生损坏的视频
    # 当使用 logo 或字幕时，强制使用 libx264
    is_apple = platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")
    if has_complex_filters:
        video_codec = "libx264"
        logger.info(f"[{task_id}] 使用 libx264（复杂滤镜链与 videotoolbox 不兼容）")
    else:
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
    video_label = "0:v"

    if use_logo:
        # 计算 logo 大小（视频宽度的 1/4）
        logo_target_width = int(width / 4) if aspect_ratio > 1 else int(height / 4)
        # logo 缩放 + 透明度
        filter_parts.append(f"[1:v]format=rgba,colorchannelmixer=aa=0.9,scale={logo_target_width}:-1[logo]")
        # 叠加 logo（右上角）
        filter_parts.append(f"[{video_label}][logo]overlay=W-w-15:15[v1]")
        video_label = "v1"

    # 字幕滤镜
    if subtitle_path and os.path.exists(subtitle_path):
        escaped_path = subtitle_path.replace("'", "'\\''").replace(":", "\\:")
        filter_parts.append(f"[{video_label}]ass='{escaped_path}'[v2]")
        video_label = "v2"

    # 缩放滤镜（添加输出标签用于 map）
    filter_parts.append(f"[{video_label}]scale='min({max_width},iw)':-2[vout]")

    # 用分号连接所有滤镜
    filter_complex = ";".join(filter_parts)

    # 如果有多个滤镜需要用 filter_complex，否则用 vf
    if use_logo or (subtitle_path and os.path.exists(subtitle_path)):
        cmd.extend(["-filter_complex", filter_complex])
        # 显式映射：视频用滤镜输出，音频用原始输入
        cmd.extend(["-map", "[vout]", "-map", "0:a"])
    else:
        cmd.extend(["-vf", f"scale='min({max_width},iw)':-2"])

    cmd.extend(["-c:v", video_codec])

    # libx264 需要额外参数
    if video_codec == "libx264":
        cmd.extend(["-preset", "medium", "-crf", "23",
                     "-maxrate", video_bitrate, "-bufsize", str(int(video_bitrate.replace("k", "")) * 2) + "k"])
    else:
        cmd.extend(["-b:v", video_bitrate])

    cmd.extend([
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

    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("initializing")
    logger.info(f"开始处理管道: {url}")

    transcribe_result = {}

    try:
        # ==================== 阶段 1: 下载 ====================
        logger.set_stage("downloading")
        self.update_state(state="DOWNLOADING", meta={"stage": "downloading", "progress": 0})

        download_result = _do_download(self, url, task_id)

        video_path = download_result["video_path"]
        title = download_result.get("title", "")
        description = download_result.get("description", "")

        logger.info(f"下载完成: {video_path}")

        # ==================== 阶段 2: 转录 ====================
        segments = []
        if options.get("embed_subtitles", True):
            logger.set_stage("transcribing")
            self.update_state(state="TRANSCRIBING", meta={"stage": "transcribing", "progress": 20})

            transcribe_result = _do_transcribe(self, video_path, task_id)

            segments = transcribe_result.get("segments", [])
            detected_language = transcribe_result.get("language", "unknown")

            logger.info(f"转录完成: {len(segments)} 片段, 语言: {detected_language}")

            # ==================== 阶段 2.5: 转录校正 ====================
            if segments:
                logger.set_stage("correcting")
                self.update_state(state="CORRECTING", meta={"stage": "correcting", "progress": 35})

                context_for_correction = f"{title}\n{description}".strip()
                segments = _do_correct_transcript(self, segments, task_id, context_for_correction)

                logger.info(f"转录校正完成")

            # ==================== 阶段 3: 翻译 ====================
            if options.get("translate", True) and segments:
                logger.set_stage("translating")
                self.update_state(state="TRANSLATING", meta={"stage": "translating", "progress": 50})

                target_lang = options.get("target_language", "zh")

                segments = _do_translate(self, segments, task_id, target_lang, description)

                logger.info(f"翻译完成")

        # ==================== 阶段 4: 编码 ====================
        logger.set_stage("encoding")
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

        logger.info(f"编码完成: {output_path}")

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
                logger.info(f"回调成功: {callback_url}")
            except Exception as e:
                logger.warning(f"回调失败: {e}")

        logger.set_stage("completed")
        logger.info(f"管道完成")
        logger.clear()
        return result

    except Exception as e:
        logger.set_stage("failed")
        logger.error(f"管道失败: {e}")

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


@shared_task(bind=True, name="media_processor.tasks.pipeline.process_file_pipeline")
def process_file_pipeline(
    self,
    file_path: str,
    options: Optional[Dict[str, Any]] = None,
    callback_url: Optional[str] = None
) -> dict:
    """
    文件处理管道（跳过下载步骤）

    Args:
        file_path: 本地视频文件路径
        options: 处理选项
        callback_url: 完成后回调 URL

    Returns:
        同 process_video_pipeline
    """
    task_id = self.request.id or str(uuid.uuid4())
    options = options or {}

    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("initializing")
    logger.info(f"开始处理文件: {file_path}")

    transcribe_result = {}

    try:
        # ==================== 阶段 1: 转录 ====================
        segments = []
        if options.get("embed_subtitles", True):
            logger.set_stage("transcribing")
            self.update_state(state="TRANSCRIBING", meta={"stage": "transcribing", "progress": 10})

            transcribe_result = _do_transcribe(self, file_path, task_id)

            segments = transcribe_result.get("segments", [])
            detected_language = transcribe_result.get("language", "unknown")

            logger.info(f"转录完成: {len(segments)} 片段, 语言: {detected_language}")

            # ==================== 阶段 1.5: 转录校正 ====================
            if segments:
                logger.set_stage("correcting")
                self.update_state(state="CORRECTING", meta={"stage": "correcting", "progress": 25})

                segments = _do_correct_transcript(self, segments, task_id)

                logger.info(f"转录校正完成")

            # ==================== 阶段 2: 翻译 ====================
            if options.get("translate", True) and segments:
                logger.set_stage("translating")
                self.update_state(state="TRANSLATING", meta={"stage": "translating", "progress": 40})

                target_lang = options.get("target_language", "zh")

                segments = _do_translate(self, segments, task_id, target_lang, "")

                logger.info(f"翻译完成")

        # ==================== 阶段 3: 编码 ====================
        logger.set_stage("encoding")
        self.update_state(state="ENCODING", meta={"stage": "encoding", "progress": 70})

        encode_result = _do_encode(
            self,
            file_path,
            task_id,
            segments=segments if options.get("embed_subtitles", True) else None,
            video_bitrate=options.get("video_bitrate", "500k"),
            max_width=options.get("max_width", 720),
            embed_logo=options.get("embed_logo", True),
            logo_base64=options.get("logo_base64"),
        )

        output_path = encode_result["output_path"]
        subtitle_path = encode_result.get("subtitle_path")

        logger.info(f"编码完成: {output_path}")

        # ==================== 完成 ====================
        result = {
            "task_id": task_id,
            "status": "completed",
            "output_path": output_path,
            "subtitle_path": subtitle_path,
            "file_size": encode_result.get("file_size", 0),
            "metadata": {
                "language": transcribe_result.get("language") if segments else None,
                "segment_count": len(segments),
            }
        }

        # 回调通知
        if callback_url:
            try:
                requests.post(callback_url, json=result, timeout=10)
                logger.info(f"回调成功: {callback_url}")
            except Exception as e:
                logger.warning(f"回调失败: {e}")

        logger.set_stage("completed")
        logger.info(f"文件处理完成")
        logger.clear()
        return result

    except Exception as e:
        logger.set_stage("failed")
        logger.error(f"文件处理失败: {e}")

        error_result = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e)
        }

        if callback_url:
            try:
                requests.post(callback_url, json=error_result, timeout=10)
            except:
                pass

        raise
