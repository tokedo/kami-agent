"""The environment container against a REAL docker daemon (SPEC D8; probes P15–P17).

The unit tier proves what the scaffold asks docker for and what the
definition mounts. These probes prove what a real environment built from
``deploy/environment/`` actually lets a command reach, from the inside,
through the scaffold's own client. They need a running environment and
skip without one:

    KAMI_SMOKE_ENV_CONTAINER=kami-env       the container to probe
    KAMI_SMOKE_RUN_DIR=/srv/run             the host run directory it mounts
    KAMI_SMOKE_RUNG=shell | shell_keeper    the rung its image was built for
    KAMI_SMOKE_ENV_ALLOW_STOP=1             also stop it, as run_complete does
                                            (P16's last half; destructive)

Docker socket: DOCKER_HOST (unix://) or /var/run/docker.sock.
"""

import os
import uuid
from pathlib import Path

import pytest

from kami_agent.docker_api import DockerClient, DockerError
from kami_agent.environment import Environment

pytestmark = pytest.mark.docker

CONTAINER = os.environ.get("KAMI_SMOKE_ENV_CONTAINER")
RUN_DIR = os.environ.get("KAMI_SMOKE_RUN_DIR")
RUNG = os.environ.get("KAMI_SMOKE_RUNG", "shell")


def _docker_or_skip() -> DockerClient:
    if not CONTAINER or not RUN_DIR:
        pytest.skip("no environment named (KAMI_SMOKE_ENV_CONTAINER, KAMI_SMOKE_RUN_DIR)")
    client = DockerClient()
    try:
        client.inspect(CONTAINER)
    except DockerError as exc:
        pytest.skip(f"docker or the environment is not reachable: {exc.message}")
    return client


@pytest.fixture
def environment():
    client = _docker_or_skip()
    return Environment(client, CONTAINER, Path(RUN_DIR), keeper_rung=RUNG == "shell_keeper")


def run(environment, command, seq=[0]):  # noqa: B006 - a per-module call counter
    seq[0] += 1
    return environment.shell(command, 30, session=999, call_seq=seq[0], result_max_bytes=65536)


# --- P15: what is not mounted does not exist --------------------------------------------


@pytest.mark.parametrize(
    "probe",
    [
        "ls /srv/run",
        "ls /srv/keys",
        "test -S /var/run/docker.sock",
        "ls /srv/lens/kami-lens.json /srv/lens/state 2>&1; test ! -e /srv/lens/state",
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), 3)\"",
    ],
)
def test_p15_the_run_the_keys_docker_and_the_network_are_unreachable(environment, probe):
    result = run(environment, probe)
    if probe.startswith("ls /srv/lens/kami-lens.json"):
        assert result.exit_code == 0, result.text  # the daemon's data is not there
    else:
        assert result.exit_code != 0, result.text


def test_p15_workspace_writes_land_in_the_run_directory(environment):
    name = f"p15-{uuid.uuid4().hex}.txt"
    result = run(environment, f"echo marker > /workspace/{name} && id -u")
    assert result.exit_code == 0, result.text
    assert "1000" in result.text
    landed = Path(RUN_DIR) / "workspace" / name
    assert landed.read_text() == "marker\n"
    landed.unlink()


# --- P16: what the agent leaves running persists, and not past the run ---------------------


def test_p16_a_background_process_persists_between_calls_and_into_the_snapshot(environment):
    marker = f"sleep {600 + uuid.uuid4().int % 1000}"
    assert run(environment, f"nohup {marker} >/dev/null 2>&1 &").exit_code == 0
    found = run(environment, f"pgrep -f '^{marker}$'")
    assert found.exit_code == 0, found.text
    snap = environment.snapshot(last_restart_count=None)
    assert any(p["args"] == marker for p in snap.processes), snap.processes
    if os.environ.get("KAMI_SMOKE_ENV_ALLOW_STOP") == "1":
        assert environment.stop() is True
        after = environment.snapshot(last_restart_count=None, start_if_stopped=False, probe=False)
        assert after.running is False
        assert after.process_count == 0


# --- P17: the two daemons answer from inside, and nothing else does ----------------------


def test_p17_the_lens_answers_from_inside(environment):
    snap = environment.snapshot(last_restart_count=None)
    assert snap.lens_socket_ok is True


def test_p17_the_keeper_exists_only_on_its_rung(environment):
    present = run(environment, "command -v keeper")
    if RUNG == "shell_keeper":
        assert present.exit_code == 0, present.text
        status = run(environment, "keeper status")
        assert status.exit_code in (0, 1), status.text
        assert '"profile": "lab"' in status.text
    else:
        assert present.exit_code != 0, present.text
        assert run(environment, "test -e /srv/keeper").exit_code != 0
