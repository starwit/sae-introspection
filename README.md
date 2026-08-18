# sae-introspection

Tools to look into a running [Starwit Awareness Engine](https://github.com/starwit/starwit-awareness-engine) (SAE) instance: watch, record, play back and inspect the messages flowing through its Redis/Valkey streams.

# Installation

## As a user (recommended)

Install the tools into an isolated environment with [pipx](https://pipx.pypa.io):

```sh
pipx install git+https://github.com/starwit/sae-introspection.git
```

To pin a specific state, append the commit id: `...sae-introspection.git@a1b2c3d`.
Upgrade with `pipx upgrade sae-introspection`, remove with `pipx uninstall sae-introspection`.

This puts the following commands on your `PATH`:

| Command | Description |
| --- | --- |
| `sae-watch` | Render a stream's frames (with annotations) in a window or to stdout |
| `sae-record` | Record one or more streams into a `.saedump` file |
| `sae-play` | Play a `.saedump` file back into a pipeline |
| `sae-echo` | Echo messages to stdout as JSON |
| `sae-plot` | Plot object trajectories from a `.saedump` file onto an image |

### Prerequisites

- Python >= 3.10
- `libturbojpeg` on your OS (e.g. `apt install libturbojpeg`)

## For development

- Python >= 3.10, Poetry >= 2.0.0
- Install dependencies: `poetry install`
- Run a tool: `poetry run sae-watch` (or `poetry run python -m sae_introspection.watch`)

# Tools
These tools were originally made for `SaeMessages`, however, `sae-echo` supports more vision-api message types (see below).

## Visual Introspection (`sae-watch`)
`sae-watch` can be used to visually look into the data flows within the pipeline.
On a technical level, it attaches to a Redis stream of your choice and then tries to guess from the name prefix which stage output it has to decode.
It will then render (and annotate, if possible) every output object / proto it receives from Redis.
You can exit the program by pressing `q` in the video window or hitting Ctrl-C on the CLI.

### Create video from output
Find out the frame size and framerate, then run (replacing `-r 10` (fps) and `-s 3840x2160` (size in px) with the appropriate values):\
`sae-watch -o | ffmpeg -y -pix_fmt bgr24 -f rawvideo -r 10 -s 3840x2160 -i - -c:v libx264 -crf 25 out.mp4`\
You can increase the quality (and file size) by lowering the `crf` value (-6 approx. doubles the file size). Use with `-n`/`--no-gui` when using on a headless machine to suppress output window.

### Examples
- `sae-watch` displays a menu with all available streams for ease of use (and after selection renders content of that stream)
- `sae-watch --help` shows all available options
- `sae-watch -s objectdetector:video1` renders frames with detected objects (assuming that `objectdetector:*` contains outputs of the objectdetector stage, which is default)

### Caveats
- Data transfer from Redis and rendering will increase your system load by another few percent


## Pipeline Recording (`sae-record`)
`sae-record` provides a simple way to record messages from some or all Redis streams into a file, i.e. create a log of all pipeline activities / state.
See `sae-record --help` for how to use it. \
For creating longer recordings, the tool offers several options to control the file size, as JPEG frames are very big in comparison to efficient video codecs like H.264/H.265 and there are some inefficiencies regarding space in the saedump format. `-r` / `--remove-frame` removes frames from messages before writing them to the dump file. `-d` / `--downscale-frames` (with `-q` / `--downscale-jpeg-quality`) enables trading some quality loss for smaller file sizes.

### Examples
- `sae-record -s geomapper:StreamID -t 86400 -d 320 -q 90 -o output.saedump` records 24 hours of geomapper output, scaling down video frames to a width of 320px (at a quality of 90%)


## Pipeline Playback (`sae-play`)
`sae-play` plays back a pipeline log into a running pipeline (i.e. at least a running Redis instance). It'll read the log file it is given and play back all messages into the corresponding streams they were recorded from. The messages will be spaced exactly as they were recorded (i.e. a 5fps recording will be played back at the same speed). For many real-world test cases the option `-t` might be interesting, which enables rewriting the message timestamps to the present moment (while still preserving message cadence).
See `sae-play --help` for how to use it.


## JSON Output (`sae-echo`)
`sae-echo` echoes all messages it receives into stdout as a JSON string (output of protobufs `MessageToJSON()`), everything else goes to stderr. It currently supports `SaeMessage`, `DetectionCountMessage` and `PositionMessage` - the message type on the chosen stream is autodetected (either using the type field or if that is not set a rather crude heuristic is used). All selected streams must carry the same message type. For `SaeMessage` payloads frame data is removed by default as to not clutter the output.\
`sae-echo` can be very useful when combined with other tools like jq. For example, to print the source id, frame timestamp and number of detections for each received message: `sae-echo | jq -r '[.frame.sourceId, .frame.timestampUtcMs, (.detections | length)] | @tsv'`


## Plot SAE Dump (`sae-plot`)
`sae-plot` reads a SAE dump file and plots contained object trajectories onto an existing image file or a grey background if no image is provided.\
Example usage: `sae-plot -i image.png dump_file.saedump`


# Case study: composing the tools

Because every tool is a plain CLI reading from / writing to stdio, they compose well with standard Unix tooling. [`scripts/save_frames.sh`](scripts/save_frames.sh) is an example of that: it pulls live frames out of a running SAE and writes them out as a de-duplicated set of JPEG files with all time-related metadata stripped (useful e.g. for building a test image corpus).

The core of it is a single pipeline:

```sh
sae-echo -f | jq -r .frame.frameDataJpeg
```

`sae-echo -f` keeps the frame data in the JSON output, `jq` picks the base64-encoded JPEG out of each message, and the loop in the script then base64-decodes it, strips metadata with `jpegtran`, and names each file by its SHA-256 sum so identical frames collapse into one file.

Usage: `./scripts/save_frames.sh OUTPUT_DIR` (needs `jq`, `jpegtran` from libjpeg-turbo-progs, and `coreutils`).
