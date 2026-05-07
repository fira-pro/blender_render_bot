"""
bot.py — Telegram bot entry point.

Uses Telethon (MTProto) for both messaging and fast file transfers.
Run with:  python bot.py
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.tl import types as tl_types

import config
from blender_worker import (
    cleanup_workspace,
    detect_blender_devices,
    run_blender_job,
)
from config import (
    API_HASH,
    API_ID,
    BAKE_SCRIPT_PATH,
    BLENDER_PATH,
    BOT_TOKEN,
    MAX_QUEUE_SIZE,
    PROGRESS_UPDATE_INTERVAL,
    RENDER_SCRIPT_PATH,
    SESSION_TTL_HOURS,
    WHITELIST_USER_IDS,
    WORKSPACE_DIR,
)
from fast_telethon import download_file, upload_file
from job_queue import (
    DEFAULT_BAKE_SETTINGS,
    DEFAULT_RENDER_SETTINGS,
    Job,
    JobQueue,
    SessionState,
    UserSession,
    make_job_id,
)
from utils import (
    fmt_duration,
    fmt_size,
    kb_after_job,
    kb_compression,
    kb_format,
    kb_operation,
    kb_settings,
    msg_bake_progress,
    msg_download_progress,
    msg_info,
    msg_job_started,
    msg_queued,
    msg_render_progress,
    msg_settings_header,
    msg_upload_progress,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ── Globals ───────────────────────────────────────────────────────────────────
client = TelegramClient("blender_bot", API_ID, API_HASH)
queue = JobQueue()
available_gpu_types: List[str] = []   # populated at startup

os.makedirs(WORKSPACE_DIR, exist_ok=True)


# ── Access control ─────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return user_id in WHITELIST_USER_IDS


async def deny(event) -> None:
    await event.respond("⛔ You are not authorised to use this bot.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def workspace_for(job_id: str) -> str:
    path = os.path.join(WORKSPACE_DIR, job_id)
    os.makedirs(path, exist_ok=True)
    return path


async def edit_or_reply(event_or_msg, text: str, buttons=None):
    """Try to edit an existing message, fall back to sending a new one."""
    try:
        await event_or_msg.edit(text, buttons=buttons, parse_mode="md")
    except Exception:
        await client.send_message(
            event_or_msg.chat_id, text, buttons=buttons, parse_mode="md"
        )


async def send_typing(chat_id: int) -> None:
    try:
        await client.send_message(chat_id, "")   # triggers "typing…" implicitly
    except Exception:
        pass


# ── TTL cleanup loop ───────────────────────────────────────────────────────────

async def ttl_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)   # check every hour
        expired = queue.expired_sessions(SESSION_TTL_HOURS * 3600)
        for uid in expired:
            sess = queue.get_session(uid)
            if sess and sess.state not in (SessionState.QUEUED, SessionState.RUNNING):
                log.info(f"TTL cleanup for user {uid}")
                if sess.job_id:
                    ws = workspace_for(sess.job_id)
                    cleanup_workspace(ws)
                queue.delete_session(uid)


# ── Job processing callback ────────────────────────────────────────────────────

async def process_job(job: Job) -> None:
    """Called by the queue worker for each job."""
    sess = queue.get_session(job.user_id)
    if not sess:
        log.warning(f"No session for job {job.job_id}")
        return

    sess.state = SessionState.RUNNING
    script = RENDER_SCRIPT_PATH if job.operation == "render" else BAKE_SCRIPT_PATH

    # Send/update a progress message
    prog_msg = await client.send_message(
        sess.chat_id,
        msg_job_started(job.operation),
        parse_mode="md",
    )
    sess.progress_msg_id = prog_msg.id

    last_update = time.time()

    async def progress_cb(info: Dict[str, Any]) -> None:
        nonlocal last_update
        now = time.time()
        if now - last_update < PROGRESS_UPDATE_INTERVAL:
            return
        last_update = now
        if info["type"] == "render_progress":
            text = msg_render_progress(info)
        elif info["type"] == "bake_progress":
            text = msg_bake_progress(info)
        else:
            return
        try:
            await client.edit_message(
                sess.chat_id, sess.progress_msg_id, text, parse_mode="md"
            )
        except Exception as exc:
            log.debug(f"Progress edit failed: {exc}")

    async def image_complete_cb(img_name: str, exr_path: str, preview_path: str) -> None:
        await _on_image_complete(sess, img_name, exr_path, preview_path)

    async def frame_complete_cb(frame_num: int, exr_path: str, preview_path: str) -> None:
        await _on_frame_complete(sess, frame_num, exr_path, preview_path)

    async def checkpoint_cb(state_json: str) -> None:
        await _package_checkpoint(sess, job, state_json)

    result = await run_blender_job(
        job_id=job.job_id,
        blend_path=job.blend_path,
        operation=job.operation,
        settings=job.settings,
        workspace_dir=job.workspace_dir,
        script_path=script,
        progress_cb=progress_cb,
        set_process_cb=queue.set_process,
        image_complete_cb=image_complete_cb,
        frame_complete_cb=frame_complete_cb,
        checkpoint_cb=checkpoint_cb,
    )

    if job.status == "cancelled":
        await client.edit_message(
            sess.chat_id,
            sess.progress_msg_id,
            "🚫  **Job cancelled.**",
            parse_mode="md",
        )
        sess.state = SessionState.IDLE
        return

    if not result["success"]:
        error_text = result.get("error", "Unknown error")
        await client.edit_message(
            sess.chat_id,
            sess.progress_msg_id,
            f"❌  **Job failed.**\n\n```\n{error_text[:3000]}\n```",
            parse_mode="md",
        )
        sess.state = SessionState.IDLE
        return

    # ── Success ───────────────────────────────────────────────────────────────
    sess.output_files = result["output_files"]
    job.status = "done"

    await client.edit_message(
        sess.chat_id,
        sess.progress_msg_id,
        f"✅  **{'Render' if job.operation == 'render' else 'Bake'} complete!**",
        parse_mode="md",
    )

    # Send preview
    await _send_preview(sess, job.operation)


async def _send_preview(sess: UserSession, operation: str) -> None:
    """Send the largest output image as preview, then ask for format/compression."""
    if not sess.output_files:
        await client.send_message(
            sess.chat_id,
            "⚠️  No output files found. Please check your .blend file settings.",
            parse_mode="md",
        )
        return

    # Pick largest file as preview
    preview_path = max(sess.output_files, key=lambda p: os.path.getsize(p))

    # Generate a small PNG thumbnail for preview if the file is EXR/TIFF
    thumb_path = await _make_thumbnail(preview_path)
    send_path = thumb_path if thumb_path else preview_path

    try:
        caption = (
            f"🖼  **Preview** — {os.path.basename(preview_path)}\n"
            f"({len(sess.output_files)} output file(s))\n\n"
            "_Choose output format:_"
        )
        await client.send_file(
            sess.chat_id,
            send_path,
            caption=caption,
            parse_mode="md",
            buttons=kb_format(operation),
            force_document=False,
        )
        sess.state = SessionState.AWAITING_FORMAT
    except Exception as exc:
        log.error(f"Preview send failed: {exc}")
        await client.send_message(
            sess.chat_id,
            "⚠️  Could not send preview. Choose output format:",
            buttons=kb_format(operation),
            parse_mode="md",
        )
        sess.state = SessionState.AWAITING_FORMAT


async def _make_thumbnail(file_path: str) -> Optional[str]:
    """Return a web-friendly preview path for the given output file.

    Priority:
    1. Blender-generated _preview.png (View Transform applied correctly).
       - For bake EXRs: <base>_preview.png sits next to the .exr
       - For render EXRs: render_preview.png in the same output directory
    2. Pillow thumbnail fallback (linear/no View Transform — OK for data maps,
       washed-out for colour renders, but beats nothing).
    3. None (send the file directly if it's already a web format).
    """
    ext = Path(file_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        return None   # already displayable

    parent_dir = os.path.dirname(file_path)

    # 1a. Bake preview: <base>_preview.png
    bake_preview = os.path.splitext(file_path)[0] + "_preview.png"
    if os.path.isfile(bake_preview):
        return bake_preview

    # 1b. Render preview: render_preview.png in same directory
    render_preview = os.path.join(parent_dir, "render_preview.png")
    if os.path.isfile(render_preview):
        return render_preview

    # 2. Pillow fallback
    try:
        from PIL import Image
        thumb_path = file_path + "_thumb.png"
        img = Image.open(file_path)
        img.thumbnail((1024, 1024))
        img.save(thumb_path, "PNG")
        return thumb_path
    except Exception as exc:
        log.warning(f"Thumbnail generation failed: {exc}")
        return None


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

async def _on_image_complete(
    sess: UserSession, img_name: str, exr_path: str, preview_path: str
) -> None:
    """Upload a finished bake image immediately and record it in the session."""
    if not exr_path or not os.path.isfile(exr_path):
        return
    file_size  = os.path.getsize(exr_path)
    send_start = time.time()
    try:
        with open(exr_path, "rb") as f:
            uploaded = await upload_file(client, f)
        from telethon.tl import types as _tlt
        from telethon import utils as _tlu
        attributes, mime = _tlu.get_attributes(exr_path)
        media = _tlt.InputMediaUploadedDocument(
            file=uploaded, mime_type=mime,
            attributes=attributes, force_file=True,
        )
        await client.send_file(
            sess.chat_id, media, force_document=True,
            caption=(
                f"✅  **{img_name}** baked — "
                f"{fmt_size(file_size)} "
                f"({fmt_duration(time.time() - send_start)})"
            ),
            parse_mode="md",
        )
        if img_name not in sess.completed_image_names:
            sess.completed_image_names.append(img_name)
    except Exception as exc:
        log.error(f"_on_image_complete failed for '{img_name}': {exc}")


async def _on_frame_complete(
    sess: UserSession, frame_num: int, exr_path: str, preview_path: str
) -> None:
    """Upload a finished render frame immediately and record it in the session."""
    if not exr_path or not os.path.isfile(exr_path):
        return
    file_size  = os.path.getsize(exr_path)
    send_start = time.time()
    try:
        with open(exr_path, "rb") as f:
            uploaded = await upload_file(client, f)
        from telethon.tl import types as _tlt
        from telethon import utils as _tlu
        attributes, mime = _tlu.get_attributes(exr_path)
        media = _tlt.InputMediaUploadedDocument(
            file=uploaded, mime_type=mime,
            attributes=attributes, force_file=True,
        )
        await client.send_file(
            sess.chat_id, media, force_document=True,
            caption=(
                f"🎞  **Frame {frame_num}** — "
                f"{fmt_size(file_size)} "
                f"({fmt_duration(time.time() - send_start)})"
            ),
            parse_mode="md",
        )
        if frame_num not in sess.completed_frames:
            sess.completed_frames.append(frame_num)
    except Exception as exc:
        log.error(f"_on_frame_complete failed for frame {frame_num}: {exc}")


async def _package_checkpoint(sess: UserSession, job: Job, state_json: str) -> None:
    """Package a checkpoint ZIP and upload it to Telegram, replacing the previous one."""
    import io
    import json as _json
    import zipfile

    try:
        state = _json.loads(state_json)
    except Exception as exc:
        log.warning(f"Checkpoint state JSON parse failed: {exc}")
        return

    ws = workspace_for(job.job_id)
    zip_name = f"checkpoint_{job.job_id[:8]}_checkpoint.zip"
    zip_path = os.path.join(ws, zip_name)

    chk_data = {
        **state,
        "job_id":    job.job_id,
        "operation": job.operation,
        "settings":  {k: v for k, v in job.settings.items() if not k.startswith("_")},
        "blend_source": {
            "file_id_info": sess.blend_file_id,
            "filename":     os.path.basename(sess.blend_path),
        },
    }

    partial_exr = state.get("partial_exr", "")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("checkpoint.json", _json.dumps(chk_data, indent=2))
            if partial_exr and os.path.isfile(partial_exr):
                zf.write(partial_exr, os.path.basename(partial_exr))
    except Exception as exc:
        log.error(f"Checkpoint ZIP creation failed: {exc}")
        return

    # Delete the previous checkpoint message
    if sess.checkpoint_msg_id:
        try:
            await client.delete_messages(sess.chat_id, [sess.checkpoint_msg_id])
        except Exception:
            pass
        sess.checkpoint_msg_id = None

    # Upload new checkpoint
    try:
        with open(zip_path, "rb") as f:
            uploaded = await upload_file(client, f)
        from telethon.tl import types as _tlt
        from telethon import utils as _tlu
        attributes, mime = _tlu.get_attributes(zip_path)
        media = _tlt.InputMediaUploadedDocument(
            file=uploaded, mime_type="application/zip",
            attributes=attributes, force_file=True,
        )
        msg = await client.send_file(
            sess.chat_id, media, force_document=True,
            caption=(
                "📦  **Checkpoint saved**\n"
                "_Forward this file to resume if the session ends._"
            ),
            parse_mode="md",
        )
        sess.checkpoint_msg_id = msg.id
        log.info(f"Checkpoint uploaded: {zip_name} (msg {msg.id})")
    except Exception as exc:
        log.error(f"Checkpoint upload failed: {exc}")


async def _handle_checkpoint_resume(event, doc, fname: str) -> None:
    """Handle a *_checkpoint.zip forwarded back to the bot to resume a job."""
    import json as _json
    import zipfile

    user_id = event.sender_id
    chat_id = event.chat_id

    sess = queue.get_session(user_id)
    if sess and sess.state in (SessionState.RUNNING, SessionState.QUEUED):
        await event.respond(
            "⚠️  A job is still active. Use /cancel first.", parse_mode="md"
        )
        return

    # Download checkpoint ZIP to a temp workspace
    job_id   = make_job_id()
    ws       = workspace_for(job_id)
    zip_path = os.path.join(ws, fname)

    dl_msg = await event.respond("📥  Loading checkpoint…", parse_mode="md")
    try:
        with open(zip_path, "wb") as f:
            await download_file(client, doc, f)
    except Exception as exc:
        await client.edit_message(chat_id, dl_msg.id, f"❌  Download failed: {exc}")
        return

    # Extract checkpoint.json and optional partial EXR
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            chk = _json.loads(zf.read("checkpoint.json"))
            partial_exr_dest = ""
            stored_exr_name  = os.path.basename(chk.get("partial_exr", ""))
            if stored_exr_name and stored_exr_name in zf.namelist():
                zf.extract(stored_exr_name, ws)
                partial_exr_dest = os.path.join(ws, stored_exr_name)
    except Exception as exc:
        await client.edit_message(chat_id, dl_msg.id, f"❌  Invalid checkpoint: {exc}")
        return

    blend_source  = chk.get("blend_source", {})
    file_id_info  = blend_source.get("file_id_info")
    blend_filename = blend_source.get("filename", "scene.blend")
    operation      = chk.get("operation", "bake")
    settings       = dict(chk.get("settings", {}))

    if not file_id_info:
        await client.edit_message(
            chat_id, dl_msg.id,
            "⚠️  Checkpoint has no `.blend` reference.\n"
            "Please send the `.blend` file manually to continue."
        )
        return

    # Re-download the .blend from Telegram using the stored document reference
    blend_dest = os.path.join(ws, blend_filename)
    await client.edit_message(
        chat_id, dl_msg.id,
        f"📥  Re-downloading `{blend_filename}` from Telegram…", parse_mode="md"
    )
    try:
        from telethon.tl.types import InputDocument
        input_doc = InputDocument(
            id=file_id_info["id"],
            access_hash=file_id_info["access_hash"],
            file_reference=bytes(file_id_info["file_reference"]),
        )
        with open(blend_dest, "wb") as f:
            await download_file(client, input_doc, f)
    except Exception as exc:
        await client.edit_message(
            chat_id, dl_msg.id, f"❌  Could not re-download `.blend`: {exc}"
        )
        return

    # Build _resume state injected into settings
    resume_state: Dict[str, Any] = {}
    if operation == "bake":
        resume_state = {
            "completed_images": chk.get("completed_images", []),
            "current_image":    chk.get("current_image",    ""),
            "done_objects":     chk.get("done_objects",     []),
            "partial_exr":      partial_exr_dest or chk.get("partial_exr", ""),
        }
    elif operation == "render":
        resume_state = {
            "completed_frames": chk.get("completed_frames", []),
        }
        for k in ("frame_start", "frame_end", "frame_step"):
            if chk.get(k):
                settings[k] = chk[k]

    settings["_resume"] = resume_state

    # Clean up the old session if present
    if sess and sess.job_id:
        cleanup_workspace(workspace_for(sess.job_id))
    queue.delete_session(user_id)

    # Create new session pre-filled from checkpoint
    new_sess = queue.create_session(user_id, chat_id, blend_dest)
    new_sess.job_id                = job_id
    new_sess.operation             = operation
    new_sess.settings              = settings
    new_sess.state                 = SessionState.CONFIGURING
    new_sess.blend_file_id         = file_id_info
    new_sess.completed_image_names = list(chk.get("completed_images", []))
    new_sess.completed_frames      = [int(f) for f in chk.get("completed_frames", [])]

    completed_count = (
        len(new_sess.completed_image_names) if operation == "bake"
        else len(new_sess.completed_frames)
    )
    unit = "image(s)" if operation == "bake" else "frame(s)"

    text = (
        f"✅  **Checkpoint loaded!**\n"
        f"Operation: `{operation}` · {completed_count} {unit} already done\n\n"
    ) + msg_settings_header(operation, settings)

    await client.edit_message(chat_id, dl_msg.id, text, parse_mode="md")
    await client.send_message(
        chat_id, "Adjust settings or press **▶ Start** to resume.",
        buttons=kb_settings(operation, settings, available_gpu_types),
        parse_mode="md",
    )


# ── File sending after format/compression choice ───────────────────────────────

async def send_final_file(sess: UserSession) -> None:
    """Re-export output in chosen format then upload via FastTelethon."""
    fmt = sess.output_format or "PNG"
    compression = sess.output_compression if sess.output_compression is not None else 0
    sess.state = SessionState.RUNNING

    # Re-save each output file in the chosen format
    final_paths = []
    for src in sess.output_files:
        dst = await _convert_output(src, fmt, compression)
        if dst:
            final_paths.append(dst)

    if not final_paths:
        await client.send_message(
            sess.chat_id, "❌  Failed to convert output files.", parse_mode="md"
        )
        sess.state = SessionState.COMPLETED
        return

    # Upload each file with progress
    for fpath in final_paths:
        file_size = os.path.getsize(fpath)
        upload_start = time.time()

        prog_msg = await client.send_message(
            sess.chat_id,
            msg_upload_progress(0, file_size, 0),
            parse_mode="md",
        )
        last_upload_update = [time.time()]

        async def upload_prog(done: int, total: int) -> None:
            now = time.time()
            if now - last_upload_update[0] < PROGRESS_UPDATE_INTERVAL:
                return
            last_upload_update[0] = now
            elapsed = now - upload_start
            try:
                await client.edit_message(
                    sess.chat_id,
                    prog_msg.id,
                    msg_upload_progress(done, total, elapsed),
                    parse_mode="md",
                )
            except Exception:
                pass

        try:
            with open(fpath, "rb") as f:
                uploaded = await upload_file(client, f, progress_callback=upload_prog)

            fname = os.path.basename(fpath)
            fsize_str = fmt_size(file_size)
            elapsed_str = fmt_duration(time.time() - upload_start)

            # Send as document using the uploaded InputFile
            from telethon.tl import types as tl
            from telethon import utils as tl_utils
            attributes, mime_type = tl_utils.get_attributes(fpath)
            media = tl.InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime_type,
                attributes=attributes,
                force_file=True,
            )
            await client.send_file(
                sess.chat_id,
                media,
                caption=(
                    f"📁  **{fname}**\n"
                    f"Size: {fsize_str}  |  Uploaded in {elapsed_str}"
                ),
                parse_mode="md",
                force_document=True,
            )
            await client.delete_messages(sess.chat_id, [prog_msg.id])

        except Exception as exc:
            log.error(f"Upload failed for {fpath}: {exc}")
            await client.edit_message(
                sess.chat_id, prog_msg.id,
                f"❌  Upload failed: {exc}", parse_mode="md"
            )

    # Offer next action
    await client.send_message(
        sess.chat_id,
        "🎉  **All files sent!**\nWhat would you like to do next?",
        buttons=kb_after_job(),
        parse_mode="md",
    )
    sess.state = SessionState.COMPLETED


async def _convert_output(src: str, fmt: str, compression: int) -> Optional[str]:
    """Convert an output file to the target format using Pillow.
    If the source is EXR and Pillow cannot read it, returns the raw EXR path
    so the caller can still upload something.
    """
    if fmt == "RAW_EXR":
        return src   # send as-is
    try:
        from PIL import Image
        ext_map = {
            "PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp",
            "TIFF": ".tiff", "EXR": ".exr",
        }
        ext = ext_map.get(fmt, ".png")
        dst = os.path.splitext(src)[0] + f"_out{ext}"

        src_ext = Path(src).suffix.lower()
        if src_ext == ".exr" and fmt == "EXR":
            import shutil
            shutil.copy2(src, dst)
            return dst

        img = Image.open(src)
        save_kwargs: Dict[str, Any] = {}

        if fmt == "PNG":
            save_kwargs = {"compress_level": min(compression, 9)}
        elif fmt == "JPEG":
            img = img.convert("RGB")
            save_kwargs = {"quality": compression or 95, "optimize": True}
        elif fmt == "WEBP":
            save_kwargs = {"quality": compression or 90}
        elif fmt == "TIFF":
            save_kwargs = {"compression": "tiff_lzw"}
        elif fmt == "EXR":
            try:
                img.save(dst, format="EXR")
                return dst
            except Exception:
                dst = os.path.splitext(src)[0] + "_out.tiff"
                img.save(dst, format="TIFF", compression="tiff_lzw")
                return dst

        img.save(dst, format=fmt if fmt != "JPEG" else "JPEG", **save_kwargs)
        return dst
    except Exception as exc:
        log.error(f"Conversion failed for {src}: {exc}")
        # Fall back to the raw source file with a warning
        return src


# ═══════════════════════════════════════════════════════════════════════════════
# Event handlers
# ═══════════════════════════════════════════════════════════════════════════════

@client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    if not is_allowed(event.sender_id):
        return await deny(event)
    await event.respond(
        "👋  **Blender Render Bot**\n\n"
        "Send me a `.blend` file and I'll offer you baking or rendering options.\n\n"
        "**Commands:**\n"
        "`/info`  — queue status & available devices\n"
        "`/cancel`  — cancel the running job\n"
        "`/done`  — finish with current file & clean up\n"
        "`/help`  — show this message",
        parse_mode="md",
    )


@client.on(events.NewMessage(pattern="/help"))
async def cmd_help(event):
    await cmd_start(event)


@client.on(events.NewMessage(pattern="/info"))
async def cmd_info(event):
    if not is_allowed(event.sender_id):
        return await deny(event)
    queue_list = list(queue._queue._queue)  # type: ignore[attr-defined]
    text = msg_info(
        queue_jobs=queue_list,
        current_job=queue.current_job(),
        gpu_types=available_gpu_types,
        sessions=queue.all_sessions(),
    )
    await event.respond(text, parse_mode="md")


@client.on(events.NewMessage(pattern="/cancel"))
async def cmd_cancel(event):
    if not is_allowed(event.sender_id):
        return await deny(event)
    sess = queue.get_session(event.sender_id)
    if not sess or sess.state not in (SessionState.RUNNING, SessionState.QUEUED):
        await event.respond("ℹ️  No active job to cancel.")
        return
    cancelled = await queue.cancel_current()
    if cancelled:
        await event.respond("🚫  **Job cancelled.**", parse_mode="md")
        sess.state = SessionState.IDLE
    else:
        await event.respond("⚠️  Could not cancel — job may have already finished.")


@client.on(events.NewMessage(pattern="/done"))
async def cmd_done(event):
    if not is_allowed(event.sender_id):
        return await deny(event)
    sess = queue.get_session(event.sender_id)
    if not sess:
        await event.respond("ℹ️  No active session.")
        return
    if sess.state in (SessionState.RUNNING, SessionState.QUEUED):
        await event.respond(
            "⚠️  A job is still running/queued. Use /cancel first."
        )
        return
    if sess.job_id:
        cleanup_workspace(workspace_for(sess.job_id))
    queue.delete_session(event.sender_id)
    await event.respond("🗑  **Session cleared.** Send a new .blend file to start.", parse_mode="md")


# ── .blend file handler ────────────────────────────────────────────────────────

@client.on(events.NewMessage)
async def handle_message(event):
    if not is_allowed(event.sender_id):
        return

    # Only handle documents (files)
    doc = event.document
    if not doc:
        return

    # Check file extension
    fname = ""
    for attr in doc.attributes:
        if isinstance(attr, tl_types.DocumentAttributeFilename):
            fname = attr.file_name
            break

    if fname.lower().endswith("_checkpoint.zip"):
        await _handle_checkpoint_resume(event, doc, fname)
        return

    if not fname.lower().endswith(".blend"):
        await event.respond(
            "⚠️  Please send a `.blend` file.", parse_mode="md"
        )
        return

    # Check for active blocking session
    sess = queue.get_session(event.sender_id)
    if sess and sess.state in (SessionState.RUNNING, SessionState.QUEUED):
        await event.respond(
            "⚠️  A job is still active. Use /cancel to stop it first, "
            "or wait for it to finish."
        )
        return

    # If there's an old completed session, clean it up silently
    if sess and sess.job_id:
        cleanup_workspace(workspace_for(sess.job_id))
        queue.delete_session(event.sender_id)

    # Create job workspace & session
    job_id = make_job_id()
    ws = workspace_for(job_id)
    blend_dest = os.path.join(ws, fname)

    # Start download with progress updates
    file_size = doc.size
    dl_start = time.time()
    prog_msg = await event.respond(
        msg_download_progress(0, file_size, 0),
        parse_mode="md",
    )
    last_dl_update = [time.time()]

    async def dl_prog(done: int, total: int) -> None:
        now = time.time()
        if now - last_dl_update[0] < PROGRESS_UPDATE_INTERVAL:
            return
        last_dl_update[0] = now
        elapsed = now - dl_start
        try:
            await client.edit_message(
                event.chat_id, prog_msg.id,
                msg_download_progress(done, total, elapsed),
                parse_mode="md",
            )
        except Exception:
            pass

    try:
        with open(blend_dest, "wb") as f:
            await download_file(client, doc, f, progress_callback=dl_prog)
    except Exception as exc:
        await client.edit_message(
            event.chat_id, prog_msg.id,
            f"❌  Download failed: {exc}", parse_mode="md"
        )
        return

    elapsed_dl = fmt_duration(time.time() - dl_start)
    await client.edit_message(
        event.chat_id, prog_msg.id,
        f"✅  **File received** — `{fname}` "
        f"({fmt_size(file_size)}, {elapsed_dl})",
        parse_mode="md",
    )

    # Create session
    sess = queue.create_session(event.sender_id, event.chat_id, blend_dest)
    sess.job_id = job_id
    sess.state  = SessionState.AWAITING_OPERATION
    # Store document reference so a checkpoint can redownload the .blend automatically
    sess.blend_file_id = {
        "id":             doc.id,
        "access_hash":   doc.access_hash,
        "file_reference": list(doc.file_reference),
        "filename":      fname,
    }

    await client.send_message(
        event.chat_id,
        "What would you like to do with this file?",
        buttons=kb_operation(),
        parse_mode="md",
    )


# ── Callback query handler ─────────────────────────────────────────────────────

@client.on(events.CallbackQuery)
async def handle_callback(event):
    if not is_allowed(event.sender_id):
        await event.answer("⛔ Not authorised.")
        return

    data = event.data.decode("utf-8")
    sess = queue.get_session(event.sender_id)

    await event.answer()   # dismiss loading spinner

    # ── Ignore placeholder buttons ─────────────────────────────────────────────
    if data == "_":
        return

    # ── Operation choice ───────────────────────────────────────────────────────
    if data.startswith("op:"):
        if not sess or sess.state != SessionState.AWAITING_OPERATION:
            await event.respond("⚠️  No active session or wrong state.")
            return
        operation = data.split(":")[1]   # "render" or "bake"
        sess.operation = operation
        defaults = (
            dict(DEFAULT_RENDER_SETTINGS)
            if operation == "render"
            else dict(DEFAULT_BAKE_SETTINGS)
        )
        # Default device: GPU if available, else CPU
        if available_gpu_types:
            defaults["device"] = available_gpu_types[0]
        sess.settings = defaults
        sess.state = SessionState.CONFIGURING

        text = msg_settings_header(operation, sess.settings)
        kb = kb_settings(operation, sess.settings, available_gpu_types)
        settings_msg = await event.edit(text, buttons=kb, parse_mode="md")
        sess.settings_msg_id = settings_msg.id if settings_msg else None
        return

    # ── Settings adjustment ────────────────────────────────────────────────────
    if data.startswith("cfg:"):
        if not sess or sess.state != SessionState.CONFIGURING:
            return
        parts = data.split(":", 2)
        key = parts[1]
        val = parts[2] if len(parts) > 2 else ""

        if key == "start":
            await _submit_job(event, sess)
            return

        # Apply setting
        if key == "device":
            sess.settings["device"] = val
        elif key == "samples":
            sess.settings["samples"] = val
        elif key == "denoise":
            sess.settings["denoise"] = (val == "true")
        elif key == "tile":
            sess.settings["tile_size"] = val
        elif key == "bake_type":
            sess.settings["bake_type"] = val
        elif key == "bake_target":
            sess.settings["bake_target"] = val
        elif key == "use_clear":
            sess.settings["use_clear"] = (val == "true")
        elif key == "margin":
            sess.settings["margin"] = int(val)
        elif key == "color_depth":
            sess.settings["color_depth"] = val
        elif key == "exr_codec":
            sess.settings["exr_codec"] = val
        elif key == "frame_end":
            # "" means "use scene default" (no explicit end frame)
            sess.settings["frame_end"] = int(val) if val else None

        # Refresh keyboard in-place
        text = msg_settings_header(sess.operation, sess.settings)
        kb = kb_settings(sess.operation, sess.settings, available_gpu_types)
        try:
            await event.edit(text, buttons=kb, parse_mode="md")
        except Exception:
            pass
        return

    # ── Format selection ───────────────────────────────────────────────────────
    if data.startswith("fmt:"):
        if not sess or sess.state != SessionState.AWAITING_FORMAT:
            return
        fmt = data.split(":", 1)[1]   # handles "RAW_EXR" (no extra colons)
        sess.output_format = fmt

        if fmt == "RAW_EXR":
            # No compression step — upload raw EXR immediately
            sess.output_compression = 0
            sess.state = SessionState.AWAITING_COMPRESSION
            await event.respond(
                "📦  Sending raw EXR file(s) without conversion…",
                parse_mode="md",
            )
            asyncio.create_task(send_final_file(sess))
        else:
            sess.state = SessionState.AWAITING_COMPRESSION
            kb = kb_compression(fmt)
            await event.respond(
                f"🗜  **Compression / quality for {fmt}:**",
                buttons=kb,
                parse_mode="md",
            )
        return

    # ── Compression selection ──────────────────────────────────────────────────
    if data.startswith("cmp:"):
        if not sess or sess.state != SessionState.AWAITING_COMPRESSION:
            return
        sess.output_compression = int(data.split(":")[1])
        await event.respond(
            f"⚙️  Preparing `{sess.output_format}` "
            f"(compression `{sess.output_compression}`)…",
            parse_mode="md",
        )
        # Run file conversion + upload in background
        asyncio.create_task(send_final_file(sess))
        return

    # ── After-job choice ───────────────────────────────────────────────────────
    if data.startswith("after:"):
        action = data.split(":")[1]
        if not sess:
            return
        if action == "done":
            await cmd_done(event)
        elif action == "reformat":
            # Re-download in a different format without re-running Blender
            if not sess.output_files:
                await event.respond("\u26a0\ufe0f  No output files available for re-download.")
                return
            sess.output_format = None
            sess.output_compression = None
            sess.state = SessionState.AWAITING_FORMAT
            await event.respond(
                "\U0001f4e5  Choose a different output format:",
                buttons=kb_format(sess.operation),
                parse_mode="md",
            )
        elif action == "another":
            if sess.state not in (SessionState.COMPLETED, SessionState.IDLE):
                await event.respond("⚠️  Still busy — wait for the current job to finish.")
                return
            # Reset to operation selection, keeping the cached .blend
            sess.operation = None
            sess.settings = {}
            sess.output_files = []
            sess.output_format = None
            sess.output_compression = None
            sess.state = SessionState.AWAITING_OPERATION
            await event.respond(
                f"♻️  Using the same file: `{os.path.basename(sess.blend_path)}`\n"
                "What would you like to do?",
                buttons=kb_operation(),
                parse_mode="md",
            )
        return


# ── Job submission helper ──────────────────────────────────────────────────────

async def _submit_job(event, sess: UserSession) -> None:
    """Validate, enqueue, and confirm the job."""
    if queue.queue_size() >= MAX_QUEUE_SIZE:
        await event.respond(
            f"⚠️  Queue is full ({MAX_QUEUE_SIZE} jobs). Try again later."
        )
        return

    job = Job(
        job_id=sess.job_id,
        user_id=sess.user_id,
        chat_id=sess.chat_id,
        blend_path=sess.blend_path,
        operation=sess.operation,
        settings=dict(sess.settings),
        workspace_dir=workspace_for(sess.job_id),
    )

    await queue.enqueue(job)
    sess.state = SessionState.QUEUED

    position = queue.queue_position(job.job_id)
    current = queue.current_job()
    if current:
        position += 1   # account for the running job

    await event.respond(
        msg_queued(position, job.job_id),
        parse_mode="md",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    global available_gpu_types

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info(f"Bot started as @{me.username}")

    # Detect Blender GPU devices at startup
    log.info("Detecting Blender render devices…")
    available_gpu_types = await detect_blender_devices()
    log.info(f"Available GPU types: {available_gpu_types or ['None']}")

    # Start the job queue worker
    queue.start_worker(process_job)

    # Start TTL cleanup loop
    asyncio.create_task(ttl_cleanup_loop())

    log.info("Bot is ready. Waiting for messages…")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
