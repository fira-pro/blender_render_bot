"""
render_script.py — Blender-side rendering script.

Invoked as:
    blender -b file.blend --python render_script.py -- \
        --device-type CUDA --use-gpu true \
        --samples 512 --denoise true \
        --tile-size 256 --output-dir /path/to/output \
        --frame-start 1 --frame-end 100 --frame-step 1 \
        --completed-frames "1,2,3"

Stdout markers (parsed by blender_worker.py):
    Fra:N Mem:… | … | Sample N/N          — live Cycles progress
    FRAME_COMPLETE:{frame_num}:{exr_path}:{preview_path}
    CHECKPOINT_STATE:{json}               — after each frame (worker throttles)
    RENDER_COMPLETE:{path}|{path}|…
    RENDER_FAILED:{description}
"""

import json as _json
import os
import sys
import traceback

# ── Parse arguments ────────────────────────────────────────────────────────────
argv = sys.argv
try:
    sep_idx = argv.index("--")
    script_args = argv[sep_idx + 1:]
except ValueError:
    script_args = []


def get_arg(name: str, default: str = "") -> str:
    try:
        idx = script_args.index(name)
        return script_args[idx + 1]
    except (ValueError, IndexError):
        return default


device_type     = get_arg("--device-type", "CPU")
use_gpu         = get_arg("--use-gpu",     "false").lower() == "true"
samples_arg     = get_arg("--samples",     "default")
denoise_arg     = get_arg("--denoise",     "true").lower() == "true"
tile_size_arg   = get_arg("--tile-size",   "default")
color_depth_arg = get_arg("--color-depth", "32")
exr_codec_arg   = get_arg("--exr-codec",   "PIZ")
output_dir      = get_arg("--output-dir",  "/tmp/blender_output")

# ── Frame range args ───────────────────────────────────────────────────────────
# Defaults resolved after bpy is imported (we need scene.frame_*)
frame_start_arg = get_arg("--frame-start", "")
frame_end_arg   = get_arg("--frame-end",   "")
frame_step_arg  = get_arg("--frame-step",  "1")
# Comma-separated frame numbers already rendered (skip on resume)
completed_frames_arg = get_arg("--completed-frames", "")

os.makedirs(output_dir, exist_ok=True)

import bpy  # noqa: E402

