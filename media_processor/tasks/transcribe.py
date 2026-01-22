"""
转录任务 - 使用 Whisper 进行语音识别
"""
import os
import platform
from typing import Optional, List, Dict
from celery import shared_task

from media_processor.logging import get_task_logger

logger = get_task_logger(__name__)

# Whisper 模型配置
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "turbo")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")

# 全局模型实例 (Worker 进程内复用)
_whisper_model = None


def get_whisper_model():
    """获取或加载 Whisper 模型 (Worker 进程内单例)"""
    global _whisper_model

    if _whisper_model is None:
        import torch
        import whisper

        # 自动选择设备
        if WHISPER_DEVICE == "auto":
            if torch.backends.mps.is_available():
                device = "mps"
            elif torch.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        else:
            device = WHISPER_DEVICE

        logger.info(f"加载 Whisper 模型: {WHISPER_MODEL} (device: {device})")

        # MPS 兼容的模型列表
        mps_compatible = ["tiny", "base", "turbo", "large-v2"]

        try:
            _whisper_model = whisper.load_model(WHISPER_MODEL, device=device)
        except (NotImplementedError, RuntimeError) as e:
            if device == "mps":
                logger.warning(f"MPS 加载失败，回退到 CPU: {e}")
                _whisper_model = whisper.load_model(WHISPER_MODEL, device="cpu")
            else:
                raise

        logger.info(f"Whisper 模型已加载 (device: {next(_whisper_model.parameters()).device})")

    return _whisper_model


@shared_task(bind=True, name="media_processor.tasks.transcribe.transcribe_audio")
def transcribe_audio(
    self,
    video_path: str,
    task_id: str,
    language: Optional[str] = None
) -> dict:
    """
    转录音频任务

    Args:
        video_path: 视频文件路径
        task_id: 任务 ID
        language: 指定语言 (可选，None 则自动检测)

    Returns:
        {
            "segments": [
                {"start": 0.0, "end": 2.5, "text": "Hello", "language": "en"},
                ...
            ],
            "language": "en",
            "duration": 180
        }
    """
    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("transcribing")
    logger.info(f"开始转录: {video_path}")

    self.update_state(state="TRANSCRIBING", meta={"progress": 0, "stage": "transcribing"})

    try:
        model = get_whisper_model()

        # 执行转录
        result = model.transcribe(
            video_path,
            task="transcribe",
            language=language,
            verbose=False
        )

        # 提取分段信息
        segments = []
        for seg in result.get("segments", []):
            segments.append({
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"].strip(),
                "language": result.get("language", "unknown")
            })

        logger.info(f"转录完成: {len(segments)} 个片段")

        return {
            "segments": segments,
            "language": result.get("language", "unknown"),
            "duration": segments[-1]["end"] if segments else 0
        }

    except Exception as e:
        logger.error(f"转录失败: {e}")
        raise
    finally:
        logger.clear()


@shared_task(bind=True, name="media_processor.tasks.transcribe.detect_language")
def detect_language(
    self,
    video_path: str,
    task_id: str,
    sample_duration: float = 30.0
) -> str:
    """
    检测视频语言

    Args:
        video_path: 视频文件路径
        task_id: 任务 ID
        sample_duration: 采样时长 (秒)

    Returns:
        语言代码 (如 "en", "zh", "ja")
    """
    # Set task context for structured logging
    logger.set_task(task_id)
    logger.set_stage("language_detection")
    logger.info(f"检测语言: {video_path}")

    try:
        import whisper

        model = get_whisper_model()

        # 加载音频并只取前 N 秒
        audio = whisper.load_audio(video_path)
        audio = whisper.pad_or_trim(audio, int(sample_duration * 16000))

        # 生成梅尔频谱图
        mel = whisper.log_mel_spectrogram(audio).to(model.device)

        # 检测语言
        _, probs = model.detect_language(mel)
        detected_lang = max(probs, key=probs.get)

        logger.info(f"检测到语言: {detected_lang}")
        return detected_lang

    except Exception as e:
        logger.error(f"语言检测失败: {e}")
        return "unknown"
    finally:
        logger.clear()
