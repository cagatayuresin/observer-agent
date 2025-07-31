"""
observer-agent  –  rules-aware edition  (v2.0.0)

• Host + pod metriklerini toplamaya devam eder (Influx için).
• RabbitMQ 'rules' (fan-out) exchange'inden kural setini alır, bellekte tutar.
• Her döngüde CrashLoopBackOff / OOMKilled vb. olayları kurallarla eşleştirip
  delete_pod veya scale_deployment gibi aksiyonlar uygular.

Basit, bloklayıcı olmayan tek dosyalık MVP – ileri işlemler (rate-limit, imza,
retry, vs.) eklenebilir.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import subprocess
import time
from collections import Counter
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import yaml
from aio_pika import Message, connect_robust
from aio_pika.channel import Channel
from aio_pika.exceptions import AMQPConnectionError
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection as Connection
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from app.logger import logger
from app.settings import settings

# -----------------------------------------------------------------------------
# GLOBALS
# -----------------------------------------------------------------------------
VERSION = Path(__file__).parent.parent.joinpath("VERSION").read_text().strip()
shutdown_event = asyncio.Event()
connection: Optional[Connection] = None
channel: Optional[Channel] = None

# Bellekte kurallar
RULES: Dict[str, Any] = {"version": 0, "rules": []}
health_status = {"status": "starting", "last_successful_publish": None}

# -----------------------------------------------------------------------------
# K8s yardımcıları
# -----------------------------------------------------------------------------
def load_k8s_config():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

def k8s_core() -> k8s_client.CoreV1Api:
    load_k8s_config()
    return k8s_client.CoreV1Api()

def delete_pod(namespace: str, name: str) -> None:
    try:
        k8s_core().delete_namespaced_pod(name=name, namespace=namespace)
        logger.info(
            "Action delete_pod executed",
            extra={"agent_id": settings.agent_id, "ns": namespace, "name": name},
        )
    except ApiException as e:
        if e.status != 404:
            logger.error(
                "delete_pod error",
                extra={
                    "agent_id": settings.agent_id,
                    "ns": namespace,
                    "name": name,
                    "status": e.status,
                },
            )

def scale_deployment(namespace: str, deployment: str, replicas: int) -> None:
    try:
        apps = k8s_client.AppsV1Api()
        body = {"spec": {"replicas": replicas}}
        apps.patch_namespaced_deployment_scale(deployment, namespace, body)
        logger.info(
            "Action scale_deployment executed",
            extra={
                "agent_id": settings.agent_id,
                "ns": namespace,
                "dep": deployment,
                "replicas": replicas,
            },
        )
    except ApiException as e:
        logger.error(
            "scale_deployment error",
            extra={
                "agent_id": settings.agent_id,
                "ns": namespace,
                "dep": deployment,
                "status": e.status,
            },
        )

# -----------------------------------------------------------------------------
# RULE ENGINE
# -----------------------------------------------------------------------------
def matches_rule(pod: Dict[str, Any], rule: Dict[str, Any], reason: str) -> bool:
    m = rule["match"]
    # namespace
    if "namespace" in m and m["namespace"] != pod["ns"]:
        return False
    # labels
    if "labels" in m:
        pod_labels = {**pod.get("labels", {})}
        if not all(pod_labels.get(k) == v for k, v in m["labels"].items()):
            return False
    # reason
    return m.get("reason") == reason

def evaluate_pod_events(pods: List[Dict[str, Any]]) -> None:
    """
    CrashLoopBackOff / OOMKilled gibi durumları tespit edip RULES'e göre aksiyon uygular.
    """
    for pod in pods:
        reason = pod.get("status_reason")  # CrashLoopBackOff, OOMKilled vs.
        if not reason:
            continue
        for rule in RULES["rules"]:
            if matches_rule(pod, rule, reason):
                action = rule["action"]
                if action["type"] == "delete_pod":
                    delete_pod(pod["ns"], pod["name"])
                elif action["type"] == "scale_deployment":
                    scale_deployment(
                        pod["ns"],
                        action.get("deployment") or pod["owner"],  # owner dep adı
                        action["replicas"],
                    )

# -----------------------------------------------------------------------------
# METRIC & EVENT GATHERING (kısaltılmış – önceki sürümle aynı mantık + reason)
# -----------------------------------------------------------------------------
def collect_metrics() -> Optional[Dict[str, Any]]:
    ts = time.time()

    # host metrics (kısa)
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    uptime = ts - psutil.boot_time()

    # k8s pod listesi
    pods, status_counts = [], Counter()
    k8s_available = False
    try:
        raw = subprocess.check_output(
            ["kubectl", "get", "pods", "-A", "-o", "json"], timeout=10
        )
        k8s_available = True
        js = json.loads(raw)
        for item in js["items"]:
            status = item["status"].get("phase")
            reason = item["status"].get("reason") or item["status"].get(
                "containerStatuses", [{}]
            )[0].get("state", {}).get("waiting", {}).get("reason")
            pods.append(
                {
                    "ns": item["metadata"]["namespace"],
                    "name": item["metadata"]["name"],
                    "status_reason": reason,
                    "status": status,
                    "labels": item["metadata"].get("labels", {}),
                    "owner": item["metadata"]["ownerReferences"][0]["name"]
                    if item["metadata"].get("ownerReferences")
                    else "",
                }
            )
            status_counts[status] += 1
    except Exception:
        pass  # host-only mod

    # rule evaluation:
    evaluate_pod_events(pods)

    return {
        "agent_id": settings.agent_id,
        "timestamp": ts,
        "cpu_percent": cpu,
        "memory_percent": mem,
        "disk_percent": disk,
        "kubernetes_available": k8s_available,
        "pod_count": len(pods),
        "pod_status_counts": dict(status_counts),
        "health_status": health_status["status"],
    }

# -----------------------------------------------------------------------------
# RABBITMQ İLETİŞİMİ
# -----------------------------------------------------------------------------
async def on_rules_message(msg: AbstractIncomingMessage) -> None:
    global RULES
    async with msg.process():
        try:
            payload = json.loads(msg.body.decode())
            if payload["version"] != RULES["version"]:
                RULES = payload
                logger.info(
                    "rules update received",
                    extra={"agent_id": settings.agent_id, "version": RULES["version"]},
                )
        except Exception as e:
            logger.error(
                "rules message error", extra={"agent_id": settings.agent_id, "err": str(e)}
            )

async def setup_connection() -> bool:
    """RabbitMQ connection + rules subscription."""
    global connection, channel
    for _ in range(settings.max_retries):
        try:
            connection = await connect_robust(str(settings.rabbitmq_url))
            channel = await connection.channel()
            await channel.declare_queue(settings.metrics_queue, durable=True)

            # rules fan-out queue
            exchange = await channel.declare_exchange("rules", "fanout")
            rules_q = await channel.declare_queue(exclusive=True, auto_delete=True)
            await rules_q.bind(exchange)
            await rules_q.consume(on_rules_message)

            health_status["status"] = "connected"
            logger.info("RabbitMQ connected", extra={"agent_id": settings.agent_id})
            return True
        except AMQPConnectionError:
            await asyncio.sleep(settings.retry_delay)
    health_status["status"] = "connection_failed"
    return False

# -----------------------------------------------------------------------------
# METRIC PUBLISH (değişmedi – kısaltılmış)
# -----------------------------------------------------------------------------
async def publish_metrics(data: Dict[str, Any]) -> bool:
    global channel
    if not channel or channel.is_closed:
        if not await setup_connection():
            return False
    try:
        body = json.dumps(data, ensure_ascii=False).encode()
        await channel.default_exchange.publish(
            Message(body, delivery_mode=2), routing_key=settings.metrics_queue
        )
        health_status["last_successful_publish"] = time.time()
        return True
    except Exception:
        health_status["status"] = "publish_failed"
        return False

# -----------------------------------------------------------------------------
# LOOP & SHUTDOWN  (kısaltılmış – önceki sürümle aynı mantık)
# -----------------------------------------------------------------------------
async def publish_loop() -> None:
    if not await setup_connection():
        logger.error("cannot connect to RabbitMQ")
        return
    while not shutdown_event.is_set():
        data = collect_metrics()
        if data:
            await publish_metrics(data)
        await asyncio.sleep(settings.interval)

async def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
    await publish_loop()

if __name__ == "__main__":
    asyncio.run(main())
