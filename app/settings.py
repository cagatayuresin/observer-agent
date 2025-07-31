"""
Pydantic-based configuration for observer-agent.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    rabbitmq_url: str  # amqp://user:pass@host:5672/
    metrics_queue: str = "metrics"
    agent_id: str

    # Interval & retry
    interval: int = 30
    max_retries: int = 5
    retry_delay: int = 5
    health_check_interval: int = 60

    # Thresholds
    max_cpu_threshold: float = 95.0
    max_memory_threshold: float = 95.0
    max_disk_threshold: float = 95.0
    
    # Kubernetes settings
    kubeconfig: str | None = None     


settings = Settings()
