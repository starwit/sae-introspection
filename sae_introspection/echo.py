import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import valkey
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from visionapi.analytics_pb2 import DetectionCountMessage
from visionapi.sae_pb2 import EventMessage, PositionMessage, SaeMessage
from visionlib.pipeline import ValkeyConsumer

from .common import (InternalMessageType, choose_streams,
                     choose_streams_from_list, default_arg_parser,
                     determine_message_type, get_contained_streams,
                     iter_dump_events, register_stop_handler)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'")
    if number < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, got {number}')
    return number

def to_json(msg: Message) -> str:
    '''Single-line JSON (one message per line, i.e. JSON Lines) of MessageToDict()'s output, with ID bytes fields as hex instead of base64.'''
    msg_dict = MessageToDict(msg, always_print_fields_with_no_presence=True)

    if isinstance(msg, SaeMessage):
        for det, det_dict in zip(msg.detections, msg_dict.get('detections', [])):
            det_dict['objectId'] = det.object_id.hex()
    elif isinstance(msg, (PositionMessage, DetectionCountMessage)):
        msg_dict['saeUuid'] = msg.sae_uuid.hex()

    return json.dumps(msg_dict, separators=(',', ':'), ensure_ascii=False)

def handle_sae_message(message_bytes: bytes, preserve_frame=False):
    msg = SaeMessage()
    msg.ParseFromString(message_bytes)

    if not preserve_frame:
        msg.frame.ClearField('frame_data')
        msg.frame.ClearField('frame_data_jpeg')

    print(to_json(msg), flush=True)

def handle_generic_message(message_bytes: bytes, msg: Message) -> None:
    msg.ParseFromString(message_bytes)

    print(to_json(msg), flush=True)

def select_dump_streams(dump_file: Path, requested_streams):
    contained_streams = get_contained_streams(dump_file)

    if requested_streams is None:
        return choose_streams_from_list(contained_streams)

    missing_streams = [s for s in requested_streams if s not in contained_streams]
    for stream in missing_streams:
        print(f'Stream {stream} is not contained in {dump_file} (contains {contained_streams}).', file=sys.stderr)
    if len(missing_streams) == len(requested_streams):
        print('None of the requested streams is contained in the dump file. Exiting.', file=sys.stderr)
        exit(1)

    return requested_streams

def main():
    arg_parser = default_arg_parser()
    arg_parser.add_argument('-s', '--streams', type=str, nargs='*', metavar='STREAM')
    arg_parser.add_argument('-f', '--preserve-frame', action='store_true', help='Do not remove frame data (WARNING: large output!)')
    arg_parser.add_argument('-d', '--dump-file', type=Path, metavar='FILE',
                            help='Read messages from a saedump file instead of Valkey (Valkey options are ignored)')
    arg_parser.add_argument('-n', '--max-messages', type=_positive_int, metavar='N',
                            help='Exit after echoing N messages (default: unlimited)')
    args = arg_parser.parse_args()

    STREAM_KEYS = args.streams
    VALKEY_HOST = args.valkey_host
    VALKEY_PORT = args.valkey_port

    if args.dump_file is not None:
        if args.start_at_head:
            print('--start-at-head has no effect when reading a dump file. Ignoring.', file=sys.stderr)
        STREAM_KEYS = select_dump_streams(args.dump_file, STREAM_KEYS)
    elif STREAM_KEYS is None:
        valkey_client = valkey.Valkey(VALKEY_HOST, VALKEY_PORT)
        STREAM_KEYS = choose_streams(valkey_client)

    stop_event = register_stop_handler()

    if args.dump_file is not None:
        source_context = nullcontext(lambda: iter_dump_events(args.dump_file, STREAM_KEYS))
    else:
        source_context = ValkeyConsumer(VALKEY_HOST, VALKEY_PORT, STREAM_KEYS, block=200, start_at_head=args.start_at_head)

    message_type: InternalMessageType = None
    message_count = 0

    with source_context as consume:
        for stream_key, proto_data in consume():
            if stop_event.is_set():
                break

            if stream_key is None:
                continue

            if message_type is None:
                message_type = determine_message_type(proto_data)
                print(f'Detected message type {message_type} on stream.', file=sys.stderr)


            if message_type == InternalMessageType.SAE:
                handle_sae_message(proto_data, args.preserve_frame)
            elif message_type == InternalMessageType.POSITION:
                handle_generic_message(proto_data, PositionMessage())
            elif message_type == InternalMessageType.DETECTION_COUNT:
                handle_generic_message(proto_data, DetectionCountMessage())
            elif message_type == InternalMessageType.SAE_EVENT:
                handle_generic_message(proto_data, EventMessage())

            message_count += 1
            if args.max_messages is not None and message_count >= args.max_messages:
                break


if __name__ == "__main__":
    main()
