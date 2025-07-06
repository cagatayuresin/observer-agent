# app/agent.py

import asyncio
import time
import json
import subprocess
import signal
from pathlib import Path
from collections import Counter
from typing import Optional, Dict, Any

import psutil
from aio_pika import connect_robust, Message, Connection, Channel
from aio_pika.exceptions import AMQPConnectionError

from .settings import settings
from .logger import logger

# Proje kökündeki VERSION dosyasından agent versiyonunu oku
VERSION = Path(__file__).parent.parent.joinpath("VERSION").read_text().strip()

# Global durumlar
shutdown_event = asyncio.Event()
connection: Optional[Connection] = None
channel: Optional[Channel] = None
health_status = {"status": "starting", "last_successful_publish": None}

def validate_metrics(data: Dict[str, Any]) -> bool:
    """Metrics verilerini validate et ve threshold kontrolü yap"""
    try:
        # Kritik threshold kontrolleri
        if data.get("cpu_percent", 0) > settings.max_cpu_threshold:
            logger.warning(f"CPU kullanımı yüksek: {data['cpu_percent']:.2f}%", extra={
                "agent_id": settings.agent_id,
                "threshold": settings.max_cpu_threshold
            })
        
        if data.get("memory_percent", 0) > settings.max_memory_threshold:
            logger.warning(f"Bellek kullanımı yüksek: {data['memory_percent']:.2f}%", extra={
                "agent_id": settings.agent_id,
                "threshold": settings.max_memory_threshold
            })
        
        if data.get("disk_percent", 0) > settings.max_disk_threshold:
            logger.warning(f"Disk kullanımı yüksek: {data['disk_percent']:.2f}%", extra={
                "agent_id": settings.agent_id,
                "threshold": settings.max_disk_threshold
            })
        
        # Veri bütünlüğü kontrolleri
        required_fields = ["agent_id", "timestamp", "cpu_percent", "memory_percent", "disk_percent"]
        for field in required_fields:
            if field not in data:
                logger.error(f"Eksik alan: {field}", extra={"agent_id": settings.agent_id})
                return False
        
        return True
    except Exception as e:
        logger.error(f"Metrics validation hatası: {e}", extra={"agent_id": settings.agent_id})
        return False

def collect_metrics():
    ts = time.time()
    
    try:
        cpu = psutil.cpu_percent(interval=1)  # 1 saniye bekleyerek daha doğru ölçüm
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        
        # Sistem bilgileri
        boot_time = psutil.boot_time()
        uptime = time.time() - boot_time
        
        # Load average (Unix sistemlerde)
        load_avg = None
        try:
            load_avg = psutil.getloadavg()
        except (AttributeError, OSError):
            pass  # Windows'ta desteklenmiyor
        
    except Exception as e:
        logger.error(f"Sistem metrikleri alınamadı: {e}", extra={"agent_id": settings.agent_id})
        # Varsayılan değerler
        cpu = mem = disk = 0
        uptime = 0
        load_avg = None

    # Kubernetes pod metrikleri (kubectl config varsa)
    pods = []
    k8s_available = False
    try:
        raw = subprocess.check_output(
            ["kubectl", "get", "pods", "--all-namespaces", "-o", "json"],
            stderr=subprocess.DEVNULL,
            timeout=10  # 10 saniye timeout
        )
        js = json.loads(raw)
        k8s_available = True
        for item in js.get("items", []):
            pod_info = {
                "ns": item["metadata"]["namespace"],
                "name": item["metadata"]["name"],
                "status": item["status"].get("phase"),
                "ready": item["status"].get("containerStatuses", [{}])[0].get("ready", False) if item["status"].get("containerStatuses") else False,
                "restarts": sum(cs.get("restartCount", 0) for cs in item["status"].get("containerStatuses", [])),
                "created": item["metadata"].get("creationTimestamp")
            }
            pods.append(pod_info)
    except subprocess.TimeoutExpired:
        logger.warning("kubectl komutu zaman aşımına uğradı", extra={"agent_id": settings.agent_id})
    except Exception as e:
        logger.debug(f"kubectl pods alınamadı: {e}", extra={"agent_id": settings.agent_id})

    # Pod statü özetleri
    statuses = [p["status"] for p in pods]
    status_counts = Counter(statuses)

    data = {
        "agent_id": settings.agent_id,
        "agent_version": VERSION,
        "timestamp": ts,
        "cpu_percent": cpu,
        "memory_percent": mem,
        "disk_percent": disk,
        "uptime_seconds": uptime,
        "load_average": load_avg,
        "kubernetes_available": k8s_available,
        "pod_count": len(pods),
        "pod_status_counts": dict(status_counts),
        "pods": pods,
        "health_status": health_status["status"]
    }
    
    # Metrics validation
    if not validate_metrics(data):
        logger.error("Metrics validation başarısız", extra={"agent_id": settings.agent_id})
        return None
    
    return data

