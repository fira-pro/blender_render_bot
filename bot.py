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

    result = await run_blender_job(
        job_id=job.job_id,
        blend_path=job.blend_path,
        operation=job.operation,
        settings=job.settings,
        workspace_dir=job.workspace_dir,
        script_path=script,
        progress_cb=progress_cb,
        set_process_cb=queue.set_process,
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
    """Create a small PNG thumbnail for non-PNG files using Pillow."""
    ext = Path(file_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        return None   # already a web-friendly format, send directly
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
    """Convert an output file to the target format using Pillow."""
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
        elif fmt in ("JPEG",):
            img = img.convert("RGB")
            save_kwargs = {"quality": compression or 95, "optimize": True}
        elif fmt == "WEBP":
            save_kwargs = {"quality": compression or 90}
        elif fmt == "TIFF":
            save_kwargs = {"compression": "tiff_lzw"}
        elif fmt == "EXR":
            # EXR needs OpenEXR; if not available, save as TIFF
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
        log.error(f"Conversion failed: {exc}")
        return src   # fall back to original


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
    sess.state = SessionState.AWAITING_OPERATION

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
        fmt = data.split(":")[1]
        sess.output_format = fmt
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
