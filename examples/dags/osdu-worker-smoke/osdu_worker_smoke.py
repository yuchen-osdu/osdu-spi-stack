# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Example add-on DAG family: one KubernetesPodOperator worker task.

Written as a CImpl-style template, the way DDMS and EDS repositories publish
their DAGs: the stack's loader replaces each placeholder from the family's
render map in dag-sources.yaml before Airflow parses the file. The
worker runs in its own pod as the OSDU Workload Identity and lists the
caller's Entitlements groups, which proves a worker image can reach the OSDU
services with a token the mesh accepts. It is not a product DAG.
"""

from datetime import datetime

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

WORKER = """
import json, os, urllib.request
from azure.identity import WorkloadIdentityCredential

partition = os.environ["DATA_PARTITION"]
scope = os.environ["AZURE_RESOURCE_ID"].rstrip("/") + "/.default"
token = WorkloadIdentityCredential().get_token(scope).token
request = urllib.request.Request(
    os.environ["entitlements_service_endpoint"].rstrip("/") + "/groups",
    headers={"Authorization": "Bearer " + token, "data-partition-id": partition},
)
with urllib.request.urlopen(request, timeout=60) as response:
    groups = json.load(response).get("groups") or []
print(f"worker identity holds {len(groups)} entitlements groups in {partition}")
if not groups:
    raise SystemExit("no entitlements groups returned")
"""

with DAG(
    dag_id="{| DAG_NAME |}",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["spi-example"],
) as dag:
    KubernetesPodOperator(
        task_id="list_entitlements_groups",
        name="osdu-worker-smoke",
        namespace="{| NAMESPACE |}",
        image="{| DOCKER_IMAGE |}",
        cmds=["python", "-c"],
        arguments=[WORKER],
        env_vars={
            **{| ENV_VARS |},
            "DATA_PARTITION": "{{ (dag_run.conf.get('execution_context') or {}).get('data-partition-id') or var.value.get('data_partition', 'opendes') }}",
        },
        **{| K8S_POD_OPERATOR_KWARGS |},
    )
