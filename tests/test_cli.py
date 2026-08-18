import subprocess

import cv2
import numpy as np
import pytest
from helpers import cli, make_detection, make_sae_msg, write_saedump

ALL_COMMANDS = ['sae-echo', 'sae-play', 'sae-plot', 'sae-record', 'sae-watch']


@pytest.mark.parametrize('command', ALL_COMMANDS)
def test_entrypoint_shows_help(command):
    '''Guards the [project.scripts] wiring, the package-relative imports and argument parsing.'''
    result = subprocess.run([cli(command), '--help'], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, f'{command} --help failed:\n{result.stderr}'
    assert result.stdout.startswith(f'usage: {command}')


def test_plot_draws_trajectories(tmp_path):
    '''sae-plot end to end: dump file in, annotated jpg out.'''
    stream_key = 'objecttracker:test'
    object_id = b'\x01' * 16
    messages = []
    for idx in range(20):
        msg = make_sae_msg(timestamp_utc_ms=1700000000000 + idx * 100, num_detections=0)
        # A single object walking across the frame, so there is a trajectory to draw
        msg.detections.append(_moving_detection(idx, object_id))
        messages.append(msg)

    dump_file = tmp_path / 'trajectories.saedump'
    write_saedump(dump_file, stream_key, messages)

    result = subprocess.run([cli('sae-plot'), str(dump_file)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f'sae-plot failed:\n{result.stderr}'

    output_file = tmp_path / 'trajectories.jpg'
    assert output_file.exists(), f'sae-plot did not write {output_file}. stdout:\n{result.stdout}'

    image = cv2.imread(str(output_file), cv2.IMREAD_COLOR)
    assert image is not None, 'sae-plot wrote a file that is not a readable image'
    assert image.shape == (1080, 1920, 3)

    # Without a background image plot.py starts from uniform grey - anything else is drawn content
    assert len(np.unique(image.reshape(-1, 3), axis=0)) > 1, 'no trajectories were drawn'


def test_plot_uses_background_image(tmp_path):
    '''The -i option plots onto an existing image, adopting its dimensions.'''
    background = np.full((240, 320, 3), 255, dtype=np.uint8)
    background_file = tmp_path / 'background.png'
    cv2.imwrite(str(background_file), background)

    object_id = b'\x02' * 16
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx * 100, num_detections=0) for idx in range(10)]
    for idx, msg in enumerate(messages):
        msg.detections.append(_moving_detection(idx, object_id))

    dump_file = tmp_path / 'on-background.saedump'
    write_saedump(dump_file, 'objecttracker:test', messages)

    result = subprocess.run(
        [cli('sae-plot'), '-i', str(background_file), str(dump_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f'sae-plot failed:\n{result.stderr}'

    image = cv2.imread(str(tmp_path / 'on-background.jpg'), cv2.IMREAD_COLOR)
    assert image is not None
    assert image.shape == (240, 320, 3)


def _moving_detection(idx, object_id):
    det = make_detection(idx=0, object_id=object_id)
    det.bounding_box.min_x = 0.05 + idx * 0.04
    det.bounding_box.min_y = 0.05 + idx * 0.04
    det.bounding_box.max_x = 0.10 + idx * 0.04
    det.bounding_box.max_y = 0.10 + idx * 0.04
    return det
