"""
bake_script.py — Blender-side texture baking script.

Invoked as:
    blender -b file.blend --python bake_script.py -- \
        --device-type CUDA --use-gpu true \
        --samples 512 --denoise true \
        --tile-size 256 \
        --bake-type COMBINED \
        --bake-target single \
        --output-dir /path/to/output

Resume / checkpoint args (populated from checkpoint.json on resume):
    --completed-images   "Albedo,Normal"         skip these image groups entirely
    --resume-image-name  "Roughness"             image currently being resumed
    --resume-image-path  "/path/Roughness_partial.exr"  partial EXR to load
    --resume-done-objects "Cube.001,Sphere"       objects already done in current image

Stdout markers (parsed by blender_worker.py):
    BAKE_PROGRESS:{done}/{total}:{obj_name}
    IMAGE_COMPLETE:{img_name}:{exr_path}:{preview_path}
    CHECKPOINT_STATE:{json}          — after each object bake (worker throttles uploads)
    BAKE_COMPLETE:{path}|{path}|…
    BAKE_FAILED:{description}
"""

import json as _json
import os
import sys
import traceback
from collections import OrderedDict

# ── Parse arguments ─────────────────────────────────────────────────────────
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


# ── Standard settings ────────────────────────────────────────────────────────
device_type  = get_arg("--device-type", "CPU")
use_gpu      = get_arg("--use-gpu", "false").lower() == "true"
samples_arg  = get_arg("--samples", "default")
denoise_arg  = get_arg("--denoise", "true").lower() == "true"
tile_arg     = get_arg("--tile-size", "default")
bake_type    = get_arg("--bake-type", "COMBINED")
bake_target  = get_arg("--bake-target", "single")
use_clear    = get_arg("--use-clear", "false").lower() == "true"
margin       = int(get_arg("--margin", "16"))
color_depth  = get_arg("--color-depth", "32")
exr_codec    = get_arg("--exr-codec",   "PIZ")
output_dir   = get_arg("--output-dir", "/tmp/blender_bake")

# ── Resume / checkpoint args ─────────────────────────────────────────────────
completed_images_arg  = get_arg("--completed-images",   "")
resume_image_name     = get_arg("--resume-image-name",  "")
resume_image_path     = get_arg("--resume-image-path",  "")
resume_done_objects   = get_arg("--resume-done-objects", "")

os.makedirs(output_dir, exist_ok=True)

import bpy  # noqa: E402 — must come after arg parse

# ── Helpers ──────────────────────────────────────────────────────────────────

def active_image_texture_node(material):
    """Return the active/selected ImageTexture node for a material, or None."""
    if not material or not material.use_nodes or not material.node_tree:
        return None
    tree = material.node_tree
    active = tree.nodes.active
    if active and active.type == "TEX_IMAGE" and active.image:
        return active
    for node in tree.nodes:
        if node.select and node.type == "TEX_IMAGE" and node.image:
            return node
    return None


def build_image_groups(bake_pairs):
    """
    Group (obj, mat, image) triples by image name.
    Groups and object lists within groups are sorted alphabetically so that
    the ordering is stable and predictable across resumed sessions.
    Returns OrderedDict[img_name, {"image": Image, "pairs": [(obj, mat)]}]
    """
    groups = OrderedDict()
    for obj, mat, image in bake_pairs:
        name = image.name
        if name not in groups:
            groups[name] = {"image": image, "pairs": []}
        groups[name]["pairs"].append((obj, mat))
    # Stable sort: groups by image name, pairs within each group by object name
    for data in groups.values():
        data["pairs"].sort(key=lambda x: x[0].name)
    return OrderedDict(sorted(groups.items()))


def load_partial_exr(image, exr_path: str) -> bool:
    """
    Copy pixel data from a partial EXR file into an existing image datablock.
    Returns True on success.
    """
    path = os.path.abspath(exr_path)
    if not os.path.isfile(path):
        print(f"  ⚠ Partial EXR not found: {path}", flush=True)
        return False
    tmp = bpy.data.images.load(path)
    try:
        if list(tmp.size) == list(image.size):
            image.pixels[:] = list(tmp.pixels)
            image.update()
            print(f"  Loaded partial EXR ({tmp.size[0]}×{tmp.size[1]}): {path}", flush=True)
            return True
        else:
            print(f"  ⚠ Partial EXR size {tmp.size[:]} ≠ image size {image.size[:]}, skipping load", flush=True)
            return False
    finally:
        bpy.data.images.remove(tmp)


