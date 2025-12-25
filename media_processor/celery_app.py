"""
Celery 应用配置
"""
import os
from celery import Celery
from kombu import Queue

# Redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# 创建 Celery 应用
celery_app = Celery(
    "media_processor",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "media_processor.tasks.download",
        "media_processor.tasks.transcribe",
        "media_processor.tasks.translate",
        "media_processor.tasks.encode",
    ]
)

# Celery 配置
celery_app.conf.update(
    # 任务序列化
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,

    # 任务结果过期时间 (24小时)
    result_expires=86400,

    # 任务确认机制 (任务完成后才确认)
    task_acks_late=True,

    # Worker 预取数量 (避免 GPU 任务积压)
    worker_prefetch_multiplier=1,

    # 任务队列定义
    task_queues=(
        Queue("download", routing_key="download"),
        Queue("transcribe", routing_key="transcribe"),
        Queue("translate", routing_key="translate"),
        Queue("encode", routing_key="encode"),
        Queue("default", routing_key="default"),
    ),

    # 默认队列
    task_default_queue="default",

    # 任务路由
    task_routes={
        "media_processor.tasks.download.*": {"queue": "download"},
        "media_processor.tasks.transcribe.*": {"queue": "transcribe"},
        "media_processor.tasks.translate.*": {"queue": "translate"},
        "media_processor.tasks.encode.*": {"queue": "encode"},
    },

    # 任务重试配置
    task_annotations={
        "*": {
            "max_retries": 3,
            "default_retry_delay": 60,
        }
    },
)
