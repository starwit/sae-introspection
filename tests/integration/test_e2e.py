import json
import select
import subprocess
import time

import pybase64
import pytest
from helpers import (FRAME_BYTES, cli, make_sae_msg, run_cli, terminate,
                     wait_until, write_saedump)
from visionapi.sae_pb2 import SaeMessage
from visionlib.pipeline import ValkeyPublisher
from visionlib.saedump import DumpMeta, Event, message_splitter

pytestmark = pytest.mark.integration

STREAM = 'objecttracker:test'


def publish(valkey_container, stream_key, messages):
    '''Publishes via ValkeyPublisher, so the wire format matches what the tools consume.'''
    with ValkeyPublisher(host=valkey_container.get_container_host_ip(),
                         port=valkey_container.get_exposed_port(6379)) as publisher:
        for msg in messages:
            publisher(stream_key, msg.SerializeToString())


def read_stream(valkey_client, stream_key):
    '''Reads a stream back as deserialized SaeMessages.'''
    result = valkey_client.xread({stream_key: '0'}, count=100)
    if not result:
        return []

    messages = []
    for _, entries in result:
        for _, fields in entries:
            proto_bytes = pybase64.standard_b64decode(fields[b'proto_data_b64'])
            msg = SaeMessage()
            msg.ParseFromString(proto_bytes)
            messages.append(msg)
    return messages


def read_saedump(path):
    '''Returns (DumpMeta, [(source_stream, SaeMessage)]) from a dump file.'''
    with open(path, 'r') as file:
        message_iter = message_splitter(file)
        meta = DumpMeta.model_validate_json(next(message_iter))

        events = []
        for raw_event in message_iter:
            event = Event.model_validate_json(raw_event)
            msg = SaeMessage()
            msg.ParseFromString(pybase64.standard_b64decode(event.data_b64))
            events.append((event.meta.source_stream, msg))

    return meta, events


def read_json_objects(proc, count, timeout=30.0):
    '''Reads count JSON objects from a process' stdout.

    MessageToJson() pretty-prints, so a single object spans many lines and the objects
    arrive as one concatenated stream - hence raw_decode() rather than json.loads() per line.
    '''
    decoder = json.JSONDecoder()
    buffer = ''
    objects = []
    deadline = time.time() + timeout

    while len(objects) < count and time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        buffer += line

        while True:
            buffer = buffer.lstrip()
            if not buffer:
                break
            try:
                obj, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                break
            objects.append(obj)
            buffer = buffer[end:]

    assert len(objects) == count, f'Expected {count} JSON objects, got {len(objects)}'
    return objects


def test_echo_outputs_json(valkey_container, valkey_args):
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx, num_detections=3) for idx in range(3)]
    publish(valkey_container, STREAM, messages)

    proc = run_cli('sae-echo', *valkey_args, '-s', STREAM, '--start-at-head', text=True)
    try:
        decoded = read_json_objects(proc, 3)
    finally:
        stdout, stderr = terminate(proc)

    assert [d['frame']['sourceId'] for d in decoded] == ['test_source'] * 3
    assert [int(d['frame']['timestampUtcMs']) for d in decoded] == [1700000000000 + idx for idx in range(3)]
    assert [len(d['detections']) for d in decoded] == [3, 3, 3]

    # Frame data is stripped unless -f is given
    assert all(not d['frame'].get('frameData') for d in decoded)
    assert 'Detected message type' in stderr


def test_echo_preserves_frame_with_flag(valkey_container, valkey_args):
    publish(valkey_container, STREAM, [make_sae_msg()])

    proc = run_cli('sae-echo', *valkey_args, '-s', STREAM, '--start-at-head', '-f', text=True)
    try:
        decoded = read_json_objects(proc, 1)[0]
    finally:
        terminate(proc)

    assert len(pybase64.standard_b64decode(decoded['frame']['frameData'])) == FRAME_BYTES


