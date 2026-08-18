from datetime import timedelta

import pytest
from visionapi.analytics_pb2 import DetectionCountMessage
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import EventMessage, PositionMessage, SaeMessage

from sae_introspection.common import (InternalMessageType,
                                      check_legacy_detection_count_message,
                                      check_legacy_sae_message,
                                      default_arg_parser,
                                      determine_message_type)


def make_sae(with_type=True) -> bytes:
    msg = SaeMessage()
    if with_type:
        msg.type = MessageType.SAE
    msg.frame.source_id = 'test_source'
    msg.frame.timestamp_utc_ms = 1700000000000
    msg.frame.shape.width = 8
    msg.frame.shape.height = 8
    msg.frame.shape.channels = 3
    return msg.SerializeToString()


def make_detection_count(with_type=True) -> bytes:
    msg = DetectionCountMessage()
    if with_type:
        msg.type = MessageType.DETECTION_COUNT
    msg.timestamp_utc_ms = 1700000000000
    return msg.SerializeToString()


def make_position(with_type=True) -> bytes:
    msg = PositionMessage()
    if with_type:
        msg.type = MessageType.POSITION
    msg.timestamp_utc_ms = 1700000000000
    msg.geo_coordinate.latitude = 52.5
    msg.geo_coordinate.longitude = 13.4
    return msg.SerializeToString()


def make_event(with_type=True) -> bytes:
    msg = EventMessage()
    if with_type:
        msg.type = MessageType.SAE_EVENT
    msg.instance_id = 'test_instance'
    msg.timestamp_utc_ms = 1700000000000
    return msg.SerializeToString()


# ---------------------------------------------------------------------------
# default_arg_parser
#
# These tests pin the CLI contract shared by sae-echo/play/record/watch. If one
# of them fails, the change breaks scripts people have already written.
# ---------------------------------------------------------------------------


def test_defaults():
    args = default_arg_parser().parse_args([])

    assert args.valkey_host == 'localhost'
    assert args.valkey_port == 6379
    assert args.start_at_head is False


@pytest.mark.parametrize('flag', ['-h', '--valkey-host', '--redis-host'])
def test_host_flags(flag):
    '''-h is the host, not help - and --redis-host still works for existing scripts.'''
    args = default_arg_parser().parse_args([flag, 'valkey.example.com'])

    assert args.valkey_host == 'valkey.example.com'


@pytest.mark.parametrize('flag', ['-p', '--valkey-port', '--redis-port'])
def test_port_flags(flag):
    args = default_arg_parser().parse_args([flag, '16379'])

    assert args.valkey_port == 16379
    assert isinstance(args.valkey_port, int)


def test_start_at_head():
    assert default_arg_parser().parse_args(['--start-at-head']).start_at_head is True


def test_help_is_long_option_only():
    '''--help exits cleanly; -h must not be interpreted as help.'''
    with pytest.raises(SystemExit) as exc_info:
        default_arg_parser().parse_args(['--help'])

    assert exc_info.value.code == 0


def test_unknown_flag_is_rejected():
    with pytest.raises(SystemExit) as exc_info:
        default_arg_parser().parse_args(['--not-a-flag'])

    assert exc_info.value.code == 2


def test_natural_timedelta_type_is_registered():
    '''The 'natural_timedelta' type is registered by default_arg_parser and used by -t/-i.'''
    parser = default_arg_parser()
    parser.add_argument('-t', '--time-limit', type='natural_timedelta')

    assert parser.parse_args(['-t', '60s']).time_limit == timedelta(seconds=60)


def test_natural_timedelta_rejects_garbage():
    '''An unparseable duration has to become an argparse error, not a traceback.'''
    parser = default_arg_parser()
    parser.add_argument('-t', '--time-limit', type='natural_timedelta')

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(['-t', 'not-a-duration'])

    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# determine_message_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('factory,expected', [
    (make_sae, InternalMessageType.SAE),
    (make_detection_count, InternalMessageType.DETECTION_COUNT),
    (make_position, InternalMessageType.POSITION),
    (make_event, InternalMessageType.SAE_EVENT),
])
def test_type_field_is_authoritative(factory, expected):
    assert determine_message_type(factory()) == expected


def test_unsupported_type_is_rejected():
    '''ANOMALY and INCIDENT exist in the enum but none of the tools handle them.'''
    msg = SaeMessage()
    msg.type = MessageType.ANOMALY

    with pytest.raises(ValueError, match=f'Unsupported message type \\(type={MessageType.ANOMALY}\\)'):
        determine_message_type(msg.SerializeToString())


@pytest.mark.parametrize('data', [b'', SaeMessage().SerializeToString()])
def test_undeterminable_message_is_rejected(data):
    '''An empty message has no type field and satisfies no heuristic.'''
    with pytest.raises(ValueError, match='Unknown message type'):
        determine_message_type(data)


# ---------------------------------------------------------------------------
# Legacy messages
#
# Messages recorded before the type field existed are identified by heuristics
# on their mandatory fields instead.
# ---------------------------------------------------------------------------


def test_legacy_sae_message_is_detected():
    assert determine_message_type(make_sae(with_type=False)) == InternalMessageType.SAE


def test_legacy_detection_count_message_is_detected():
    assert determine_message_type(make_detection_count(with_type=False)) == InternalMessageType.DETECTION_COUNT


def test_legacy_position_message_is_misdetected():
    '''Known limitation, pinned so a change to the heuristics is noticed.

    PositionMessage and DetectionCountMessage both carry timestamp_utc_ms in field 1, and the
    DetectionCount heuristic checks nothing else - so a PositionMessage that predates the type
    field cannot be told apart from a DetectionCountMessage. Setting the type field avoids this.
    '''
    assert determine_message_type(make_position(with_type=False)) == InternalMessageType.DETECTION_COUNT


def test_legacy_sae_heuristic_accepts_complete_frame():
    assert check_legacy_sae_message(make_sae(with_type=False)) is True


@pytest.mark.parametrize('missing_field', ['source_id', 'timestamp_utc_ms', 'shape'])
def test_legacy_sae_heuristic_requires_mandatory_frame_fields(missing_field):
    msg = SaeMessage()
    if missing_field != 'source_id':
        msg.frame.source_id = 'test_source'
    if missing_field != 'timestamp_utc_ms':
        msg.frame.timestamp_utc_ms = 1700000000000
    if missing_field != 'shape':
        msg.frame.shape.width = 8

    assert check_legacy_sae_message(msg.SerializeToString()) is False


def test_legacy_detection_count_heuristic_requires_timestamp():
    assert check_legacy_detection_count_message(make_detection_count(with_type=False)) is True
    assert check_legacy_detection_count_message(DetectionCountMessage().SerializeToString()) is False
