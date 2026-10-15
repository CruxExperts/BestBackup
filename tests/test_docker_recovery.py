import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from bbackup.docker_recovery import DockerRecoveryError, DockerRecoveryUncertainError, DockerRuntime

IMAGE_ID = "sha256:" + "a" * 64


class FakeContainer:
    def __init__(self, client, container_id, name, state, mounts, *, start_error=False, stop_error=False):
        self._client = client
        self.id = container_id
        self.start_error = start_error
        self.stop_error = stop_error
        self.events = []
        self.diff_states = []
        self.attrs = {
            "Id": container_id,
            "Name": f"/{name}",
            "Image": IMAGE_ID,
            "Config": {
                "Image": "app:stable",
                "Env": ["APP_SECRET=private-value"],
                "Cmd": ["serve"],
                "Entrypoint": None,
                "User": "1000",
                "WorkingDir": "/srv/app",
                "Labels": {"role": "app"},
                "ExposedPorts": {},
                "Volumes": {},
                "Tty": False,
                "OpenStdin": False,
                "StdinOnce": False,
                "Healthcheck": None,
            },
            "HostConfig": {
                "Privileged": False,
                "PublishAllPorts": False,
                "AutoRemove": False,
                "CapAdd": None,
                "CapDrop": None,
                "Devices": None,
                "DeviceRequests": None,
                "Links": None,
                "VolumesFrom": None,
                "PortBindings": {},
                "Tmpfs": None,
                "Sysctls": None,
                "ExtraHosts": None,
                "Dns": None,
                "DnsSearch": None,
                "DnsOptions": None,
                "SecurityOpt": None,
                "StorageOpt": None,
                "Ulimits": None,
                "LogConfig": {"Type": "json-file", "Config": {}},
                "NetworkMode": "bridge",
                "PidMode": "",
                "IpcMode": "private",
                "UTSMode": "",
                "UsernsMode": "",
                "CgroupnsMode": "private",
                "Runtime": "runc",
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "ReadonlyRootfs": False,
                "MaskedPaths": [],
                "ReadonlyPaths": [],
                "Mounts": [],
            },
            "NetworkSettings": {"Networks": {"bridge": {"IPAMConfig": None, "Aliases": []}}},
            "Mounts": mounts,
            "State": {"Status": state},
        }

    def reload(self):
        return None

    def diff(self):
        self.diff_states.append(self.attrs["State"]["Status"])
        results = getattr(self, "diff_results", None)
        if results is not None:
            return results.pop(0)
        return getattr(self, "writable_layer_changes", [])

    def stop(self, *, timeout):
        self.events.append(("stop", timeout))
        if self.stop_error:
            raise RuntimeError("stop failed")
        self.attrs["State"]["Status"] = "exited"

    def start(self):
        self.events.append(("start",))
        if self.start_error:
            raise RuntimeError("start failed")
        self.attrs["State"]["Status"] = "running"


class FakeContainers:
    def __init__(self, containers=()):
        self.by_name = {}
        self.by_id = {}
        for container in containers:
            self.add(container)

    def add(self, container):
        self.by_name[container.attrs["Name"].lstrip("/")] = container
        self.by_id[container.id] = container

    def remove_id(self, container_id):
        container = self.by_id.pop(container_id)
        self.by_name.pop(container.attrs["Name"].lstrip("/"), None)

    def get(self, identifier):
        container = self.by_id.get(identifier) or self.by_name.get(identifier.lstrip("/"))
        if container is None:
            raise type("NotFound", (Exception,), {})()
        return container

    def list(self, *, all=False):
        return list(self.by_id.values())


class FakeImage:
    id = IMAGE_ID
    attrs = {"Config": {"Healthcheck": None, "ExposedPorts": {}}}


class FakeImages:
    def get(self, image_id):
        if image_id != FakeImage.id:
            raise RuntimeError("missing image")
        return FakeImage()


class FakeVolume:
    attrs = {"Driver": "local", "Scope": "local", "Options": {}}

    def reload(self):
        return None


class FakeVolumes:
    def __init__(self):
        self.by_name = {}

    def add(self, name):
        self.by_name[name] = FakeVolume()

    def get(self, name):
        return self.by_name[name]


