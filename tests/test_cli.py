import subprocess

import pytest
from helpers import cli

ALL_COMMANDS = ['sae-echo', 'sae-play', 'sae-plot', 'sae-record', 'sae-watch']


@pytest.mark.parametrize('command', ALL_COMMANDS)
def test_entrypoint_shows_help(command):
    '''Guards the [project.scripts] wiring, the package-relative imports and argument parsing.'''
    result = subprocess.run([cli(command), '--help'], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, f'{command} --help failed:\n{result.stderr}'
    assert result.stdout.startswith(f'usage: {command}')
