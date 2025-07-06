from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

    rabbitmq_url: str        # amqp://…
    metrics_queue: str = "metrics"
    agent_id: str
    interval: int = 30       # saniye
    
    # Yeni ayarlar
    max_retries: int = 5
    retry_delay: int = 5     # saniye
    health_check_interval: int = 60  # saniye
    max_cpu_threshold: float = 95.0
    max_memory_threshold: float = 95.0
    max_disk_threshold: float = 95.0

settings = Settings()
