import functools
import os
import sys
import time
from importlib.resources import files

import valkey
from PIL import Image, ImageDraw, ImageFont
from visionapi.sae_pb2 import Detection, SaeMessage
from visionlib.pipeline import ValkeyConsumer, get_raw_frame_data

from .common import (InternalMessageType, choose_stream, default_arg_parser,
                     determine_message_type, register_stop_handler)

# pygame prints a banner to stdout on import, which would corrupt the raw frames written with --stdout
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
import pygame

# Images are kept in BGR channel order (like the frames coming from the SAE), so colors are BGR, too.
# Pillow does not care about channel order, it just calls the image mode "RGB".
ANNOTATION_COLOR = (0, 0, 255)
DEFAULT_WINDOW_SIZE = (1280, 720)

previous_frame_timestamp = 0
args = None
stop_event = None

def swap_red_blue(image: Image.Image) -> Image.Image:
    c1, c2, c3 = image.split()
    return Image.merge('RGB', (c3, c2, c1))

def get_image(sae_msg: SaeMessage) -> Image.Image:
    if args.image_file is not None:
        try:
            return swap_red_blue(Image.open(args.image_file).convert('RGB'))
        except OSError as e:
            raise ValueError(f'Could not read image from file {args.image_file}') from e
    else:
        frame = get_raw_frame_data(sae_msg.frame)
        if frame is not None:
            return Image.fromarray(frame)
        else:
            # If no frame is available, return a grey image as a last resort
            return Image.new('RGB', (sae_msg.frame.shape.width, sae_msg.frame.shape.height), (127, 127, 127))

@functools.lru_cache
def get_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size)

def annotate(image: Image.Image, detection: Detection):
    bbox_x1 = int(detection.bounding_box.min_x * image.width)
    bbox_y1 = int(detection.bounding_box.min_y * image.height)
    bbox_x2 = int(detection.bounding_box.max_x * image.width)
    bbox_y2 = int(detection.bounding_box.max_y * image.height)

    class_id = detection.class_id
    conf = detection.confidence

    label = f'{class_id} - {round(conf,2)}'

    if detection.object_id is not None:
        object_id = detection.object_id.hex()[:4]
        label = f'ID {object_id} - {class_id} - {round(conf,2)}'

    line_width = max(round((image.width + image.height) / 2 * 0.002), 2)

    draw = ImageDraw.Draw(image)
    draw.rectangle((bbox_x1, bbox_y1, bbox_x2, bbox_y2), outline=ANNOTATION_COLOR, width=line_width)
    draw.text((bbox_x1, bbox_y1 - 10), label, fill=ANNOTATION_COLOR, font=get_font(line_width * 8), anchor='ls')

def showImage(stream_id, image: Image.Image):
    # When using fixed scale, resize the image before displaying
    if args.fixed_scale:
        new_size = (int(image.width * args.fixed_scale), int(image.height * args.fixed_scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    surface = pygame.image.frombytes(swap_red_blue(image).tobytes(), image.size, 'RGB')

    if not pygame.display.get_init():
        pygame.display.init()
        pygame.display.set_caption(stream_id)
        # Must be set before the window is created by set_mode()
        pygame.display.set_icon(pygame.image.load(files('sae_introspection') / 'assets' / 'icon.png'))

    window = pygame.display.get_surface()

    if args.fixed_scale:
        # Match the exact size of the scaled image
        if window is None or window.get_size() != image.size:
            window = pygame.display.set_mode(image.size)
        window.blit(surface, (0, 0))
    else:
        if window is None:
            window = pygame.display.set_mode(DEFAULT_WINDOW_SIZE, pygame.RESIZABLE)
        # Fit the image into the window, keeping its aspect ratio
        scale = min(window.get_width() / image.width, window.get_height() / image.height)
        target_size = (max(round(image.width * scale), 1), max(round(image.height * scale), 1))
        window.fill((0, 0, 0))
        window.blit(pygame.transform.smoothscale(surface, target_size),
                    ((window.get_width() - target_size[0]) // 2, (window.get_height() - target_size[1]) // 2))

    pygame.display.flip()

def handle_window_events():
    # Has to be called regularly (also while no frames arrive), otherwise the window becomes unresponsive
    if not pygame.display.get_init():
        return
    for event in pygame.event.get():
        if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_q):
            stop_event.set()

def handle_sae_message(sae_message_bytes, stream_key, show_image=True):
    global previous_frame_timestamp, args

    sae_msg = SaeMessage()
    sae_msg.ParseFromString(sae_message_bytes)

    frametime = sae_msg.frame.timestamp_utc_ms - previous_frame_timestamp
    previous_frame_timestamp = sae_msg.frame.timestamp_utc_ms

    log_line = f'E2E-Delay: {round(time.time() * 1000 - sae_msg.frame.timestamp_utc_ms): >8} ms, Display Frametime: {frametime: >5} ms'
    if sae_msg.HasField('metrics'):
        log_line += f', Detection: {sae_msg.metrics.detection_inference_time_us: >7} us, Tracking: {sae_msg.metrics.tracking_inference_time_us: >7} us'
    print(log_line, file=sys.stderr)

    image = get_image(sae_msg)

    for detection in sae_msg.detections:
        annotate(image, detection)
    
    if args.stdout:
        sys.stdout.buffer.write(image.tobytes())

    if show_image:
        showImage(stream_key, image)


def main():
    global args, stop_event

    arg_parser = default_arg_parser()
    arg_parser.add_argument('-s', '--stream', type=str)
    arg_parser.add_argument('-i', '--image-file', type=str, default=None, metavar='FILE')
    arg_parser.add_argument('-o', '--stdout', action='store_true', help='Output annotated raw frames to stdout (e.g. to pipe into ffmpeg)')
    arg_parser.add_argument('-f', '--fixed-scale', type=float, metavar='SCALE',
                           help='Display with fixed scaling factor and high-quality scaling (2=double size, 1=original size, 0.75=75%% size , 0.5=half size, etc.)')
    arg_parser.add_argument('-n', '--no-gui', action='store_true', help='Do not display a GUI window. Useful when only piping to stdout.')

    args = arg_parser.parse_args()

    if args.stdout and sys.stdout.isatty():
        print('Stdout is the terminal. Ignoring "stdout" option. Please redirect (e.g. into ffmpeg)', file=sys.stderr)
        args.stdout = False

    STREAM_KEY = args.stream
    VALKEY_HOST = args.valkey_host
    VALKEY_PORT = args.valkey_port

    if STREAM_KEY is None:
        valkey_client = valkey.Valkey(VALKEY_HOST, VALKEY_PORT)
        STREAM_KEY = choose_stream(valkey_client)

    stop_event = register_stop_handler()

    consume = ValkeyConsumer(VALKEY_HOST, VALKEY_PORT, [STREAM_KEY], block=200, start_at_head=args.start_at_head)

    with consume:
        for stream_key, proto_data in consume():
            handle_window_events()

            if stop_event.is_set():
                break

            if stream_key is None:
                continue

            if (msg_type := determine_message_type(proto_data)) != InternalMessageType.SAE:
                print(f'Detected message type on stream {stream_key} is {msg_type.name}. Only type SAE is supported.')
                exit(1)

            handle_sae_message(proto_data, stream_key, show_image=not args.no_gui)

    pygame.quit()

if __name__ == "__main__":
    main()
