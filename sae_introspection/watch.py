import functools
import io
import os
import sys
import time
from importlib.resources import files

import valkey
from PIL import Image, ImageDraw, ImageFont
from visionapi.anomaly_pb2 import AnomalyMessage, Trajectory
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
ANOMALY_TRIGGER_COLOR = (0, 255, 255)
DEFAULT_WINDOW_SIZE = (1280, 720)
GREY = (127, 127, 127)
# Max. time between handling window events while waiting (keeps the window responsive)
WINDOW_EVENT_INTERVAL_S = 0.05

previous_frame_timestamp = 0
# Size of the first frame written to stdout. Raw video needs all frames to be the same size.
stdout_frame_size = None
stdout_resize_warned = False
args = None
stop_event = None

def swap_red_blue(image: Image.Image) -> Image.Image:
    c1, c2, c3 = image.split()
    return Image.merge('RGB', (c3, c2, c1))

def read_image_file() -> Image.Image:
    try:
        return swap_red_blue(Image.open(args.image_file).convert('RGB'))
    except OSError as e:
        raise ValueError(f'Could not read image from file {args.image_file}') from e

def get_image(sae_msg: SaeMessage) -> Image.Image:
    if args.image_file is not None:
        return read_image_file()
    else:
        frame = get_raw_frame_data(sae_msg.frame)
        if frame is not None:
            return Image.fromarray(frame)
        else:
            # If no frame is available, return a grey image as a last resort
            return Image.new('RGB', (sae_msg.frame.shape.width, sae_msg.frame.shape.height), GREY)

def get_anomaly_image(frame_data_jpeg: bytes, fallback_size) -> Image.Image:
    if args.image_file is not None:
        return read_image_file()
    if len(frame_data_jpeg) > 0:
        return swap_red_blue(Image.open(io.BytesIO(frame_data_jpeg)).convert('RGB'))
    # If no frame is available, return a grey image as a last resort
    return Image.new('RGB', fallback_size, GREY)

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

    line_width = get_line_width(image)

    draw = ImageDraw.Draw(image)
    draw.rectangle((bbox_x1, bbox_y1, bbox_x2, bbox_y2), outline=ANNOTATION_COLOR, width=line_width)
    draw.text((bbox_x1, bbox_y1 - 10), label, fill=ANNOTATION_COLOR, font=get_font(line_width * 8), anchor='ls')

def get_line_width(image: Image.Image) -> int:
    return max(round((image.width + image.height) / 2 * 0.002), 2)

def annotate_trajectory(image: Image.Image, trajectory: Trajectory, up_to_ts: int = None):
    '''Draws the trajectory up to (and including) the given timestamp, or entirely if None.
    Points that triggered the anomaly are highlighted.'''
    points = sorted(
        (p for p in trajectory.trajectory_points if up_to_ts is None or p.timestamp_utc_ms <= up_to_ts),
        key=lambda p: p.timestamp_utc_ms,
    )
    if len(points) == 0:
        return

    # Detection centers are normalized (like bounding boxes in SaeMessage)
    coords = [(p.detection_center.x * image.width, p.detection_center.y * image.height) for p in points]
    line_width = get_line_width(image)

    draw = ImageDraw.Draw(image)
    if len(coords) > 1:
        draw.line(coords, fill=ANNOTATION_COLOR, width=line_width, joint='curve')

    radius = line_width * 2
    for point, (x, y) in zip(points, coords):
        if point.anomaly_trigger:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=ANOMALY_TRIGGER_COLOR)

    label = f'{trajectory.class_id}'
    if len(trajectory.object_id) > 0:
        label = f'ID {trajectory.object_id.hex()[:4]} - {trajectory.class_id}'
    x, y = coords[-1]
    draw.text((x, y - 10), label, fill=ANNOTATION_COLOR, font=get_font(line_width * 8), anchor='ls')

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

def wait_responsive(seconds: float):
    '''Waits for the given time while keeping the window responsive. Returns early if a stop is requested.'''
    deadline = time.time() + seconds
    while not stop_event.is_set():
        handle_window_events()
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        stop_event.wait(min(remaining, WINDOW_EVENT_INTERVAL_S))

def emit_frame(image: Image.Image, stream_key: str, show_image: bool):
    global stdout_frame_size, stdout_resize_warned

    if args.stdout:
        out_image = image
        if stdout_frame_size is None:
            stdout_frame_size = image.size
        elif image.size != stdout_frame_size:
            if not stdout_resize_warned:
                print(f'Frame size {image.size} differs from first frame size {stdout_frame_size}. '
                      'Resizing differing frames for stdout output.', file=sys.stderr)
                stdout_resize_warned = True
            out_image = image.resize(stdout_frame_size, Image.Resampling.BILINEAR)
        sys.stdout.buffer.write(out_image.tobytes())

    if show_image:
        showImage(stream_key, image)

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

    emit_frame(image, stream_key, show_image)

def handle_anomaly_message(anomaly_message_bytes, stream_key, show_image=True):
    '''An AnomalyMessage carries a clip. Its frames are rendered with the trajectories progressing over time and,
    when displayed, paced as recorded. The last frame stays on screen until the next message arrives.'''
    msg = AnomalyMessage()
    msg.ParseFromString(anomaly_message_bytes)

    frames = sorted(msg.jpeg_frames, key=lambda f: f.timestamp_utc_ms)
    points = [p for t in msg.trajectories for p in t.trajectory_points]
    timestamps = [f.timestamp_utc_ms for f in frames] + [p.timestamp_utc_ms for p in points]

    log_line = (f'Anomaly from {msg.source_id or "<unknown source>"}: {len(frames)} frames, {len(msg.trajectories)} trajectories, '
                f'{sum(p.anomaly_trigger for p in points)} trigger points')
    if len(timestamps) > 0:
        log_line += f', Clip duration: {max(timestamps) - min(timestamps)} ms, Age: {round(time.time() * 1000 - max(timestamps))} ms'
    if msg.HasField('model_info'):
        log_line += f', Model: {msg.model_info.name} {msg.model_info.version}'
    print(log_line, file=sys.stderr)

    if len(frames) == 0:
        # Nothing to play back, show the complete trajectories at once
        image = get_anomaly_image(b'', DEFAULT_WINDOW_SIZE)
        for trajectory in msg.trajectories:
            annotate_trajectory(image, trajectory)
        emit_frame(image, stream_key, show_image)
        return

    clip_start = time.time()
    fallback_size = DEFAULT_WINDOW_SIZE
    for frame in frames:
        if show_image:
            # Pacing only matters for display, the content written to stdout is the same either way
            wait_responsive(clip_start + (frame.timestamp_utc_ms - frames[0].timestamp_utc_ms) / 1000 - time.time())
        if stop_event.is_set():
            return

        image = get_anomaly_image(frame.frame_data_jpeg, fallback_size)
        fallback_size = image.size

        for trajectory in msg.trajectories:
            annotate_trajectory(image, trajectory, frame.timestamp_utc_ms)

        emit_frame(image, stream_key, show_image)


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

            match determine_message_type(proto_data):
                case InternalMessageType.SAE:
                    handle_sae_message(proto_data, stream_key, show_image=not args.no_gui)
                case InternalMessageType.ANOMALY:
                    handle_anomaly_message(proto_data, stream_key, show_image=not args.no_gui)
                case msg_type:
                    print(f'Detected message type on stream {stream_key} is {msg_type.name}. Only types SAE and ANOMALY are supported.', file=sys.stderr)
                    exit(1)

    pygame.quit()

if __name__ == "__main__":
    main()