class FakeDockerAPI:
    def __init__(self, client):
        self.client = client

    def create_host_config(self, *, network_mode, restart_policy, read_only):
        return {
            "NetworkMode": network_mode,
            "RestartPolicy": restart_policy,
            "ReadonlyRootfs": read_only,
        }

    def create_container(self, **kwargs):
        container = FakeContainer(self.client, "new-container-id", kwargs["name"], "created", [])
        self.client.containers.add(container)
        return {"Id": container.id}


class FakeClient:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)
        self.containers.client = self
        self.images = FakeImages()
        self.volumes = FakeVolumes()
        self.api = FakeDockerAPI(self)
        self.closed = False

    def info(self):
        return {"ID": "daemon-id"}

    def close(self):
        self.closed = True


def _socket_binding(tmp_path):
    socket_path = tmp_path / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    return listener, socket_path


def _source():
    return SimpleNamespace(name="app", kind="docker", container="old-app",
                           connection="local", stop_policy="stop-restart")


def _runtime(tmp_path, container, listener):
    client = FakeClient([container])
    runtime = DockerRuntime(
        tmp_path / "state",
        {"local": {"socket": str(listener.getsockname())}},
        client_factory=lambda *_args: client,
        recovery_timeout=0.01,
        poll_interval=0.001,
    )
    container._client = client
    return runtime, client


def _mount(data_path):
    return [{
        "Type": "bind", "Source": str(data_path), "Destination": "/srv/app/data",
        "RW": True, "Propagation": "rprivate", "Name": "",
    }]


def test_stopped_container_is_not_stopped_or_started(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "exited", _mount(data))
    runtime, client = _runtime(tmp_path, container, listener)
    private_dir = tmp_path / "state" / "capture"

    with runtime.capture((_source(),), private_dir, protected_paths=(tmp_path / "state",),
                         operation_id="capture-1") as (paths, exports):
        metadata = Path(exports[0]["path"])
        assert str(data.resolve()) in paths
        assert metadata.stat().st_mode & 0o777 == 0o600
        assert json.loads(metadata.read_text())["container_id"] == "exact-id"

    assert container.attrs["State"]["Status"] == "exited"
    assert container.events == []
    assert not runtime.journal_path.exists()
    assert client.closed
    listener.close()