async def setup_connection():
    """RabbitMQ bağlantısını kur"""
    global connection, channel
    
    for attempt in range(settings.max_retries):
        try:
            logger.info(f"RabbitMQ bağlantısı kuruluyor (deneme {attempt + 1}/{settings.max_retries})", extra={
                "agent_id": settings.agent_id,
                "agent_version": VERSION
            })
            
            connection = await connect_robust(str(settings.rabbitmq_url))
            channel = await connection.channel()
            await channel.declare_queue(settings.metrics_queue, durable=True)
            
            health_status["status"] = "connected"
            logger.info("RabbitMQ bağlantısı başarılı", extra={
                "agent_id": settings.agent_id,
                "agent_version": VERSION
            })
            return True
            
        except AMQPConnectionError as e:
            logger.error(f"RabbitMQ bağlantı hatası (deneme {attempt + 1}): {e}", extra={
                "agent_id": settings.agent_id,
                "attempt": attempt + 1,
                "max_retries": settings.max_retries
            })
            
            if attempt < settings.max_retries - 1:
                await asyncio.sleep(settings.retry_delay)
            else:
                health_status["status"] = "connection_failed"
                return False
        except Exception as e:
            logger.error(f"Beklenmeyen bağlantı hatası: {e}", extra={
                "agent_id": settings.agent_id,
                "attempt": attempt + 1
            })
            await asyncio.sleep(settings.retry_delay)
    
    return False

async def publish_metrics(data: Dict[str, Any]) -> bool:
    """Metrics verisini RabbitMQ'ya gönder"""
    global channel
    
    if not channel or channel.is_closed:
        logger.warning("Kanal bağlantısı kesildi, yeniden bağlanılıyor", extra={
            "agent_id": settings.agent_id
        })
        if not await setup_connection():
            return False
    
    try:
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        msg = Message(body, delivery_mode=2)
        await channel.default_exchange.publish(
            msg, routing_key=settings.metrics_queue
        )
        
        health_status["last_successful_publish"] = time.time()
        logger.info("Metrics gönderildi", extra={
            "agent_id": settings.agent_id,
            "timestamp": data.get("timestamp"),
            "data_size": len(body)
        })
        return True
        
    except Exception as e:
        logger.error(f"Metrics gönderme hatası: {e}", extra={
            "agent_id": settings.agent_id,
            "error": str(e)
        })
        health_status["status"] = "publish_failed"
        return False

async def health_monitor():
    """Sağlık durumunu izle"""
    while not shutdown_event.is_set():
        try:
            current_time = time.time()
            last_publish = health_status.get("last_successful_publish")
            
            if last_publish and (current_time - last_publish) > settings.health_check_interval * 2:
                logger.warning("Uzun süredir başarılı publish yapılmadı", extra={
                    "agent_id": settings.agent_id,
                    "last_publish_ago": current_time - last_publish
                })
                health_status["status"] = "unhealthy"
            
            # Sistem kaynak kontrolü
            cpu_percent = psutil.cpu_percent()
            memory_percent = psutil.virtual_memory().percent
            
            if cpu_percent > settings.max_cpu_threshold or memory_percent > settings.max_memory_threshold:
                logger.warning("Sistem kaynakları kritik seviyede", extra={
                    "agent_id": settings.agent_id,
                    "cpu_percent": cpu_percent,
                    "memory_percent": memory_percent
                })
            
            await asyncio.sleep(settings.health_check_interval)
            
        except Exception as e:
            logger.error(f"Sağlık izleme hatası: {e}", extra={"agent_id": settings.agent_id})
            await asyncio.sleep(settings.health_check_interval)

