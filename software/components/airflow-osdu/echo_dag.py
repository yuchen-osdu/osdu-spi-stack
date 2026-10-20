"""Lists installed packages; Workflow integration tests trigger it by name."""

import logging
import os
import site
from datetime import datetime, timedelta

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

logger = logging.getLogger(__name__)


def list_packages():
    for directory in sorted(next(os.walk(site.getsitepackages()[0]))[1]):
        logger.info(directory)


with DAG(
    "Echo_DAG",
    description="Checks the OSDU libraries are installed",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    dagrun_timeout=timedelta(minutes=30),
    default_args={"retries": 0},
):
    PythonOperator(task_id="list_packages_task", python_callable=list_packages)
