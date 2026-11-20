'''sae-echo reading from a saedump file. No Valkey needed, so these run in the default test set.'''
import subprocess

import pybase64
import pytest
from helpers import FRAME_BYTES, cli, make_sae_msg, parse_json_stream, write_saedump

STREAM = 'objecttracker:test'
OTHER_STREAM = 'objecttracker:other'


def run_echo(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([cli('sae-echo'), *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def single_stream_dump(tmp_path):
    messages = [make_sae_msg(timestamp_utc_ms=1700000000000 + idx, num_detections=3) for idx in range(5)]
    return write_saedump(tmp_path / 'single.saedump', STREAM, messages), messages


@pytest.fixture
def two_stream_dump(tmp_path):
    messages = [
        make_sae_msg(source_id='a' if idx % 2 == 0 else 'b', timestamp_utc_ms=1700000000000 + idx)
        for idx in range(6)
    ]
    streams = [STREAM if idx % 2 == 0 else OTHER_STREAM for idx in range(6)]
    return write_saedump(tmp_path / 'two.saedump', STREAM, messages, message_streams=streams)


def test_echo_dump_outputs_all_messages(single_stream_dump):
    dump_file, messages = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM)
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert [int(d['frame']['timestampUtcMs']) for d in decoded] == [1700000000000 + idx for idx in range(5)]
    assert [len(d['detections']) for d in decoded] == [3] * 5
    assert all(not d['frame'].get('frameData') for d in decoded)
    assert 'Detected message type' in result.stderr


def test_echo_dump_object_id_is_hex(single_stream_dump):
    dump_file, messages = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM)
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert [[det['objectId'] for det in d['detections']] for d in decoded] == \
        [[det.object_id.hex() for det in msg.detections] for msg in messages]


def test_echo_dump_preserves_frame(single_stream_dump):
    dump_file, _ = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM, '-f')
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert all(len(pybase64.standard_b64decode(d['frame']['frameData'])) == FRAME_BYTES for d in decoded)


def test_echo_dump_filters_streams(two_stream_dump):
    result = run_echo('-d', str(two_stream_dump), '-s', OTHER_STREAM)
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert [d['frame']['sourceId'] for d in decoded] == ['b'] * 3


def test_echo_dump_multiple_streams(two_stream_dump):
    result = run_echo('-d', str(two_stream_dump), '-s', STREAM, OTHER_STREAM)
    assert result.returncode == 0, result.stderr

    assert [d['frame']['sourceId'] for d in parse_json_stream(result.stdout)] == ['a', 'b'] * 3


def test_echo_dump_max_messages(single_stream_dump):
    dump_file, _ = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM, '-n', '2')
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert [int(d['frame']['timestampUtcMs']) for d in decoded] == [1700000000000, 1700000000001]


def test_echo_dump_max_messages_counts_selected_streams_only(two_stream_dump):
    result = run_echo('-d', str(two_stream_dump), '-s', OTHER_STREAM, '-n', '2')
    assert result.returncode == 0, result.stderr

    decoded = parse_json_stream(result.stdout)
    assert [int(d['frame']['timestampUtcMs']) for d in decoded] == [1700000000001, 1700000000003]


@pytest.mark.parametrize('value', ['0', '-1', 'abc'])
def test_echo_rejects_invalid_max_messages(single_stream_dump, value):
    dump_file, _ = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM, '-n', value)
    assert result.returncode == 2
    assert '--max-messages' in result.stderr


def test_echo_dump_warns_about_missing_stream(two_stream_dump):
    result = run_echo('-d', str(two_stream_dump), '-s', STREAM, 'does:not-exist')
    assert result.returncode == 0, result.stderr

    assert 'does:not-exist is not contained' in result.stderr
    assert len(parse_json_stream(result.stdout)) == 3


def test_echo_dump_fails_if_no_stream_matches(two_stream_dump):
    result = run_echo('-d', str(two_stream_dump), '-s', 'does:not-exist')
    assert result.returncode == 1
    assert result.stdout == ''


def test_echo_dump_ignores_start_at_head(single_stream_dump):
    dump_file, _ = single_stream_dump

    result = run_echo('-d', str(dump_file), '-s', STREAM, '--start-at-head')
    assert result.returncode == 0, result.stderr

    assert len(parse_json_stream(result.stdout)) == 5
    assert '--start-at-head has no effect' in result.stderr