def save_partial_exr(image, output_dir: str, img_name: str, color_depth: str) -> str:
    """Save current in-progress image state as a partial EXR for checkpointing."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in img_name)
    dest = os.path.join(output_dir, safe + "_partial.exr")
    image.filepath_raw       = dest
    image.file_format        = "OPEN_EXR"
    image.use_half_precision = (color_depth == "16")
    image.save()
    return dest


def save_exr_raw(image, output_dir: str, color_depth: str):
    """Save final raw OpenEXR (bypasses color management). Returns (path, base_name)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in image.name)
    base = os.path.splitext(safe)[0]
    dest = os.path.join(output_dir, base + ".exr")
    image.filepath_raw       = dest
    image.file_format        = "OPEN_EXR"
    image.use_half_precision = (color_depth == "16")
    image.save()
    return dest, base


def save_preview_png(image, output_dir: str, base_name: str, scene) -> str:
    """Save PNG with scene View Transform (Filmic/AgX) applied."""
    dest = os.path.join(output_dir, base_name + "_preview.png")
    ims = scene.render.image_settings
    prev_fmt, prev_dep, prev_mode = ims.file_format, ims.color_depth, ims.color_mode
    try:
        ims.file_format = "PNG"
        ims.color_depth = "8"
        ims.color_mode  = "RGBA"
        image.save_render(dest, scene=scene)
    finally:
        ims.file_format = prev_fmt
        ims.color_depth = prev_dep
        ims.color_mode  = prev_mode
    return dest