async def graceful_shutdown():
    """Temiz kapanış"""
    logger.info("Graceful shutdown başlatılıyor", extra={
        "agent_id": settings.agent_id,
        "agent_version": VERSION
    })
    
    shutdown_event.set()
    
    if channel and not channel.is_closed:
        await channel.close()
    
    if connection and not connection.is_closed:
        await connection.close()
    
    logger.info("Agent temiz bir şekilde kapatıldı", extra={
        "agent_id": settings.agent_id,
        "agent_version": VERSION
    })

def signal_handler(signum, frame):
    """Signal handler"""
    logger.info(f"Signal alındı: {signum}", extra={
        "agent_id": settings.agent_id
    })
    asyncio.create_task(graceful_shutdown())

async def publish_loop():
    """Ana publish döngüsü"""
    health_status["status"] = "starting"
    
    # İlk bağlantıyı kur
    if not await setup_connection():
        logger.error("RabbitMQ bağlantısı kurulamadı, agent durduruluyor", extra={
            "agent_id": settings.agent_id
        })
        return
    
    # Sağlık izleme görevini başlat
    health_task = asyncio.create_task(health_monitor())
    
    consecutive_failures = 0
    max_consecutive_failures = 5
    
    try:
        while not shutdown_event.is_set():
            try:
                # Metrics topla
                data = collect_metrics()
                if data is None:
                    logger.error("Metrics toplanamadı, döngü atlanıyor", extra={
                        "agent_id": settings.agent_id
                    })
                    await asyncio.sleep(settings.interval)
                    continue
                
                # Metrics gönder
                if await publish_metrics(data):
                    consecutive_failures = 0
                    health_status["status"] = "healthy"
                else:
                    consecutive_failures += 1
                    logger.warning(f"Metrics gönderilemedi (ardışık hata: {consecutive_failures})", extra={
                        "agent_id": settings.agent_id,
                        "consecutive_failures": consecutive_failures
                    })
                
                # Çok fazla ardışık hata varsa bağlantıyı yeniden kur
                if consecutive_failures >= max_consecutive_failures:
                    logger.error("Çok fazla ardışık hata, bağlantı yeniden kuruluyor", extra={
                        "agent_id": settings.agent_id,
                        "consecutive_failures": consecutive_failures
                    })
                    await setup_connection()
                    consecutive_failures = 0
                
                # Interval bekle
                await asyncio.sleep(settings.interval)
                
            except Exception as e:
                logger.error(f"Publish loop hatası: {e}", exc_info=True, extra={
                    "agent_id": settings.agent_id
                })
                await asyncio.sleep(settings.interval)
                
    except asyncio.CancelledError:
        logger.info("Publish loop iptal edildi", extra={
            "agent_id": settings.agent_id
        })
    finally:
        # Sağlık izleme görevini iptal et
        health_task.cancel()
        try:
            await health_task
        except asyncio.CancelledError:
            pass

async def main():
    """Ana fonksiyon"""
    # Signal handler'ları ayarla
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Agent başlatılıyor", extra={
        "agent_id": settings.agent_id,
        "agent_version": VERSION,
        "settings": {
            "interval": settings.interval,
            "max_retries": settings.max_retries,
            "retry_delay": settings.retry_delay,
            "health_check_interval": settings.health_check_interval
        }
    })
    
    try:
        await publish_loop()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt alındı", extra={
            "agent_id": settings.agent_id
        })
    except Exception as e:
        logger.error("Agent kritik hata ile durduruldu", exc_info=True, extra={
            "agent_id": settings.agent_id,
            "agent_version": VERSION
        })
    finally:
        await graceful_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent durduruldu")
    except Exception as e:
        logger.error("Agent hata ile durduruldu", exc_info=e, extra={
            "agent_id": settings.agent_id,
            "agent_version": VERSION
        })
