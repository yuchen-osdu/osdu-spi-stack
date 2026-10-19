"""Liveness DAG: one shell task, so a success proves scheduling and execution."""

from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

with DAG(
    "airflow_monitoring",
    description="Liveness monitoring",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    dagrun_timeout=timedelta(minutes=20),
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
):
    BashOperator(task_id="echo", bash_command="echo test", priority_weight=2**31 - 1)