def test_capture_preserves_daemon_masks_and_typed_local_mounts(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    volume_data = tmp_path / "local_volume"
    volume_data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    mounts = _mount(data) + [{
        "Type": "volume",
        "Source": str(volume_data),
        "Destination": "/srv/app/database",
        "RW": False,
        "Propagation": "",
        "Name": "app-data",
    }]
    container = FakeContainer(None, "exact-id", "old-app", "exited", mounts)
    container.attrs["Config"]["Volumes"] = {"/srv/app/database": {}}
    masked_paths = ["/proc/custom", "/sys/devices/system/cpu/cpu0/thermal_throttle"]
    readonly_paths = ["/proc/another-custom-path"]
    container.attrs["HostConfig"]["MaskedPaths"] = masked_paths
    container.attrs["HostConfig"]["ReadonlyPaths"] = readonly_paths
    container.attrs["HostConfig"]["Mounts"] = [
        {
            "Type": "bind",
            "Source": str(data),
            "Target": "/srv/app/data",
            "ReadOnly": False,
            "BindOptions": {"Propagation": "rprivate"},
        },
        {
            "Type": "volume",
            "Source": "app-data",
            "Target": "/srv/app/database",
            "ReadOnly": True,
            "VolumeOptions": {
                "NoCopy": False, "Labels": None, "DriverConfig": None, "Subpath": "",
            },
        },
    ]
    runtime, client = _runtime(tmp_path, container, listener)
    client.volumes.add("app-data")

    with runtime.capture(
        (_source(),), tmp_path / "state" / "capture",
        protected_paths=(tmp_path / "state",), operation_id="capture-masks-mount",
    ) as (paths, exports):
        metadata = json.loads(Path(exports[0]["path"]).read_text())
        assert str(data.resolve()) in paths
        assert str(volume_data.resolve()) in paths

    archived_mounts = {mount["destination"]: mount for mount in metadata["mounts"]}
    assert metadata["schema_version"] == 2
    assert metadata["host"]["masked_paths"] == masked_paths
    assert metadata["host"]["readonly_paths"] == readonly_paths
    assert archived_mounts["/srv/app/data"]["read_only"] is False
    assert archived_mounts["/srv/app/database"]["source_kind"] == "local-volume"
    assert archived_mounts["/srv/app/database"]["read_only"] is True
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_refuses_writable_layer_changes_before_stopping(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    container.writable_layer_changes = [{"Path": "/app/state.db", "Kind": 1}]
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(DockerRecoveryError, match="writable-layer changes"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-writable-layer",
        ):
            pass

    assert container.events == []
    assert not runtime.journal_path.exists()
    listener.close()

def test_capture_allows_added_mountpoint_entries_rebuilt_from_metadata(tmp_path):
    data_paths = (tmp_path / "volume", tmp_path / "data")
    for path in data_paths:
        path.mkdir()
    listener, _ = _socket_binding(tmp_path)
    destinations = ("/volume", "/data")
    mounts = [
        {
            "Type": "bind",
            "Source": str(path),
            "Destination": destination,
            "RW": True,
            "Propagation": "rprivate",
            "Name": "",
        }
        for path, destination in zip(data_paths, destinations)
    ]
    container = FakeContainer(None, "exact-id", "old-app", "running", mounts)
    container.writable_layer_changes = [
        {"Path": destination, "Kind": 1} for destination in destinations
    ]
    runtime, _client = _runtime(tmp_path, container, listener)

    with runtime.capture(
        (_source(),), tmp_path / "state" / "capture",
        protected_paths=(tmp_path / "state",), operation_id="capture-mountpoints",
    ) as (paths, _exports):
        assert container.attrs["State"]["Status"] == "exited"
        assert all(str(path.resolve()) in paths for path in data_paths)

    assert container.diff_states == ["running", "exited"]
    assert [event[0] for event in container.events] == ["stop", "start"]
    assert not runtime.journal_path.exists()
    listener.close()


@pytest.mark.parametrize(
    ("change_path", "change_kind"),
    [
        ("/data-sibling", 1),
        ("/data/child", 1),
        ("/data", 0),
        ("/data", 2),
    ],
)
def test_capture_refuses_changes_not_at_added_mountpoint(tmp_path, change_path, change_kind):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    container.writable_layer_changes = [{"Path": change_path, "Kind": change_kind}]
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(DockerRecoveryError, match="writable-layer changes"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-near-mount",
        ):
            pytest.fail("capture yielded paths for an uncaptured root-layer entry")

    assert container.events == []
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_rechecks_writable_layer_after_stop_and_restarts(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    container.diff_results = [
        [],
        [{"Path": "/app/state.db", "Kind": 1}],
    ]
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(DockerRecoveryError, match="writable-layer changes occurred while stopping"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-diff-race",
        ):
            pytest.fail("capture yielded paths despite a post-stop writable-layer change")

    assert container.diff_states == ["running", "exited"]
    assert [event[0] for event in container.events] == ["stop", "start"]
    assert container.attrs["State"]["Status"] == "running"
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_failure_restarts_only_original_running_container(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(RuntimeError, match="restic failed"):
        with runtime.capture((_source(),), tmp_path / "state" / "capture",
                             protected_paths=(tmp_path / "state",), operation_id="capture-2"):
            assert container.attrs["State"]["Status"] == "exited"
            raise RuntimeError("restic failed")

    assert [event[0] for event in container.events] == ["stop", "start"]
    assert container.attrs["State"]["Status"] == "running"
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_does_not_accept_automatic_restart_as_its_restart(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(DockerRecoveryUncertainError, match="did not restore every originally running"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-auto-restart",
        ):
            container.attrs["State"]["Status"] = "running"

    assert [event[0] for event in container.events] == ["stop"]
    assert runtime.journal_path.is_file()
    recovery = runtime.recover("local", reviewed=True)
    assert recovery["recovered"] == 1
    assert [event[0] for event in container.events] == ["stop"]
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_refuses_data_shared_with_unselected_running_container(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    selected = FakeContainer(None, "selected-id", "old-app", "running", _mount(data))
    runtime, client = _runtime(tmp_path, selected, listener)
    other = FakeContainer(client, "other-id", "other-app", "running", _mount(data))
    client.containers.add(other)

    with pytest.raises(DockerRecoveryError, match="not selected for quiescence"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-shared",
        ):
            pass

    assert selected.events == []
    assert other.events == []
    assert not runtime.journal_path.exists()
    listener.close()


def test_capture_refuses_unavailable_unselected_mount_source(tmp_path):
    selected_data = tmp_path / "selected-data"
    selected_data.mkdir()
    unavailable = tmp_path / "removed-mount-source"
    listener, _ = _socket_binding(tmp_path)
    selected = FakeContainer(None, "selected-id", "old-app", "running", _mount(selected_data))
    runtime, client = _runtime(tmp_path, selected, listener)
    unrelated = FakeContainer(client, "unrelated-id", "other-app", "running", _mount(unavailable))
    client.containers.add(unrelated)

    with pytest.raises(DockerRecoveryError, match="unavailable"):
        with runtime.capture(
            (_source(),), tmp_path / "state" / "capture",
            protected_paths=(tmp_path / "state",), operation_id="capture-unavailable-unrelated",
        ):
            pytest.fail("capture proceeded without canonicalizing every running mount")

    assert selected.events == []
    assert unrelated.events == []
    assert not runtime.journal_path.exists()
    listener.close()


def test_restart_failure_keeps_durable_intention_and_fails_capture(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data), start_error=True)
    runtime, _client = _runtime(tmp_path, container, listener)

    with pytest.raises(DockerRecoveryUncertainError, match="review docker recover"):
        with runtime.capture((_source(),), tmp_path / "state" / "capture",
                             protected_paths=(tmp_path / "state",), operation_id="capture-3"):
            pass

    assert container.attrs["State"]["Status"] == "exited"
    assert runtime.journal_path.is_file()
    assert runtime.journal_path.stat().st_mode & 0o777 == 0o600
    listener.close()


def test_recovery_never_starts_replacement_with_same_name(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, _ = _socket_binding(tmp_path)
    original = FakeContainer(None, "original-id", "old-app", "running", _mount(data))
    runtime, client = _runtime(tmp_path, original, listener)

    with pytest.raises(DockerRecoveryUncertainError):
        with runtime.capture((_source(),), tmp_path / "state" / "capture",
                             protected_paths=(tmp_path / "state",), operation_id="capture-4"):
            client.containers.remove_id("original-id")
            replacement = FakeContainer(client, "replacement-id", "old-app", "exited", _mount(data))
            client.containers.add(replacement)

    result = runtime.recover("local", reviewed=True)
    assert result["replayed"] is False
    assert result["unresolved"] == [{"operation_id": "capture-4", "container_id": "original-id"}]
    assert replacement.events == []
    assert runtime.journal_path.is_file()
    listener.close()

def test_prepared_intention_does_not_start_a_container_stop_not_requested_by_capture(tmp_path):
    data = tmp_path / "persistent"
    data.mkdir()
    listener, socket_path = _socket_binding(tmp_path)
    container = FakeContainer(None, "exact-id", "old-app", "running", _mount(data))
    runtime, _client = _runtime(tmp_path, container, listener)
    runtime._journal_add(({
        "operation_id": "capture-prepared",
        "connection": "local",
        "socket": str(socket_path.resolve()),
        "daemon_id": "daemon-id",
        "container_id": "exact-id",
        "container_name": "old-app",
        "phase": "prepared",
    },))
    container.attrs["State"]["Status"] = "exited"

    result = runtime.recover("local", reviewed=True)

    assert result["recovered"] == 0
    assert result["unresolved"] == [{
        "operation_id": "capture-prepared", "container_id": "exact-id",
    }]
    assert container.events == []
    assert runtime.journal_path.is_file()
    listener.close()

def test_restore_refuses_existing_target_container_before_mutation(tmp_path):
    listener, _ = _socket_binding(tmp_path)
    existing = FakeContainer(None, "already-there-id", "restored-app", "running", [])
    runtime, client = _runtime(tmp_path, existing, listener)
    archive = tmp_path / "archive"
    data = archive / "srv" / "app" / "data"
    data.mkdir(parents=True)
    metadata_dir = archive / "metadata"
    metadata_dir.mkdir()
    metadata = {
        "schema_version": 2,
        "kind": "docker-container",
        "source": "app",
        "container_name": "old-app",
        "container_id": "source-id",
        "daemon_id": "source-daemon",
        "image_id": IMAGE_ID,
        "config": {
            "env": ["APP_SECRET=private-value"], "cmd": ["serve"], "entrypoint": None,
            "user": "1000", "working_dir": "/srv/app", "labels": {"role": "app"},
            "exposed_ports": {}, "tty": False, "open_stdin": False, "stdin_once": False,
            "healthcheck": None, "hostname": None, "domainname": None, "stop_signal": None,
        },
        "host": {"network_mode": "bridge", "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
                 "readonly_rootfs": False, "masked_paths": [], "readonly_paths": []},
        "healthcheck_configured": False,
        "mounts": [{"type": "bind", "archive_source": "/srv/app/data",
                    "destination": "/srv/app/data", "read_only": False, "source_kind": "bind"}],
    }
    manifest = metadata_dir / "bbackup-docker-app.json"
    manifest.write_text(json.dumps(metadata))
    manifest.chmod(0o600)

    with pytest.raises(DockerRecoveryError, match="already exists"):
        runtime.restore_new("local", archive, source_name="app", container_name="restored-app",
                            trusted_archive=True, expected_daemon_id="daemon-id")

    assert set(client.containers.by_id) == {"already-there-id"}
    assert existing.attrs["State"]["Status"] == "running"
    assert existing.events == []
    listener.close()

def test_restore_starts_new_container_with_the_restored_mount_data(tmp_path):
    listener, _ = _socket_binding(tmp_path)
    original = FakeContainer(None, "source-container-id", "old-app", "exited", [])
    runtime, client = _runtime(tmp_path, original, listener)
    archive = tmp_path / "archive"
    data = archive / "srv" / "app" / "data"
    data.mkdir(parents=True)
    (data / "fixture.txt").write_text("restored application data\n")
    metadata_dir = archive / "metadata"
    metadata_dir.mkdir()
    metadata = {
        "schema_version": 2,
        "kind": "docker-container",
        "source": "app",
        "container_name": "old-app",
        "container_id": "source-container-id",
        "daemon_id": "source-daemon",
        "image_id": IMAGE_ID,
        "config": {
            "env": ["APP_SECRET=private-value"], "cmd": ["serve"], "entrypoint": None,
            "user": "1000", "working_dir": "/srv/app", "labels": {"role": "app"},
            "exposed_ports": {}, "tty": False, "open_stdin": False, "stdin_once": False,
            "healthcheck": None, "hostname": None, "domainname": None, "stop_signal": None,
        },
        "host": {"network_mode": "bridge", "restart_policy": {"Name": "no", "MaximumRetryCount": 0},
                 "readonly_rootfs": False, "masked_paths": ["/proc/custom-mask"],
                 "readonly_paths": ["/sys/custom-readonly"]},
        "healthcheck_configured": False,
        "mounts": [{"type": "bind", "archive_source": "/srv/app/data",
                    "destination": "/srv/app/data", "read_only": False, "source_kind": "bind"}],
    }
    manifest = metadata_dir / "bbackup-docker-app.json"
    manifest.write_text(json.dumps(metadata))
    manifest.chmod(0o600)

    result = runtime.restore_new(
        "local", archive, source_name="app", container_name="restored-app",
        trusted_archive=True, expected_daemon_id="daemon-id",
    )

    assert result["running"] is True
    assert result["container_name"] == "restored-app"
    assert (data / "fixture.txt").read_text() == "restored application data\n"
    restored = client.containers.get("restored-app")
    assert restored.attrs["State"]["Status"] == "running"
    assert [event[0] for event in restored.events] == ["start"]
    listener.close()
