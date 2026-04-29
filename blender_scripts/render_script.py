"""
render_script.py — Blender-side rendering script.

Invoked as:
    blender -b file.blend --python render_script.py -- \
        --device-type CUDA --use-gpu true \
        --samples 512 --denoise true \
        --tile-size 256 --output-dir /path/to/output

Prints progress to stdout so blender_worker.py can parse it.
On completion prints:
    RENDER_COMPLETE:/path/to/output/render.png
On failure prints:
    RENDER_FAILED:error description
"""

import os
import sys
import traceback

# ── Parse arguments (everything after '--') ────────────────────────────────────
argv = sys.argv
try:
    sep_idx = argv.index("--")
    script_args = argv[sep_idx + 1 :]
except ValueError:
    script_args = []


def get_arg(name: str, default: str = "") -> str:
    """Return the value of --name from script_args."""
    try:
        idx = script_args.index(name)
        return script_args[idx + 1]
    except (ValueError, IndexError):
        return default


device_type     = get_arg("--device-type", "CPU")
use_gpu         = get_arg("--use-gpu", "false").lower() == "true"
samples_arg     = get_arg("--samples", "default")
denoise_arg     = get_arg("--denoise", "true").lower() == "true"
tile_size_arg   = get_arg("--tile-size", "default")
color_depth_arg = get_arg("--color-depth", "32")   # "16" or "32"
exr_codec_arg   = get_arg("--exr-codec",   "PIZ")  # "PIZ" or "ZIP"
output_dir      = get_arg("--output-dir",  "/tmp/blender_output")

os.makedirs(output_dir, exist_ok=True)

# ── Import bpy after arg parse (Blender provides it) ──────────────────────────
import bpy

try:
    scene = bpy.context.scene
    render = scene.render
    cycles = scene.cycles

    # ── Render engine ──────────────────────────────────────────────────────────
    render.engine = "CYCLES"

    # ── Device setup ──────────────────────────────────────────────────────────
    if use_gpu:
        cycles.device = "GPU"
        cycles_prefs = bpy.context.preferences.addons["cycles"].preferences
        cycles_prefs.compute_device_type = device_type
        cycles_prefs.refresh_devices()
        # Enable all GPU devices (disable CPU to maximise GPU usage)
        for dev in cycles_prefs.devices:
            dev.use = dev.type != "CPU"
        print(
            f"GPU rendering enabled — device type: {device_type}",
            flush=True,
        )
    else:
        cycles.device = "CPU"
        print("CPU rendering enabled", flush=True)

    # ── Samples ───────────────────────────────────────────────────────────────
    if samples_arg != "default":
        cycles.samples = int(samples_arg)
        print(f"Samples set to: {cycles.samples}", flush=True)
    else:
        print(f"Using scene samples: {cycles.samples}", flush=True)

    # ── Denoising ─────────────────────────────────────────────────────────────
    cycles.use_denoising = denoise_arg
    print(f"Denoising: {cycles.use_denoising}", flush=True)

    # ── Tile size ─────────────────────────────────────────────────────────────
    if tile_size_arg != "default":
        try:
            ts = int(tile_size_arg)
            cycles.tile_size = ts
            print(f"Tile size set to: {ts}", flush=True)
        except (AttributeError, TypeError):
            # Older Blender — use render.tile_x/y
            try:
                ts = int(tile_size_arg)
                render.tile_x = ts
                render.tile_y = ts
                print(f"Tile size (render) set to: {ts}", flush=True)
            except Exception:
                pass

    # ── Output path ───────────────────────────────────────────────────────────
    output_path = os.path.join(output_dir, "render")
    render.filepath = output_path
    # Save as OpenEXR for maximum dynamic range and color depth.
    # The user can convert to PNG/JPEG/etc. after baking via the bot.
    render.image_settings.file_format = "OPEN_EXR"
    render.image_settings.color_mode  = "RGBA"
    render.image_settings.color_depth = color_depth_arg   # "16" or "32"
    render.image_settings.exr_codec   = exr_codec_arg     # "PIZ", "ZIP", …
    print(f"Output: OpenEXR {color_depth_arg}-bit [{exr_codec_arg}] → {output_dir}", flush=True)

    # ── Register completion handlers ──────────────────────────────────────────
    _output_files = []

    def on_render_complete(scene, depsgraph=None):
        fp = bpy.path.abspath(scene.render.filepath)
        # Blender appends frame number + extension (e.g. render0001.exr)
        for ext in (".exr", ".png", ".jpg"):
            candidate = fp + ext
            if os.path.isfile(candidate):
                _output_files.append(candidate)
                return
        # Fallback: newest file in output_dir
        import glob
        files = sorted(
            glob.glob(os.path.join(output_dir, "render*")),
            key=os.path.getmtime,
        )
        if files:
            _output_files.append(files[-1])

    bpy.app.handlers.render_complete.append(on_render_complete)

    # ── Render ────────────────────────────────────────────────────────────────
    print(f"Starting render of frame {scene.frame_current}…", flush=True)
    bpy.ops.render.render(write_still=True)

    # Collect output
    if not _output_files:
        # Fallback: look for any file written in output_dir
        import glob
        files = sorted(
            glob.glob(os.path.join(output_dir, "*")),
            key=os.path.getmtime,
        )
        _output_files = [f for f in files if os.path.isfile(f)]

    if _output_files:
        paths_str = "|".join(_output_files)
        print(f"RENDER_COMPLETE:{paths_str}", flush=True)
    else:
        print(
            f"RENDER_COMPLETE:{os.path.join(output_dir, 'render.png')}",
            flush=True,
        )

except Exception as exc:
    tb = traceback.format_exc()
    print(f"RENDER_FAILED:{exc}\n{tb}", flush=True)
    sys.exit(1)
