import functools
import io
import sys
import time
from dataclasses import dataclass
from typing import TextIO

import numpy as np
import pybase64
import valkey
from PIL import Image
from turbojpeg import TurboJPEG
from visionapi.anomaly_pb2 import AnomalyMessage
from visionapi.sae_pb2 import SaeMessage
from visionlib.pipeline import ValkeyConsumer, get_raw_frame_data
from visionlib.saedump import MESSAGE_SEPARATOR, DumpMeta, Event, EventMeta

from .common import (InternalMessageType, choose_streams, default_arg_parser,
                     determine_message_type, register_stop_handler)

# Color of the placeholder frames that replace AnomalyMessage frames when removing frames
PLACEHOLDER_GREY = (127, 127, 127)

# Initialized lazily, as it needs libturbojpeg installed on the system, which is
# only required when actually downscaling frames
jpeg = None

def get_jpeg() -> TurboJPEG:
    global jpeg
    if jpeg is None:
        jpeg = TurboJPEG()
    return jpeg

def write_meta(file: TextIO, start_time: float, stream_keys: list[str]):
    meta = DumpMeta(
        start_time=start_time,
        recorded_streams=stream_keys
    )
    file.write(meta.model_dump_json())
    file.write(MESSAGE_SEPARATOR)

def write_event(file: TextIO, stream_key: str, proto_data: bytes):
    bytes_to_write = proto_data
    
    event = Event(
        meta=EventMeta(
            record_time=time.time(),
            source_stream=stream_key
        ),
        data_b64=pybase64.standard_b64encode(bytes_to_write)
    )

    file.write(event.model_dump_json())
    file.write(MESSAGE_SEPARATOR)

@dataclass(frozen=True)
class FrameOptions:
    '''How frames contained in recorded messages are to be modified. Removing frames takes precedence over downscaling.'''
    remove_frames: bool = False
    downscale_width: int = 0
    jpeg_quality: int = 85

    @property
    def downscale(self) -> bool:
        return not self.remove_frames and self.downscale_width > 0

    @property
    def modifies_frames(self) -> bool:
        return self.remove_frames or self.downscale

def process_message(msg_type: InternalMessageType, proto_data: bytes, opts: FrameOptions) -> bytes:
    '''Applies the frame options to a message. Message types without frames are returned unchanged.'''
    if not opts.modifies_frames:
        return proto_data

    match msg_type:
        case InternalMessageType.SAE:
            scale_width = opts.downscale_width if opts.downscale else 0
            return process_sae_message(proto_data, opts.remove_frames, scale_width, opts.jpeg_quality)
        case InternalMessageType.ANOMALY:
            return process_anomaly_message(proto_data, opts)
        case _:
            return proto_data

def process_sae_message(proto_data: bytes, is_remove_frame=False, scale_width=0, scale_quality=85) -> bytes:
    msg = SaeMessage()
    msg.ParseFromString(proto_data)

    if is_remove_frame:
        remove_frame(msg)

    if scale_width > 0:
        resize_frame(msg, scale_width, scale_quality)

    return msg.SerializeToString()

def remove_frame(msg: SaeMessage):
    msg.frame.ClearField('frame_data')
    msg.frame.ClearField('frame_data_jpeg')

def resize_frame(msg: SaeMessage, scale_width=0, quality=85):
    frame = get_raw_frame_data(msg.frame)

    if frame is None:
        return

    msg.frame.frame_data_jpeg = downscale_bgr_to_jpeg(frame, scale_width, quality)

def downscale_bgr_to_jpeg(frame: np.ndarray, scale_width: int, quality=85) -> bytes:
    original_height, original_width = frame.shape[:2]
    scale_factor = scale_width / original_width
    new_size = (scale_width, round(original_height * scale_factor))
    # Pillow ignores channel order, so the BGR frame stays BGR (as expected by TurboJPEG)
    frame = np.asarray(Image.fromarray(frame).resize(new_size, Image.Resampling.BOX))

    return get_jpeg().encode(frame, quality)

