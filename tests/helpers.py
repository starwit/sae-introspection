import io
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, List

import pybase64
from PIL import Image
from visionapi.anomaly_pb2 import AnomalyMessage
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import Detection, SaeMessage
from visionlib.saedump import MESSAGE_SEPARATOR, DumpMeta, Event, EventMeta

FRAME_WIDTH = 8
FRAME_HEIGHT = 8
FRAME_CHANNELS = 3
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * FRAME_CHANNELS


def make_sae_msg(source_id='test_source', timestamp_utc_ms=1700000000000, num_detections=2) -> SaeMessage:
    '''Builds a minimal but complete SaeMessage.

    The frame carries raw (uncompressed) data, so frame_data must be exactly
    height * width * channels bytes - that is what get_raw_frame_data() reshapes it by.
    source_id and timestamp_utc_ms must be set, otherwise is_sae_message() (the legacy
    fallback in determine_message_type()) rejects the message.
    '''
    msg = SaeMessage()
    msg.type = MessageType.SAE
    msg.frame.source_id = source_id
    msg.frame.timestamp_utc_ms = timestamp_utc_ms
    msg.frame.shape.width = FRAME_WIDTH
    msg.frame.shape.height = FRAME_HEIGHT
    msg.frame.shape.channels = FRAME_CHANNELS
    msg.frame.frame_data = bytes(FRAME_BYTES)

    for idx in range(num_detections):
        msg.detections.append(_make_detection(idx))

    return msg


def _make_detection(idx=0) -> Detection:
    det = Detection()
    det.bounding_box.min_x = 0.1 + idx * 0.01
    det.bounding_box.min_y = 0.1 + idx * 0.01
    det.bounding_box.max_x = 0.2 + idx * 0.01
    det.bounding_box.max_y = 0.2 + idx * 0.01
    det.confidence = 0.9
    det.class_id = 1
    det.object_id = uuid.uuid4().bytes
    return det


def make_jpeg(width=FRAME_WIDTH, height=FRAME_HEIGHT, color=(200, 50, 50)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buffer, format='JPEG')
    return buffer.getvalue()


def make_anomaly_msg(source_id='test_source', start_ts_ms=1700000000000, num_frames=3, num_trajectories=2,
                     frame_interval_ms=100) -> AnomalyMessage:
    '''Builds an AnomalyMessage: a clip of real JPEG frames plus trajectories with one point per frame.

    The trajectory points use normalized detection centers and the last point of the first
    trajectory is marked as anomaly trigger.
    '''
    msg = AnomalyMessage()
    msg.type = MessageType.ANOMALY
    msg.source_id = source_id
    msg.model_info.name = 'test_model'
    msg.model_info.version = '1.0'

    for idx in range(num_frames):
        frame = msg.jpeg_frames.add()
        frame.timestamp_utc_ms = start_ts_ms + idx * frame_interval_ms
        frame.frame_data_jpeg = make_jpeg()

    for traj_idx in range(num_trajectories):
        traj = msg.trajectories.add()
        traj.class_id = 2
        traj.object_id = uuid.uuid4().bytes
        for idx in range(num_frames):
            point = traj.trajectory_points.add()
            point.timestamp_utc_ms = start_ts_ms + idx * frame_interval_ms
            point.detection_center.x = 0.1 + idx * 0.1
            point.detection_center.y = 0.2 + traj_idx * 0.3
            point.geo_coordinate.latitude = 52.5
            point.geo_coordinate.longitude = 13.4
        if traj_idx == 0:
            traj.trajectory_points[-1].anomaly_trigger = True

    return msg


def write_saedump(path: Path, stream_key: str, messages: List[SaeMessage], interval_s=0.1) -> Path:
    '''Writes messages into a saedump file, mirroring how record.py writes one.'''
    start_time = time.time()

    with open(path, 'w') as file:
        meta = DumpMeta(start_time=start_time, recorded_streams=[stream_key])
        file.write(meta.model_dump_json())
        file.write(MESSAGE_SEPARATOR)

        for idx, msg in enumerate(messages):
            event = Event(
                meta=EventMeta(
                    record_time=start_time + idx * interval_s,
                    source_stream=stream_key,
                ),
                data_b64=pybase64.standard_b64encode(msg.SerializeToString()),
            )
            file.write(event.model_dump_json())
            file.write(MESSAGE_SEPARATOR)

    return path


def cli(name: str) -> str:
    '''Resolves an installed console script, so the entry points are part of what is tested.'''
    path = shutil.which(name)
    assert path is not None, f'Console script {name} not found on PATH. Run "poetry install" first.'
    return path


def run_cli(name: str, *args: str, **popen_kwargs) -> subprocess.Popen:
    '''Starts a tool as a subprocess with unbuffered stdout, so output can be read incrementally.'''
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    return subprocess.Popen(
        [cli(name), *args],
        stdout=popen_kwargs.pop('stdout', subprocess.PIPE),
        stderr=popen_kwargs.pop('stderr', subprocess.PIPE),
        env=env,
        **popen_kwargs,
    )


def wait_until(predicate: Callable[[], bool], timeout=10.0, interval=0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def terminate(proc: subprocess.Popen, timeout=10.0):
    '''Stops a tool that runs until interrupted and returns its (stdout, stderr).

    The tools install a SIGTERM handler that sets their stop event, so terminate() is a
    clean shutdown rather than a kill.
    '''
    proc.terminate()
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(f'Process did not exit after SIGTERM. stderr:\n{stderr!r}')
