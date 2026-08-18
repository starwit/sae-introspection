import time

import pytest
import valkey
from testcontainers.core.container import DockerContainer


@pytest.fixture(scope='module')
def valkey_container():
    with DockerContainer(image='valkey/valkey:9-alpine').with_exposed_ports(6379) as container:
        yield container


@pytest.fixture(scope='module')
def valkey_client(valkey_container):
    client = valkey.Valkey(host=valkey_container.get_container_host_ip(),
                           port=valkey_container.get_exposed_port(6379))

    # DockerContainer has no wait strategy, so the container may not be accepting
    # connections yet when the first test starts
    deadline = time.time() + 30
    while True:
        try:
            client.ping()
            return client
        except valkey.exceptions.ConnectionError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


@pytest.fixture(autouse=True)
def cleanup_valkey(valkey_client):
    yield
    valkey_client.flushall()


@pytest.fixture
def valkey_args(valkey_container):
    '''Host/port as the CLI flags every tool takes.'''
    return ['-h', str(valkey_container.get_container_host_ip()),
            '-p', str(valkey_container.get_exposed_port(6379))]