def process_anomaly_message(proto_data: bytes, opts: FrameOptions) -> bytes:
    '''An AnomalyMessage carries a clip of JPEG frames. Removing frames replaces each of them with a grey placeholder
    of the same size, so that the clip structure (frame count, timestamps, dimensions) is preserved.'''
    msg = AnomalyMessage()
    msg.ParseFromString(proto_data)

    for jpeg_frame in msg.jpeg_frames:
        if len(jpeg_frame.frame_data_jpeg) == 0:
            continue
        if opts.remove_frames:
            replace_with_placeholder(jpeg_frame)
        elif opts.downscale:
            frame = get_jpeg().decode(jpeg_frame.frame_data_jpeg)
            jpeg_frame.frame_data_jpeg = downscale_bgr_to_jpeg(frame, opts.downscale_width, opts.jpeg_quality)

    return msg.SerializeToString()

def replace_with_placeholder(jpeg_frame):
    try:
        # Only reads the JPEG header, the image data is not decoded
        size = Image.open(io.BytesIO(jpeg_frame.frame_data_jpeg)).size
    except OSError as e:
        print(f'Could not read JPEG header of anomaly frame (ts={jpeg_frame.timestamp_utc_ms}), leaving it unchanged: {e}', file=sys.stderr)
        return
    jpeg_frame.frame_data_jpeg = placeholder_jpeg(*size)

@functools.lru_cache
def placeholder_jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), PLACEHOLDER_GREY).save(buffer, format='JPEG')
    return buffer.getvalue()


def main():
    arg_parser = default_arg_parser()
    arg_parser.add_argument('-s', '--streams', type=str, nargs='*', metavar='STREAM')
    arg_parser.add_argument('-o', '--output-file', type=str, default=f'./{time.strftime("%Y-%m-%dT%H-%M-%S%z")}.saedump', metavar='FILE')
    arg_parser.add_argument('-t', '--time-limit', type='natural_timedelta', help='Stop recording after TIME_LIMIT (default "60s")', default='60s')
    arg_parser.add_argument('-r', '--remove-frame', action='store_true',
                            help='Remove frame data from messages (reduces size significantly). SaeMessage: frame data is removed. '
                                 'AnomalyMessage: frames are replaced by grey placeholders of the same size. Takes precedence over -d.')
    arg_parser.add_argument('-d', '--downscale-frames', default=0, type=int, help='Downscale frames (SaeMessage frame, AnomalyMessage frames) to given width (preserving aspect ratio)', metavar='WIDTH')
    arg_parser.add_argument('-q', '--downscale-jpeg-quality', default=85, type=int, help='JPEG quality for downscaling frames (0-100, sane values 80-95)', metavar='QUALITY')
    args = arg_parser.parse_args()

    STREAM_KEYS = args.streams
    VALKEY_HOST = args.valkey_host
    VALKEY_PORT = args.valkey_port

    frame_options = FrameOptions(
        remove_frames=args.remove_frame,
        downscale_width=args.downscale_frames,
        jpeg_quality=args.downscale_jpeg_quality,
    )
    if args.remove_frame and args.downscale_frames > 0:
        print('-d/-q have no effect together with -r', file=sys.stderr)

    if STREAM_KEYS is None:
        valkey_client = valkey.Valkey(VALKEY_HOST, VALKEY_PORT)
        STREAM_KEYS = choose_streams(valkey_client)

    print(f'Recording streams {STREAM_KEYS} for {args.time_limit} into {args.output_file}')

    stop_event = register_stop_handler()

    consume = ValkeyConsumer(VALKEY_HOST, VALKEY_PORT, STREAM_KEYS, block=200, start_at_head=args.start_at_head)

    start_time = time.time()

    with consume, open(args.output_file, 'x') as output_file:
        
        write_meta(output_file, start_time, STREAM_KEYS)

        for stream_key, proto_data in consume():
            if stop_event.is_set():
                break

            if time.time() - start_time > args.time_limit.total_seconds():
                print(f'Reached configured time limit of {args.time_limit}')
                break

            if stream_key is None:
                continue

            try:
                msg_type = determine_message_type(proto_data)
            except ValueError as e:
                # Unknown messages cannot be processed, but are still worth recording
                print(f'Recording message on {stream_key} unprocessed: {e}', file=sys.stderr)
            else:
                proto_data = process_message(msg_type, proto_data, frame_options)

            write_event(output_file, stream_key, proto_data)

if __name__ == "__main__":
    main()
