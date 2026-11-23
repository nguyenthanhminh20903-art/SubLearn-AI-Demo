import logging
import shutil
import subprocess
from pathlib import Path
from socket import gethostname

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
MIB = 1024 * 1024
WORKER_ID = gethostname()[:100]


class ResourceMetrics(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)
    cpu_usage: float | None = Field(default=None, ge=0, le=100)
    ram_usage_mb: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    gpu_usage: float | None = Field(default=None, ge=0, le=100)
    vram_usage_mb: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    disk_usage_mb: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def measure(path, *, gpu=False):
    metrics = ResourceMetrics(worker_id=WORKER_ID)
    try:
        import psutil

        memory = psutil.virtual_memory()
        metrics.cpu_usage = psutil.cpu_percent(interval=0.15)
        metrics.ram_usage_mb = round((memory.total - memory.available) / MIB, 2)
    except (ImportError, OSError):
        pass
    try:
        metrics.disk_usage_mb = round(shutil.disk_usage(path).used / MIB, 2)
    except OSError:
        pass
    nvidia = shutil.which('nvidia-smi') if gpu else None
    if nvidia:
        try:
            report = subprocess.run(
                [nvidia, '--query-gpu=utilization.gpu,memory.used', '--format=csv,noheader,nounits'],
                check=True, capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            values = [tuple(map(float, row.split(','))) for row in report.stdout.splitlines() if row.strip()]
            if values:
                metrics.gpu_usage = max(item[0] for item in values)
                metrics.vram_usage_mb = sum(item[1] for item in values)
        except (ValueError, OSError, IndexError, subprocess.SubprocessError):
            pass
    return metrics


def save_sample(cursor, job_id, environment, metrics):
    cursor.execute('''INSERT INTO RESOURCE_METRIC
        (job_id,environment,worker_id,cpu_usage,ram_usage_mb,gpu_usage,vram_usage_mb,disk_usage_mb,recorded_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP(3))''',
        (job_id, environment, metrics.worker_id, metrics.cpu_usage, metrics.ram_usage_mb,
         metrics.gpu_usage, metrics.vram_usage_mb, metrics.disk_usage_mb))


def monitor_local(stop):
    from .mysql_db import mysql_transaction

    while not stop.wait(10):
        try:
            with mysql_transaction() as cursor:
                cursor.execute('''SELECT DISTINCT j.job_id FROM PROCESSING_JOB j
                                  JOIN PROCESSING_STEP s ON s.job_id=j.job_id
                                  WHERE j.status='RUNNING' AND s.status='RUNNING'
                                    AND s.execution_environment='LOCAL' ''')
                jobs = cursor.fetchall()
            if jobs:
                sample = measure(Path(__file__).parent)
                with mysql_transaction() as cursor:
                    for job in jobs:
                        save_sample(cursor, job['job_id'], 'LOCAL', sample)
        except Exception as error:
            logger.debug('Resource sample unavailable (%s)', type(error).__name__)