try:
    scene  = bpy.context.scene
    render = scene.render
    cycles = scene.cycles

    # ── Render engine ──────────────────────────────────────────────────────────
    render.engine = "CYCLES"

    # ── Device ────────────────────────────────────────────────────────────────
    if use_gpu:
        cycles.device = "GPU"
        cycles_prefs  = bpy.context.preferences.addons["cycles"].preferences
        cycles_prefs.compute_device_type = device_type
        cycles_prefs.refresh_devices()
        for dev in cycles_prefs.devices:
            dev.use = dev.type != "CPU"
        print(f"GPU rendering — {device_type}", flush=True)
    else:
        cycles.device = "CPU"
        print("CPU rendering", flush=True)

    # ── Samples ───────────────────────────────────────────────────────────────
    if samples_arg != "default":
        cycles.samples = int(samples_arg)
    print(f"Samples: {cycles.samples}", flush=True)

    # ── Denoising ─────────────────────────────────────────────────────────────
    cycles.use_denoising = denoise_arg
    print(f"Denoising: {cycles.use_denoising}", flush=True)

    # ── Tile size ─────────────────────────────────────────────────────────────
    if tile_size_arg != "default":
        try:
            ts = int(tile_size_arg)
            cycles.tile_size = ts
        except (AttributeError, TypeError):
            try:
                render.tile_x = render.tile_y = int(tile_size_arg)
            except Exception:
                pass

    # ── Output format ─────────────────────────────────────────────────────────
    render.image_settings.file_format = "OPEN_EXR"
    render.image_settings.color_mode  = "RGBA"
    render.image_settings.color_depth = color_depth_arg
    render.image_settings.exr_codec   = exr_codec_arg
    print(f"Output: OpenEXR {color_depth_arg}-bit [{exr_codec_arg}] → {output_dir}", flush=True)

    # ── Frame range ───────────────────────────────────────────────────────────
    frame_start = int(frame_start_arg) if frame_start_arg else scene.frame_start
    frame_end   = int(frame_end_arg)   if frame_end_arg   else scene.frame_end
    frame_step  = int(frame_step_arg)  if frame_step_arg  else 1
    completed_frames = set(
        int(f.strip()) for f in completed_frames_arg.split(",") if f.strip()
    )

    frames_to_render = [
        f for f in range(frame_start, frame_end + 1, frame_step)
        if f not in completed_frames
    ]
    total_frames = len(frames_to_render)
    print(f"Frame range: {frame_start}–{frame_end} step {frame_step} "
          f"({total_frames} frame(s) to render, {len(completed_frames)} skipped)", flush=True)

    # ── Helper: save preview PNG with View Transform ───────────────────────────
    def save_frame_preview(frame_num: int) -> str:
        preview_path = os.path.join(output_dir, f"render_{frame_num:04d}_preview.png")
        try:
            rr = bpy.data.images.get("Render Result")
            if rr:
                ims = render.image_settings
                prev_fmt, prev_dep, prev_mode = ims.file_format, ims.color_depth, ims.color_mode
                try:
                    ims.file_format = "PNG"
                    ims.color_depth = "8"
                    ims.color_mode  = "RGBA"
                    rr.save_render(preview_path, scene=scene)
                finally:
                    ims.file_format = prev_fmt
                    ims.color_depth = prev_dep
                    ims.color_mode  = prev_mode
        except Exception as _e:
            print(f"  Preview PNG failed for frame {frame_num}: {_e}", flush=True)
            return ""
        return preview_path

    # ── Render loop ───────────────────────────────────────────────────────────
    all_output_files = []
    all_completed    = list(completed_frames)  # grows as frames finish

    for f_idx, frame_num in enumerate(frames_to_render, start=1):
        scene.frame_set(frame_num)
        render.filepath = os.path.join(output_dir, f"render_{frame_num:04d}")

        print(f"\n── Frame {frame_num} ({f_idx}/{total_frames}) ──", flush=True)

        # Track what Blender writes via the render_complete handler
        _frame_files: list = []

        def _on_frame_complete(sc, depsgraph=None, _fnum=frame_num, _buf=_frame_files):
            fp = bpy.path.abspath(sc.render.filepath)
            for ext in (".exr", ".png", ".jpg"):
                if os.path.isfile(fp + ext):
                    _buf.append(fp + ext)
                    return
            import glob as _glob
            candidates = sorted(
                _glob.glob(os.path.join(output_dir, f"render_{_fnum:04d}*")),
                key=os.path.getmtime,
            )
            if candidates:
                _buf.append(candidates[-1])

        bpy.app.handlers.render_complete.append(_on_frame_complete)
        try:
            bpy.ops.render.render(write_still=True)
        finally:
            bpy.app.handlers.render_complete.remove(_on_frame_complete)

        # Resolve output path
        if _frame_files:
            exr_path = _frame_files[0]
        else:
            # Fallback: look for the file by expected name
            for ext in (".exr", ".png"):
                candidate = os.path.join(output_dir, f"render_{frame_num:04d}{ext}")
                if os.path.isfile(candidate):
                    exr_path = candidate
                    break
            else:
                exr_path = ""

        preview_path = save_frame_preview(frame_num)
        all_output_files.append(exr_path)
        all_completed.append(frame_num)

        print(f"FRAME_COMPLETE:{frame_num}:{exr_path}:{preview_path}", flush=True)

        # Checkpoint state after each frame
        chk = {
            "operation":       "render",
            "frame_start":     frame_start,
            "frame_end":       frame_end,
            "frame_step":      frame_step,
            "completed_frames": all_completed,
        }
        print(f"CHECKPOINT_STATE:{_json.dumps(chk, separators=(',', ':'))}", flush=True)

    # ── Finish ────────────────────────────────────────────────────────────────
    valid_files = [p for p in all_output_files if p and os.path.isfile(p)]
    if valid_files:
        print(f"RENDER_COMPLETE:{'|'.join(valid_files)}", flush=True)
    else:
        print(f"RENDER_COMPLETE:{os.path.join(output_dir, 'render.exr')}", flush=True)

except Exception as exc:
    tb = traceback.format_exc()
    print(f"RENDER_FAILED:{exc}\n{tb}", flush=True)
    sys.exit(1)