def test_record_writes_dump(valkey_container, valkey_args, tmp_path):
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx) for idx in range(5)]
    publish(valkey_container, STREAM, messages)

    # record.py opens the output with mode 'x', so the path must not exist yet
    output_file = tmp_path / 'recording.saedump'
    result = subprocess.run(
        [cli('sae-record'), *valkey_args, '-s', STREAM, '--start-at-head', '-t', '5s', '-o', str(output_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f'sae-record failed:\n{result.stderr}'
    assert output_file.exists()

    meta, events = read_saedump(output_file)

    assert meta.recorded_streams == [STREAM]
    assert len(events) == 5
    assert all(stream == STREAM for stream, _ in events)
    assert [msg.frame.timestamp_utc_ms for _, msg in events] == [1700000000000 + idx for idx in range(5)]
    assert all(len(msg.frame.frame_data) == FRAME_BYTES for _, msg in events)


def test_record_removes_frames(valkey_container, valkey_args, tmp_path):
    publish(valkey_container, STREAM, [make_sae_msg() for _ in range(3)])

    output_file = tmp_path / 'no-frames.saedump'
    result = subprocess.run(
        [cli('sae-record'), *valkey_args, '-s', STREAM, '--start-at-head', '-t', '5s', '-r', '-o', str(output_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f'sae-record failed:\n{result.stderr}'

    _, events = read_saedump(output_file)

    assert len(events) == 3
    assert all(msg.frame.frame_data == b'' for _, msg in events)
    assert all(msg.frame.frame_data_jpeg == b'' for _, msg in events)
    # Everything except the frame payload survives
    assert all(len(msg.detections) == 2 for _, msg in events)


def test_watch_writes_raw_frames_to_stdout(valkey_container, valkey_args):
    publish(valkey_container, STREAM, [make_sae_msg() for _ in range(3)])

    proc = run_cli('sae-watch', *valkey_args, '-s', STREAM, '--start-at-head', '-n', '-o')
    try:
        assert wait_until(lambda: proc.poll() is not None or _readable(proc), timeout=30)
        time.sleep(1.0)
    finally:
        stdout, stderr = terminate(proc)

    # -n suppresses the GUI, -o writes the annotated frames as raw bgr24 bytes
    assert len(stdout) == 3 * FRAME_BYTES, f'stderr:\n{stderr.decode(errors="replace")}'
    assert b'E2E-Delay' in stderr


def test_play_publishes_dump(valkey_container, valkey_client, valkey_args, tmp_path):
    playback_stream = 'geomapper:playback'
    # ValkeyPublisher trims to stream_maxlen=10 by default, so stay at or below that
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx * 100) for idx in range(5)]
    dump_file = write_saedump(tmp_path / 'playback.saedump', playback_stream, messages)

    result = subprocess.run(
        [cli('sae-play'), *valkey_args, str(dump_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f'sae-play failed:\n{result.stderr}'

    played_back = read_stream(valkey_client, playback_stream)

    assert len(played_back) == 5
    assert [msg.frame.timestamp_utc_ms for msg in played_back] == [1700000000000 + idx * 100 for idx in range(5)]
    assert [msg.SerializeToString() for msg in played_back] == [msg.SerializeToString() for msg in messages]


def test_play_adjusts_timestamps(valkey_container, valkey_client, valkey_args, tmp_path):
    playback_stream = 'geomapper:adjusted'
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx * 100) for idx in range(3)]
    dump_file = write_saedump(tmp_path / 'adjusted.saedump', playback_stream, messages)

    before_ms = time.time() * 1000
    result = subprocess.run(
        [cli('sae-play'), *valkey_args, '-t', str(dump_file)],
        capture_output=True, text=True, timeout=120,
    )
    after_ms = time.time() * 1000
    assert result.returncode == 0, f'sae-play failed:\n{result.stderr}'

    played_back = read_stream(valkey_client, playback_stream)

    assert len(played_back) == 3
    for msg in played_back:
        assert before_ms <= msg.frame.timestamp_utc_ms <= after_ms


def test_record_then_play_round_trip(valkey_container, valkey_client, valkey_args, tmp_path):
    '''The full loop: capture a live stream to disk, then replay it byte for byte.'''
    source_stream = 'objecttracker:roundtrip'
    original = [make_sae_msg(source_id='roundtrip', timestamp_utc_ms=1700000000000 + idx * 40) for idx in range(5)]
    publish(valkey_container, source_stream, original)

    dump_file = tmp_path / 'roundtrip.saedump'
    record = subprocess.run(
        [cli('sae-record'), *valkey_args, '-s', source_stream, '--start-at-head', '-t', '5s', '-o', str(dump_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert record.returncode == 0, f'sae-record failed:\n{record.stderr}'

    valkey_client.delete(source_stream)
    assert read_stream(valkey_client, source_stream) == []

    play = subprocess.run(
        [cli('sae-play'), *valkey_args, str(dump_file)],
        capture_output=True, text=True, timeout=120,
    )
    assert play.returncode == 0, f'sae-play failed:\n{play.stderr}'

    replayed = read_stream(valkey_client, source_stream)

    assert [msg.SerializeToString() for msg in replayed] == [msg.SerializeToString() for msg in original]


def _readable(proc):
    '''True once the process has written something we could read.'''
    ready, _, _ = select.select([proc.stdout], [], [], 0)
    return bool(ready)
