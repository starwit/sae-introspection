import io

import numpy as np
import pytest
from helpers import FRAME_HEIGHT, FRAME_WIDTH, make_anomaly_msg, make_jpeg, make_sae_msg
from PIL import Image
from turbojpeg import TurboJPEG
from visionapi.anomaly_pb2 import AnomalyMessage
from visionapi.common_pb2 import MessageType
from visionapi.sae_pb2 import PositionMessage, SaeMessage

from sae_introspection.common import InternalMessageType
from sae_introspection.record import (PLACEHOLDER_GREY, FrameOptions,
                                      process_message, resize_frame)


def parse_anomaly(data: bytes) -> AnomalyMessage:
    msg = AnomalyMessage()
    msg.ParseFromString(data)
    return msg


def decode(jpeg_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg_bytes)).convert('RGB')


def large_anomaly_msg() -> AnomalyMessage:
    msg = make_anomaly_msg()
    for frame in msg.jpeg_frames:
        frame.frame_data_jpeg = make_jpeg(64, 32)
    return msg


# ---------------------------------------------------------------------------
# FrameOptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('opts,downscale,modifies', [
    (FrameOptions(), False, False),
    (FrameOptions(remove_frames=True), False, True),
    (FrameOptions(downscale_width=16), True, True),
    (FrameOptions(remove_frames=True, downscale_width=16), False, True),
])
def test_frame_options(opts, downscale, modifies):
    '''Removing frames takes precedence over downscaling.'''
    assert opts.downscale is downscale
    assert opts.modifies_frames is modifies


# ---------------------------------------------------------------------------
# process_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('msg_type,msg', [
    (InternalMessageType.SAE, make_sae_msg()),
    (InternalMessageType.ANOMALY, make_anomaly_msg()),
])
def test_no_options_is_passthrough(msg_type, msg):
    data = msg.SerializeToString()
    assert process_message(msg_type, data, FrameOptions()) is data


@pytest.mark.parametrize('opts', [FrameOptions(remove_frames=True), FrameOptions(downscale_width=4)])
def test_messages_without_frames_pass_through(opts):
    msg = PositionMessage()
    msg.type = MessageType.POSITION
    msg.timestamp_utc_ms = 1700000000000
    data = msg.SerializeToString()

    assert process_message(InternalMessageType.POSITION, data, opts) is data


def test_sae_remove_frame_clears_frame_fields():
    '''Unchanged behavior: SaeMessage frames are removed, not replaced.'''
    result = SaeMessage()
    result.ParseFromString(process_message(InternalMessageType.SAE, make_sae_msg().SerializeToString(),
                                           FrameOptions(remove_frames=True)))

    assert result.frame.frame_data == b''
    assert result.frame.frame_data_jpeg == b''
    assert len(result.detections) == 2


@pytest.mark.parametrize('opts', [FrameOptions(remove_frames=True), FrameOptions(remove_frames=True, downscale_width=16)])
def test_anomaly_remove_frame_inserts_grey_placeholders(opts):
    original = large_anomaly_msg()
    result = parse_anomaly(process_message(InternalMessageType.ANOMALY, original.SerializeToString(), opts))

    assert len(result.jpeg_frames) == len(original.jpeg_frames)
    for res_frame, orig_frame in zip(result.jpeg_frames, original.jpeg_frames):
        assert res_frame.timestamp_utc_ms == orig_frame.timestamp_utc_ms
        image = decode(res_frame.frame_data_jpeg)
        # Original size, even if -d is given as well
        assert image.size == (64, 32)
        assert np.all(np.abs(np.asarray(image, dtype=int) - PLACEHOLDER_GREY) <= 2)
    assert result.trajectories == original.trajectories
    assert result.source_id == original.source_id


def test_anomaly_remove_frame_keeps_unreadable_frame():
    original = make_anomaly_msg(num_frames=1)
    original.jpeg_frames[0].frame_data_jpeg = b'not a jpeg'

    result = parse_anomaly(process_message(InternalMessageType.ANOMALY, original.SerializeToString(),
                                           FrameOptions(remove_frames=True)))

    assert result.jpeg_frames[0].frame_data_jpeg == b'not a jpeg'


def test_anomaly_downscale():
    original = large_anomaly_msg()
    result = parse_anomaly(process_message(InternalMessageType.ANOMALY, original.SerializeToString(),
                                           FrameOptions(downscale_width=16, jpeg_quality=90)))

    assert [f.timestamp_utc_ms for f in result.jpeg_frames] == [f.timestamp_utc_ms for f in original.jpeg_frames]
    assert all(decode(f.frame_data_jpeg).size == (16, 8) for f in result.jpeg_frames)
    assert result.trajectories == original.trajectories


def test_resize_frame_output_is_unchanged():
    '''Guards the downscale refactoring: SaeMessage output must equal the original implementation's.'''
    msg = make_sae_msg()
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    msg.frame.frame_data = frame.tobytes()

    # Original implementation of resize_frame()
    new_size = (4, round(FRAME_HEIGHT * 4 / FRAME_WIDTH))
    expected = TurboJPEG().encode(np.asarray(Image.fromarray(frame).resize(new_size, Image.Resampling.BOX)), 90)

    resize_frame(msg, 4, 90)

    assert msg.frame.frame_data_jpeg == expected
