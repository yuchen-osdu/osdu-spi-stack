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

from datetime import datetime, timezone

from spi import images
from spi.images import (
    ImageRegistryEntry,
    ImageSource,
    ResolvedImage,
    image_lock_names,
    render_image_lock_configmap,
    resolve_ghcr_ref_image,
    resolve_ghcr_tag_image,
    resolve_image,
)

WELLBORE_IMAGES = {
    "wellbore-domain-services",
    "wellbore-domain-services-worker",
}


def test_image_sets_are_profile_aware():
    core = set(image_lock_names("core"))
    graduated = set(image_lock_names("graduated"))

    assert WELLBORE_IMAGES.isdisjoint(core)
    assert graduated == core | WELLBORE_IMAGES
    assert len(core) == 13
    assert len(graduated) == 15


def test_resolve_image_selects_newest_immutable_sha(monkeypatch):
    old_sha = "a" * 40
    new_sha = "b" * 40

    def fake_gitlab_get(url: str):
        if "registry/repositories?" in url:
            return [
                {
                    "id": 123,
                    "name": "partition-master",
                    "location": "community.opengroup.org:5555/osdu/partition-master",
                }
            ]
        if url.endswith("/tags?per_page=100&page=1"):
            return [{"name": old_sha}, {"name": "latest"}, {"name": new_sha}]
        if url.endswith(f"/tags/{old_sha}"):
            return {
                "name": old_sha,
                "created_at": "2026-05-01T00:00:00+00:00",
                "digest": "sha256:old",
            }
        if url.endswith("/tags/latest"):
            return {
                "name": "latest",
                "created_at": "2026-05-22T00:00:00+00:00",
                "digest": "sha256:latest",
            }
        if url.endswith(f"/tags/{new_sha}"):
            return {
                "name": new_sha,
                "created_at": "2026-05-21T00:00:00+00:00",
                "digest": "sha256:new",
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(images, "gitlab_get", fake_gitlab_get)

    resolved = resolve_image(
        "partition",
        ImageRegistryEntry(1, "partition", "services/partition.yaml"),
        "master",
    )

    assert resolved.tag == new_sha
    assert resolved.digest == "sha256:new"


def test_community_default_branch_can_differ_per_service(monkeypatch):
    seen = {}

    def fake_repositories(project_id, image_name):
        seen["image_name"] = image_name
        return [
            {
                "id": 123,
                "name": image_name,
                "location": f"community.opengroup.org:5555/example/{image_name}",
            }
        ]

    monkeypatch.setattr(images, "_registry_repositories", fake_repositories)
    monkeypatch.setattr(images, "_registry_tags", lambda project_id, repo_id: [{"name": "sha"}])
    monkeypatch.setattr(
        images,
        "_newest_immutable_tag",
        lambda project_id, repo_id, tags: {
            "name": "a" * 40,
            "digest": "sha256:worker",
            "created_at": "",
        },
    )

    resolve_image(
        "wellbore-domain-services-worker",
        ImageRegistryEntry(
            1384,
            "wellbore-domain-services-worker",
            "worker.yaml",
            community_default_branch="main",
        ),
        "master",
    )

    assert seen["image_name"] == "wellbore-domain-services-worker-main"


def test_render_image_lock_contains_service_keys_without_schema_load():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"community.opengroup.org:5555/example/{name}",
            tag="1" * 40,
            created_at="2026-05-22T00:00:00+00:00",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        source=ImageSource.COMMUNITY,
        ref="master",
        org="",
        resolved_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
    )

    assert "name: osdu-image-lock" in yaml
    assert 'IMAGE_SOURCE: "community"' in yaml
    assert 'IMAGE_TAG: ""' in yaml
    assert 'IMAGE_BRANCH: "master"' in yaml
    assert 'IMAGE_PROFILE: "core"' in yaml
    assert "PARTITION_IMAGE_REPOSITORY" in yaml
    assert "PARTITION_IMAGE_DIGEST" in yaml
    assert "INDEXER_QUEUE_IMAGE_TAG" in yaml
    assert "SCHEMA_LOAD_IMAGE_TAG" not in yaml


def test_resolve_ghcr_tag_image_pins_manifest_digest(monkeypatch):
    monkeypatch.setattr(
        images,
        "_ghcr_manifest_digest",
        lambda repository, tag: "sha256:" + ("b" * 64),
    )

    resolved = resolve_ghcr_tag_image(
        service_name="partition",
        org="yuchen-osdu",
        tag="main-snapshot",
    )

    assert resolved.repository == "ghcr.io/yuchen-osdu/partition"
    assert resolved.tag == "main-snapshot"
    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.image == f"{resolved.repository}@{resolved.digest}"


def test_resolve_ghcr_ref_image_uses_ref_commit_and_manifest_digest(monkeypatch):
    commit_sha = "a" * 40

    monkeypatch.setattr(
        images,
        "github_get",
        lambda url: {
            "sha": commit_sha,
            "commit": {"committer": {"date": "2026-07-20T00:00:00Z"}},
        },
    )
    monkeypatch.setattr(
        images,
        "_ghcr_manifest_digest",
        lambda repository, tag: "sha256:" + ("b" * 64),
    )

    resolved = resolve_ghcr_ref_image(
        service_name="partition",
        org="yuchen-osdu",
        ref="fix/core-lib-azure-3.0.1",
    )

    assert resolved.repository == "ghcr.io/yuchen-osdu/partition"
    assert resolved.tag == "sha-" + commit_sha[:12]
    assert resolved.digest == "sha256:" + ("b" * 64)
    assert resolved.image == f"{resolved.repository}@{resolved.digest}"


def test_render_ghcr_main_lock_records_tag_selector():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"ghcr.io/yuchen-osdu/{name}",
            tag="main-snapshot",
            created_at="",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names()
    }

    yaml = render_image_lock_configmap(
        resolved,
        source=ImageSource.GHCR,
        org="yuchen-osdu",
        resolved_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert 'IMAGE_TAG: "main-snapshot"' in yaml
    assert 'IMAGE_REF: ""' in yaml
    assert 'IMAGE_BRANCH: ""' in yaml


def test_render_graduated_lock_contains_only_selected_profile_images():
    resolved = {
        name: ResolvedImage(
            name=name,
            repository=f"ghcr.io/yuchen-osdu/{name}",
            tag="main-snapshot",
            created_at="",
            digest=f"sha256:{name}",
        )
        for name in image_lock_names("graduated")
    }

    yaml = render_image_lock_configmap(
        resolved,
        source=ImageSource.GHCR,
        org="yuchen-osdu",
        profile="graduated",
        resolved_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )

    assert 'IMAGE_PROFILE: "graduated"' in yaml
    assert 'IMAGE_COUNT: "15"' in yaml
    assert "WELLBORE_DOMAIN_SERVICES_IMAGE_DIGEST" in yaml
    assert "WELLBORE_DOMAIN_SERVICES_WORKER_IMAGE_DIGEST" in yaml
