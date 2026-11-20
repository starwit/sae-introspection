from argparse import ArgumentParser
from pathlib import Path
from typing import Dict, Generator, Tuple

import pybase64
import tqdm
from palettable.colorbrewer.qualitative import Set1_9
from PIL import Image, ImageDraw
from visionapi.sae_pb2 import SaeMessage
from visionlib.saedump import Event

from .common import (InternalMessageType, add_version_arg,
                     choose_stream_from_list, determine_message_type,
                     get_contained_streams, iter_sae_dump,
                     register_stop_handler)

CLS_CMAP = Set1_9.colors
ALPHA = 0.80

def iter_sae_messages(dump_file: Path, stream_id: str) -> Generator[SaeMessage, None, None]:
    event_iter = iter_sae_dump(dump_file)

    # Skip the DumpMeta message
    _ = next(event_iter)
    
    for json_event in event_iter:
        event = Event.model_validate_json(json_event)

        if event.meta.source_stream != stream_id:
            continue

        proto_bytes = pybase64.standard_b64decode(event.data_b64)
        sae_msg = SaeMessage()
        sae_msg.ParseFromString(proto_bytes)
        
        if (msg_type := determine_message_type(proto_bytes)) != InternalMessageType.SAE:
            raise ValueError(f'Found message type {msg_type}, SAE messages needed')
        
        yield sae_msg
        
def draw_line(img: Image.Image, pt1: Tuple[float], pt2: Tuple[float], class_id: int):
    ImageDraw.Draw(img).line([(int(pt1[0] * img.width), int(pt1[1] * img.height)), (int(pt2[0] * img.width), int(pt2[1] * img.height))], fill=get_color(class_id), width=1)

def get_color(class_id: int) -> Tuple[int]:
    return tuple(CLS_CMAP[class_id % len(CLS_CMAP)])


def main():
    arg_parser = ArgumentParser()
    add_version_arg(arg_parser)
    arg_parser.add_argument('dumpfile', type=Path, help='Path to SAE dump file to plot')
    arg_parser.add_argument('-i', '--image-file', help='Path to an image to plot trajectories on (grey background will be used if not specified)')
    args = arg_parser.parse_args()

    stop_event = register_stop_handler()

    streams = get_contained_streams(args.dumpfile)
    if len(streams) == 1:
        stream_id = streams[0]
    else:
        stream_id = choose_stream_from_list(streams)

    if args.image_file is not None:
        image = Image.open(args.image_file).convert('RGB')
    else:
        image = Image.new('RGB', (1920, 1080), (127, 127, 127))

    annotated_image = image.copy()

    output_file = args.dumpfile.parent / f'{args.dumpfile.stem}.jpg'

    previous_points: Dict[bytes, Tuple[float, float]] = {}

    print('Drawing trajectories from messages')

    for sae_msg in tqdm.tqdm(iter_sae_messages(args.dumpfile, stream_id)):
        if stop_event.is_set():
            break

        for det in sae_msg.detections:
            center_x = (det.bounding_box.max_x + det.bounding_box.min_x) / 2
            center_y = (det.bounding_box.max_y + det.bounding_box.min_y) / 2
            new_point = (center_x, center_y)
            obj_id = det.object_id

            if obj_id in previous_points:
                draw_line(annotated_image, pt1=previous_points[obj_id], pt2=new_point, class_id=det.class_id)

            previous_points[obj_id] = new_point

    
    output_image = Image.blend(image, annotated_image, ALPHA)

    output_image.save(output_file, quality=90)
    print(f'Output written to {output_file}')
        


if __name__ == "__main__":
    main()
