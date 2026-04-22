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

Assumptions (as per bot requirements):
  - UV is already unwrapped and packed.
  - Every material that should be baked has an ImageTexture node
    that is SELECTED and ACTIVE in the node editor.
  - "single" target  → all active ImageTexture nodes reference the SAME image.
  - "per_material"   → each material has its OWN image.

Progress markers printed to stdout (parsed by blender_worker.py):
    BAKE_PROGRESS:2/8:ObjectName
    BAKE_COMPLETE:/abs/path/img1.png|/abs/path/img2.png
    BAKE_FAILED:error description
"""

import os
import sys
import traceback

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


device_type  = get_arg("--device-type", "CPU")
use_gpu      = get_arg("--use-gpu", "false").lower() == "true"
samples_arg  = get_arg("--samples", "default")
denoise_arg  = get_arg("--denoise", "true").lower() == "true"
tile_arg     = get_arg("--tile-size", "default")
bake_type    = get_arg("--bake-type", "COMBINED")
bake_target  = get_arg("--bake-target", "single")   # "single" | "per_material"
output_dir   = get_arg("--output-dir", "/tmp/blender_bake")

os.makedirs(output_dir, exist_ok=True)

import bpy

# ── Helper ──────────────────────────────────────────────────────────────────

def active_image_texture_node(material):
    """
    Return the active (selected) ImageTexture node for a material,
    or None if none exists.
    """
    if not material or not material.use_nodes or not material.node_tree:
        return None
    tree = material.node_tree
    active = tree.nodes.active
    if active and active.type == "TEX_IMAGE" and active.image:
        return active
    # Fallback: look for any selected TEX_IMAGE node
    for node in tree.nodes:
        if node.select and node.type == "TEX_IMAGE" and node.image:
            return node
    return None


def save_image(image, output_dir: str) -> str:
    """Save a Blender image to the output directory as PNG and return the path."""
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in image.name)
    if not safe_name.lower().endswith(".png"):
        safe_name += ".png"
    dest = os.path.join(output_dir, safe_name)
    image.filepath_raw = dest
    image.file_format = "PNG"
    image.save()
    return dest


# ── Main bake logic ─────────────────────────────────────────────────────────
try:
    scene = bpy.context.scene
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

    # ── Samples ───────────────────────────────────────────────────────────────
    if samples_arg != "default":
        cycles.samples = int(samples_arg)

    # ── Denoising ─────────────────────────────────────────────────────────────
    cycles.use_denoising = denoise_arg

    # ── Tile size ─────────────────────────────────────────────────────────────
    if tile_arg != "default":
        try:
            ts = int(tile_arg)
            cycles.tile_size = ts
        except (AttributeError, TypeError):
            try:
                render.tile_x = int(tile_arg)
                render.tile_y = int(tile_arg)
            except Exception:
                pass

    # ── Collect bake targets ──────────────────────────────────────────────────
    # Build list of (object, material) pairs that have an active ImageTexture
    bake_pairs = []
    seen_images = {}   # image.name → Image (to deduplicate)

    # Use view_layer.objects so we only touch objects that are actually
    # present in the active View Layer.  bpy.data.objects includes ALL
    # objects in the file (even those in excluded collections), and
    # calling obj.select_set(True) on them raises a RuntimeError.
    for obj in bpy.context.view_layer.objects:
        if obj.type != "MESH":
            continue
        # Skip objects that are hidden in the viewport or not selectable
        if obj.hide_get() or not obj.visible_get():
            continue
        for slot in obj.material_slots:
            mat = slot.material
            node = active_image_texture_node(mat)
            if node is None:
                continue
            bake_pairs.append((obj, mat, node.image))
            seen_images[node.image.name] = node.image

    if not bake_pairs:
        print(
            "BAKE_FAILED:No mesh objects with an active ImageTexture node found.",
            flush=True,
        )
        sys.exit(1)

    total = len(bake_pairs)
    print(f"Found {total} (object, material) pair(s) to bake.", flush=True)

    # Deselect all objects
    bpy.ops.object.select_all(action="DESELECT")

    saved_paths = []

    for idx, (obj, mat, image) in enumerate(bake_pairs, start=1):
        obj_name = obj.name
        img_name = image.name
        print(
            f"BAKE_PROGRESS:{idx - 1}/{total}:{obj_name}",
            flush=True,
        )
        print(
            f"  Baking '{bake_type}' for object='{obj_name}' "
            f"material='{mat.name}' image='{img_name}'…",
            flush=True,
        )

        # Make this object the active selection
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

        # Ensure the correct material is active on the object
        for i, slot in enumerate(obj.material_slots):
            if slot.material == mat:
                obj.active_material_index = i
                break

        # Ensure the ImageTexture node is active in the material
        for node in mat.node_tree.nodes:
            node.select = False
        target_node = active_image_texture_node(mat)
        if target_node:
            target_node.select = True
            mat.node_tree.nodes.active = target_node

        try:
            bpy.ops.object.bake(
                type=bake_type,
                use_clear=(idx == 1),   # clear only on first bake for single-image mode
                margin=16,
                use_selected_to_active=False,
            )
        except RuntimeError as exc:
            print(
                f"  ⚠ Bake failed for {obj_name}/{mat.name}: {exc}",
                flush=True,
            )
            continue

        # For per_material mode or the last iteration of single mode, save now
        if bake_target == "per_material" or idx == total:
            dest = save_image(image, output_dir)
            if dest not in saved_paths:
                saved_paths.append(dest)
            print(f"  Saved: {dest}", flush=True)

        print(f"BAKE_PROGRESS:{idx}/{total}:{obj_name}", flush=True)

    # For single-image mode, all materials share the same image — save once
    if bake_target == "single" and not saved_paths:
        for img in seen_images.values():
            dest = save_image(img, output_dir)
            if dest not in saved_paths:
                saved_paths.append(dest)
            print(f"  Saved: {dest}", flush=True)

    if saved_paths:
        print(f"BAKE_COMPLETE:{'|'.join(saved_paths)}", flush=True)
    else:
        print(
            "BAKE_FAILED:Bake completed but no images were saved.",
            flush=True,
        )
        sys.exit(1)

except Exception as exc:
    tb = traceback.format_exc()
    print(f"BAKE_FAILED:{exc}\n{tb}", flush=True)
    sys.exit(1)
