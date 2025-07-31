"""
observer-agent – rules-aware edition  (v2.1.0)

• Host + pod metrikleri (CPU, bellek, disk)
• Pod bazlı kaynak kullanımı (millicore / byte + %)
• RabbitMQ üzerinden metrik publish
• RabbitMQ 'rules' exchange’inden kural seti alır, bellekte tutar
• CrashLoopBackOff / OOMKilled vb. olayları kurallarla eşleştirip delete_pod /
  scale_deployment aksiyonlarını uygular
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from aio_pika import Message, connect_robust
from aio_pika.channel import Channel
from aio_pika.exceptions import AMQPConnectionError
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection as Connection
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from app.logger import logger
from app.settings import settings

# ----------------------------------------------------------------------------- #
VERSION = "2.1.0"
shutdown_event = asyncio.Event()
connection: Optional[Connection] = None
channel: Optional[Channel] = None

# Bellekte tutulan kurallar
RULES: Dict[str, Any] = {"version": 0, "rules": []}
health_status = {"status": "starting", "last_successful_publish": None}

# ----------------------------------------------------------------------------- #
# Kubernetes yardımcı fonksiyonları                                             #
# ----------------------------------------------------------------------------- #
def load_k8s_config():
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

def k8s_core() -> k8s_client.CoreV1Api:
    load_k8s_config()
    return k8s_client.CoreV1Api()

# ----------------------------------------------------------------------------- #
# Pod resource helpers – kubectl top                                            #
# ----------------------------------------------------------------------------- #
_CPU_TOTAL_MILLICORE = psutil.cpu_count(logical=True) * 1000
_MEM_TOTAL_BYTES = psutil.virtual_memory().total

def _parse_cpu(raw: str) -> int:
    """'25m'→25,  '2'→2000 (millicore)."""
    raw = raw.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1])
    return int(float(raw) * 1000)

def _parse_mem(raw: str) -> int:
    """'128Mi'→134217728, '1Gi'→1073741824 (byte)."""
    raw = raw.strip()
    if raw[-2:].lower() in {"ki", "mi", "gi"}:
        num = float(raw[:-2])
        unit = raw[-2:].lower()
        mult = {"ki": 1024, "mi": 1024**2, "gi": 1024**3}[unit]
        return int(num * mult)
    return int(raw)

def get_pod_resource_usage() -> List[Dict[str, Any]]:
    """
    Çıktı: kubectl top pod -A --no-headers
    NAMESPACE  NAME  CPU(cores)  MEMORY(bytes)
    """
    try:
        out = subprocess.check_output(
            ["kubectl", "top", "pod", "-A", "--no-headers"],
            text=True,
            timeout=10,
        )
    except Exception:
        return []

    usage: List[Dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        ns, name, cpu_raw, mem_raw = parts[:4]
        cpu_m = _parse_cpu(cpu_raw)
        mem_b = _parse_mem(mem_raw)
        cpu_pct = round(cpu_m / _CPU_TOTAL_MILLICORE * 100, 2) if _CPU_TOTAL_MILLICORE else None
        mem_pct = round(mem_b / _MEM_TOTAL_BYTES * 100, 2) if _MEM_TOTAL_BYTES else None
        usage.append(
            {
                "ns": ns,
                "pod_name": name,
                "cpu_millicores": cpu_m,
                "cpu_percent": cpu_pct,
                "memory_bytes": mem_b,
                "memory_percent": mem_pct,
            }
        )
    return usage

# ----------------------------------------------------------------------------- #
# RULE ENGINE                                                                   #
# ----------------------------------------------------------------------------- #
def matches_rule(pod: Dict[str, Any], rule: Dict[str, Any], reason: str) -> bool:
    m = rule.get("match", {})
    # namespace eşleşmesi
    if "namespace" in m and m["namespace"] != pod["ns"]:
        return False
    # label eşleşmesi
    if "labels" in m:
        pod_labels = pod.get("labels", {})
        if not all(pod_labels.get(k) == v for k, v in m["labels"].items()):
            return False
    # hata nedeni
    return m.get("reason") == reason

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

def evaluate_pod_events(pods: List[Dict[str, Any]]) -> None:
    """CrashLoopBackOff / OOMKilled vb. olayları kurallarla eşleştirir."""
    for pod in pods:
        reason = pod.get("status_reason")
        if not reason:
            continue
        for rule in RULES["rules"]:
            if matches_rule(pod, rule, reason):
                action = rule.get("action", {})
                if action.get("type") == "delete_pod":
                    delete_pod(pod["ns"], pod["name"])
                elif action.get("type") == "scale_deployment":
                    scale_deployment(
                        pod["ns"],
                        action.get("deployment") or pod["owner"],
                        action.get("replicas", 1),
                    )

# ----------------------------------------------------------------------------- #
# METRIC & EVENT GATHERING                                                      #
# ----------------------------------------------------------------------------- #
def collect_metrics() -> Optional[Dict[str, Any]]:
    ts = time.time()

    # host metrics
    cpu_percent = psutil.cpu_percent(interval=1)
    mem_percent = psutil.virtual_memory().percent
    disk_percent = psutil.disk_usage("/").percent

    # kubernetes pod list
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
            reason = (
                item["status"].get("reason")
                or item["status"]
                .get("containerStatuses", [{}])[0]
                .get("state", {})
                .get("waiting", {})
                .get("reason")
            )
            pods.append(
                {
                    "ns": item["metadata"]["namespace"],
                    "name": item["metadata"]["name"],
                    "status_reason": reason,
                    "status": status,
                    "labels": item["metadata"].get("labels", {}),
                    "owner": item["metadata"]
                    .get("ownerReferences", [{}])[0]
                    .get("name", ""),
                }
            )
            status_counts[status] += 1
    except Exception:
        pass  # host-only mod

    # pod resource metrics
    pod_resources = get_pod_resource_usage()

    # rule evaluation
    evaluate_pod_events(pods)

    return {
        "agent_id": settings.agent_id,
        "timestamp": ts,
        "cpu_percent": cpu_percent,
        "memory_percent": mem_percent,
        "disk_percent": disk_percent,
        "kubernetes_available": k8s_available,
        "pod_count": len(pods),
        "pod_status_counts": dict(status_counts),
        "pod_resources": pod_resources,  # yeni alan
        "health_status": health_status["status"],
    }

# ----------------------------------------------------------------------------- #
# RABBITMQ iletişimi                                                            #
# ----------------------------------------------------------------------------- #
async def on_rules_message(msg: AbstractIncomingMessage) -> None:
    global RULES
    async with msg.process():
        try:
            payload = json.loads(msg.body.decode())
            if payload.get("version") != RULES["version"]:
                RULES = payload
                logger.info(
                    "rules update received",
                    extra={"agent_id": settings.agent_id, "version": RULES["version"]},
                )
        except Exception as e:
            logger.error(
                "rules message error",
                extra={"agent_id": settings.agent_id, "err": str(e)},
            )

async def setup_connection() -> bool:
    global connection, channel
    for _ in range(settings.max_retries):
        try:
            connection = await connect_robust(str(settings.rabbitmq_url))
            channel = await connection.channel()
            await channel.declare_queue(settings.metrics_queue, durable=True)

            # rules fan-out
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

async def publish_metrics(data: Dict[str, Any]) -> bool:
    global channel
    if not channel or channel.is_closed:
        if not await setup_connection():
            return False
    try:
        body = json.dumps(data, ensure_ascii=False).encode()
        await channel.default_exchange.publish(
            Message(body, delivery_mode=2),
            routing_key=settings.metrics_queue,
        )
        health_status["last_successful_publish"] = time.time()
        return True
    except Exception:
        health_status["status"] = "publish_failed"
        return False

async def publish_loop() -> None:
    if not await setup_connection():
        logger.error("cannot connect to RabbitMQ")
        return
    while not shutdown_event.is_set():
        data = collect_metrics()
        if data:
            await publish_metrics(data)
        await asyncio.sleep(settings.interval)

# ----------------------------------------------------------------------------- #
# MAIN                                                                          #
# ----------------------------------------------------------------------------- #
async def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
    await publish_loop()

if __name__ == "__main__":
    asyncio.run(main())
