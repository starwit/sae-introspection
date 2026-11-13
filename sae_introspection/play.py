import sys
import time
from datetime import timedelta

import pybase64
from visionapi.analytics_pb2 import DetectionCountMessage
from visionapi.anomaly_pb2 import AnomalyMessage
from visionapi.sae_pb2 import EventMessage, PositionMessage, SaeMessage
from visionlib.pipeline import ValkeyPublisher
from visionlib.saedump import DumpMeta, Event, message_splitter

from .common import (InternalMessageType, default_arg_parser,
                     determine_message_type, register_stop_handler)


def time_until_record_time(playback_start_time: float, record_start_time: float, record_target_time: float):
    current_time = time.time()
    playback_delta = current_time - playback_start_time
    target_delta = record_target_time - record_start_time
    return max(0, target_delta - playback_delta)

def time_until_interval(prev_message_time: float, target_interval: timedelta):
    current_time = time.time()
    target_time = prev_message_time + target_interval.total_seconds()
    sleep_time = target_time - current_time
    return max(0, sleep_time)

def now_ms() -> int:
    return time.time_ns() // 1000000

# Message types for which a "no timestamp adjustment" warning was already printed
_warned_types = set()

def adjust_timestamps_to_now(proto_bytes: bytes) -> bytes:
    '''Rewrites the timestamps of a message to the present moment.
    Messages with a single timestamp get it set to now. An AnomalyMessage (a clip spanning a time window) has all its
    timestamps shifted by the same offset, so that its latest timestamp is now and the relative timing is preserved.
    Messages of unknown type are returned unchanged.'''
    reason = 'no timestamp handling for this message type'
    try:
        msg_type = determine_message_type(proto_bytes)
    except ValueError as e:
        msg_type = None
        reason = str(e)

    match msg_type:
        case InternalMessageType.SAE:
            msg = SaeMessage()
            msg.ParseFromString(proto_bytes)
            msg.frame.timestamp_utc_ms = now_ms()
            return msg.SerializeToString()
        case InternalMessageType.POSITION:
            msg = PositionMessage()
            msg.ParseFromString(proto_bytes)
            msg.timestamp_utc_ms = now_ms()
            return msg.SerializeToString()
        case InternalMessageType.DETECTION_COUNT:
            msg = DetectionCountMessage()
            msg.ParseFromString(proto_bytes)
            msg.timestamp_utc_ms = now_ms()
            return msg.SerializeToString()
        case InternalMessageType.SAE_EVENT:
            msg = EventMessage()
            msg.ParseFromString(proto_bytes)
            msg.timestamp_utc_ms = now_ms()
            return msg.SerializeToString()
        case InternalMessageType.ANOMALY:
            msg = AnomalyMessage()
            msg.ParseFromString(proto_bytes)
            shift_anomaly_timestamps_to_now(msg)
            return msg.SerializeToString()
        case _:
            if msg_type not in _warned_types:
                _warned_types.add(msg_type)
                print(f'Cannot adjust timestamps of message, playing it back unchanged ({reason})', file=sys.stderr)
            return proto_bytes

def shift_anomaly_timestamps_to_now(msg: AnomalyMessage):
    timestamped = [*msg.jpeg_frames, *(point for traj in msg.trajectories for point in traj.trajectory_points)]
    if len(timestamped) == 0:
        return

    offset = now_ms() - max(item.timestamp_utc_ms for item in timestamped)
    for item in timestamped:
        item.timestamp_utc_ms += offset


def main():
    arg_parser = default_arg_parser()
    arg_parser.add_argument('dumpfile')
    arg_parser.add_argument('-l', '--loop', action='store_true', help='Loop indefinitely (exit with Ctrl-C)')
    arg_parser.add_argument('-t', '--adjust-timestamps', action='store_true', help='Adjust message timestamps to the time in the moment of playback. '
                            'AnomalyMessage clips are shifted as a whole (latest timestamp = now), preserving their relative timing')
    arg_parser.add_argument('-i', '--fixed-interval', type='natural_timedelta', help='Ignore embedded timestamp and instead output messages at the given interval (natural timedelta)', metavar='INTERVAL')
    args = arg_parser.parse_args()

    VALKEY_HOST = args.valkey_host
    VALKEY_PORT = args.valkey_port

    stop_event = register_stop_handler()

    publish = ValkeyPublisher(VALKEY_HOST, VALKEY_PORT)

    with publish, open(args.dumpfile, 'r') as input_file:
        while not stop_event.is_set():
            message_iter = message_splitter(input_file)

            playback_start_ts = time.time()
            prev_message_ts = 0
            start_message = next(message_iter)
            dump_meta = DumpMeta.model_validate_json(start_message)
            print(f'Starting playback from file {args.dumpfile} containing streams {dump_meta.recorded_streams}')

            for message in message_iter:
                event = Event.model_validate_json(message)
                proto_bytes = pybase64.standard_b64decode(event.data_b64)

                if args.fixed_interval is not None:
                    stop_event.wait(time_until_interval(prev_message_ts, args.fixed_interval))
                else:
                    stop_event.wait(time_until_record_time(playback_start_ts, dump_meta.start_time, event.meta.record_time))

                if args.adjust_timestamps:
                    proto_bytes = adjust_timestamps_to_now(proto_bytes)

                publish(event.meta.source_stream, proto_bytes)

                prev_message_ts = time.time()

                if stop_event.is_set():
                    break
            
            if not args.loop:
                break
            else:
                input_file.seek(0)


if __name__ == "__main__":
    main()