# ── Main bake logic ──────────────────────────────────────────────────────────
try:
    scene  = bpy.context.scene
    render = scene.render
    cycles = scene.cycles

    # ── Engine & device ───────────────────────────────────────────────────────
    render.engine = "CYCLES"
    if use_gpu:
        cycles.device = "GPU"
        cyc_prefs = bpy.context.preferences.addons["cycles"].preferences
        cyc_prefs.compute_device_type = device_type
        cyc_prefs.refresh_devices()
        for dev in cyc_prefs.devices:
            dev.use = dev.type != "CPU"
        print(f"GPU baking enabled — {device_type}", flush=True)
    else:
        cycles.device = "CPU"
        print("CPU baking enabled", flush=True)

    if samples_arg != "default":
        cycles.samples = int(samples_arg)
    cycles.use_denoising = denoise_arg

    if tile_arg != "default":
        try:
            cycles.tile_size = int(tile_arg)
        except (AttributeError, TypeError):
            try:
                render.tile_x = render.tile_y = int(tile_arg)
            except Exception:
                pass

    # ── Collect bake pairs ────────────────────────────────────────────────────
    bake_pairs = []
    for obj in bpy.context.view_layer.objects:
        if obj.type != "MESH":
            continue
        if obj.hide_get() or not obj.visible_get():
            continue
        for slot in obj.material_slots:
            mat  = slot.material
            node = active_image_texture_node(mat)
            if node is None:
                continue
            bake_pairs.append((obj, mat, node.image))

    if not bake_pairs:
        print("BAKE_FAILED:No mesh objects with an active ImageTexture node found.", flush=True)
        sys.exit(1)

    # ── Build image groups (stable sort) ──────────────────────────────────────
    image_groups = build_image_groups(bake_pairs)
    total_images = len(image_groups)

    # Full group map — stored in every checkpoint for resume arg reconstruction
    group_map = {
        iname: [o.name for o, _m in data["pairs"]]
        for iname, data in image_groups.items()
    }
    print(f"Image groups ({total_images}): {list(image_groups.keys())}", flush=True)

    # ── Apply resume state ────────────────────────────────────────────────────
    completed_images = [s.strip() for s in completed_images_arg.split(",") if s.strip()]
    resume_done_objs = [s.strip() for s in resume_done_objects.split(",")  if s.strip()]

    for cname in completed_images:
        if cname in image_groups:
            image_groups.pop(cname)
            print(f"Skipping completed image: {cname}", flush=True)

    if resume_image_name and resume_image_name in image_groups:
        grp = image_groups[resume_image_name]
        if resume_image_path:
            load_partial_exr(grp["image"], resume_image_path)
        grp["pairs"] = [
            (obj, mat) for obj, mat in grp["pairs"]
            if obj.name not in resume_done_objs
        ]
        print(
            f"Resuming '{resume_image_name}', remaining: "
            f"{[o.name for o, _ in grp['pairs']]}",
            flush=True,
        )

    # ── Bake loop ─────────────────────────────────────────────────────────────
    bpy.ops.object.select_all(action="DESELECT")
    saved_paths   = []
    all_completed = list(completed_images)   # grows as images finish this session
    global_done   = 0                        # objects baked this session (for BAKE_PROGRESS)
    global_total  = sum(len(d["pairs"]) for d in image_groups.values())

    for img_idx, (img_name, group_data) in enumerate(image_groups.items(), start=1):
        image     = group_data["image"]
        pairs     = group_data["pairs"]
        is_resume = (img_name == resume_image_name)

        # Objects already done in this image (from resume arg, for checkpoint JSON)
        done_in_group = list(resume_done_objs) if is_resume else []

        print(f"\n── Image {img_idx}/{total_images}: '{img_name}' "
              f"({len(pairs)} object(s) to bake) ──", flush=True)

        for pair_idx, (obj, mat) in enumerate(pairs, start=1):
            obj_name = obj.name
            print(f"BAKE_PROGRESS:{global_done}/{global_total}:{obj_name}", flush=True)
            print(f"  Baking '{bake_type}' → obj='{obj_name}' "
                  f"mat='{mat.name}' img='{img_name}'…", flush=True)

            # Select object & activate correct material + image node
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            for i, slot in enumerate(obj.material_slots):
                if slot.material == mat:
                    obj.active_material_index = i
                    break
            for node in mat.node_tree.nodes:
                node.select = False
            target_node = active_image_texture_node(mat)
            if target_node:
                target_node.select = True
                mat.node_tree.nodes.active = target_node

            # Only clear image on the very first bake of a fresh (non-resumed) image
            do_clear = use_clear and not is_resume and pair_idx == 1

            try:
                bpy.ops.object.bake(
                    type=bake_type,
                    use_clear=do_clear,
                    margin=margin,
                    use_selected_to_active=False,
                )
            except RuntimeError as exc:
                print(f"  ⚠ Bake failed for {obj_name}/{mat.name}: {exc}", flush=True)
                continue

            global_done    += 1
            is_resume       = False   # Only the first object in a resumed group is "resume"
            done_in_group.append(obj_name)
            print(f"BAKE_PROGRESS:{global_done}/{global_total}:{obj_name}", flush=True)

            # ── Emit checkpoint state after each object (worker throttles uploads) ──
            remaining_in_group = len(pairs) - pair_idx
            if remaining_in_group > 0:
                partial_path = save_partial_exr(image, output_dir, img_name, color_depth)
                chk = {
                    "operation":        "bake",
                    "completed_images": all_completed,
                    "current_image":    img_name,
                    "done_objects":     done_in_group,
                    "partial_exr":      partial_path,
                    "group_map":        group_map,
                }
                print(f"CHECKPOINT_STATE:{_json.dumps(chk, separators=(',', ':'))}", flush=True)

        # ── All objects for this image done ───────────────────────────────────
        exr_dest, bname = save_exr_raw(image, output_dir, color_depth)
        saved_paths.append(exr_dest)
        preview_path = ""
        try:
            preview_path = save_preview_png(image, output_dir, bname, scene)
        except Exception as _e:
            print(f"  Preview PNG failed: {_e}", flush=True)

        # Clean up the partial EXR for this image
        partial = os.path.join(output_dir,
                               "".join(c if c.isalnum() or c in "-_." else "_"
                                       for c in img_name).rstrip(".") + "_partial.exr")
        if os.path.isfile(partial):
            os.remove(partial)

        all_completed.append(img_name)
        print(f"IMAGE_COMPLETE:{img_name}:{exr_dest}:{preview_path}", flush=True)
        print(f"  Saved: {exr_dest}", flush=True)

        # Emit updated checkpoint (no partial EXR — current image is done)
        chk = {
            "operation":        "bake",
            "completed_images": all_completed,
            "current_image":    "",
            "done_objects":     [],
            "partial_exr":      "",
            "group_map":        group_map,
        }
        print(f"CHECKPOINT_STATE:{_json.dumps(chk, separators=(',', ':'))}", flush=True)

    # ── Finish ────────────────────────────────────────────────────────────────
    if saved_paths:
        print(f"BAKE_COMPLETE:{'|'.join(saved_paths)}", flush=True)
    else:
        print("BAKE_FAILED:Bake completed but no images were saved.", flush=True)
        sys.exit(1)

except Exception as exc:
    tb = traceback.format_exc()
    print(f"BAKE_FAILED:{exc}\n{tb}", flush=True)
    sys.exit(1)
