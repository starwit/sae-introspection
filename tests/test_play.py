import time

import pytest
from helpers import make_anomaly_msg, make_sae_msg
from visionapi.analytics_pb2 import DetectionCountMessage
from visionapi.anomaly_pb2 import AnomalyMessage
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import EventMessage, PositionMessage, SaeMessage

from sae_introspection.play import adjust_timestamps_to_now

OLD_TS = 1700000000000


def now_ms():
    return time.time() * 1000


def single_timestamp_messages():
    position = PositionMessage(type=MessageType.POSITION, timestamp_utc_ms=OLD_TS)
    position.geo_coordinate.latitude = 52.5
    return [
        (PositionMessage, position, lambda m: m.timestamp_utc_ms),
        (DetectionCountMessage, DetectionCountMessage(type=MessageType.DETECTION_COUNT, timestamp_utc_ms=OLD_TS), lambda m: m.timestamp_utc_ms),
        (EventMessage, EventMessage(type=MessageType.SAE_EVENT, instance_id='x', timestamp_utc_ms=OLD_TS), lambda m: m.timestamp_utc_ms),
        (SaeMessage, make_sae_msg(timestamp_utc_ms=OLD_TS), lambda m: m.frame.timestamp_utc_ms),
    ]


@pytest.mark.parametrize('msg_cls,msg,get_ts', single_timestamp_messages())
def test_single_timestamp_is_set_to_now(msg_cls, msg, get_ts):
    before = now_ms()
    result = msg_cls()
    result.ParseFromString(adjust_timestamps_to_now(msg.SerializeToString()))
    after = now_ms()

    assert before - 1 <= get_ts(result) <= after + 1
    # Nothing but the timestamp changes
    result_ts_cleared, msg_ts_cleared = msg_cls(), msg_cls()
    result_ts_cleared.CopyFrom(result)
    msg_ts_cleared.CopyFrom(msg)
    for m in (result_ts_cleared, msg_ts_cleared):
        (m.frame if msg_cls is SaeMessage else m).timestamp_utc_ms = 0
    assert result_ts_cleared == msg_ts_cleared


def test_anomaly_timestamps_are_shifted_as_a_whole():
    original = make_anomaly_msg(start_ts_ms=OLD_TS, num_frames=4, frame_interval_ms=100)
    # Trajectory points may extend beyond the last frame
    original.trajectories[1].trajectory_points[-1].timestamp_utc_ms = OLD_TS + 500

    before = now_ms()
    result = AnomalyMessage()
    result.ParseFromString(adjust_timestamps_to_now(original.SerializeToString()))
    after = now_ms()

    def all_ts(msg):
        return [f.timestamp_utc_ms for f in msg.jpeg_frames] + \
               [p.timestamp_utc_ms for t in msg.trajectories for p in t.trajectory_points]

    offsets = {new - old for new, old in zip(all_ts(result), all_ts(original))}
    assert len(offsets) == 1, 'all timestamps must be shifted by the same offset'
    assert before - 1 <= max(all_ts(result)) <= after + 1


def test_anomaly_without_timestamps_is_unchanged():
    msg = AnomalyMessage(type=MessageType.ANOMALY, source_id='x')
    data = msg.SerializeToString()
    assert adjust_timestamps_to_now(data) == data


def test_unknown_type_is_passed_through(capsys):
    data = SaeMessage(type=MessageType.INCIDENT).SerializeToString()

    assert adjust_timestamps_to_now(data) == data
    assert adjust_timestamps_to_now(data) == data
    # Warned only once
    assert capsys.readouterr().err.count('Cannot adjust timestamps') == 1
