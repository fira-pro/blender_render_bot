# blender_render_bot

A headless **Blender 5.1** baking & rendering server controlled entirely via a Telegram bot using Telethon MTProto (for large-file support and fast transfers).

---

## Features

- 📥 Receive `.blend` files and download via parallel MTProto transfers (FastTelethon)
- 🎨 **Bake** or **🖼 Render** with per-job settings:
  - Device: CPU / GPU (CUDA, HIP, Metal, OneAPI — auto-detected)
  - Samples, denoising, tile size
  - Bake type (Combined, Diffuse, Normal, AO, etc.)
  - Bake target: single image (all objects → one texture) or per-material images
- 📊 Real-time progress updates (sample progress for render, object progress for bake)
- 🖼 Preview image sent on completion
- 💾 Choose output format (PNG, JPEG, EXR, TIFF, WebP) and compression
- 📤 Large-file upload via parallel MTProto (FastTelethon + `cryptg`)
- ♻️ Multiple operations on one file without re-uploading
- 🚫 Cancel running jobs at any time
- ⏳ FIFO job queue with position reporting
- ℹ️ `/info` command: queue status, active sessions, available GPU types
- 🔐 Whitelisted user IDs only
- 🗑 Auto-cleanup: on user `/done` or 48-hour TTL

---

## Requirements

- Python 3.11+
- Blender 5.1 installed on the server (headless / no display needed)
- A Telegram account (API credentials from [my.telegram.org](https://my.telegram.org))
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/youruser/blender_render_bot.git
cd blender_render_bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up credentials
cp .env.example .env
nano .env   # fill in your values (see below)

# 5. Run the bot
python bot.py
```

### First run (session auth)

Because the bot uses Telethon (MTProto), you **do not** need to authenticate interactively — it connects as a bot using your `BOT_TOKEN`. The session file `blender_bot.session` is created automatically.

---

## Configuration (`.env`)

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `WHITELIST_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `BLENDER_PATH` | Absolute path to the Blender 5.1 executable |
| `WORKSPACE_DIR` | Directory for job files (default: `./workspace`) |
| `SESSION_TTL_HOURS` | Hours before idle sessions are auto-cleaned (default: `48`) |
| `MAX_QUEUE_SIZE` | Maximum jobs in queue (default: `10`) |

---

## Installing Blender on a Linux server

```bash
# Download Blender 5.1 (adjust URL for latest release)
wget https://download.blender.org/release/Blender5.1/blender-5.1.0-linux-x64.tar.xz
tar -xf blender-5.1.0-linux-x64.tar.xz -C /usr/local/
ln -s /usr/local/blender-5.1.0-linux-x64/blender /usr/local/bin/blender

# Verify
blender --version
```

### On Google Colab

```python
!wget -q https://download.blender.org/release/Blender5.1/blender-5.1.0-linux-x64.tar.xz
!tar -xf blender-5.1.0-linux-x64.tar.xz -C /content/
# In .env set: BLENDER_PATH=/content/blender-5.1.0-linux-x64/blender
```

---

## Speed tip: install `cryptg`

`cryptg` is a native AES implementation that significantly speeds up Telethon encryption:

```bash
pip install cryptg
```

It is installed automatically via `requirements.txt`. If it fails to build, the bot still works — just slower on large file transfers.

---

## Bot commands

| Command | Description |
|---|---|
| `/start` / `/help` | Show welcome message and command list |
| `/info` | Show queue status, running jobs, detected GPU devices |
| `/cancel` | Cancel the currently running job |
| `/done` | Mark the current file as done and clean up workspace |

---

## Project structure

```
blender_render_bot/
├── bot.py                      Main bot entry point
├── job_queue.py                FIFO queue and session state machine
├── blender_worker.py           Subprocess runner and progress parser
├── fast_telethon.py            Parallel upload/download (FastTelethon)
├── config.py                   .env loader
├── utils.py                    Keyboard builders and message formatters
├── blender_scripts/
│   ├── detect_devices.py       GPU device detection (run inside Blender)
│   ├── render_script.py        Headless render logic (run inside Blender)
│   └── bake_script.py          Headless bake logic (run inside Blender)
├── .env.example                Credentials template
├── requirements.txt            Python dependencies
└── README.md                   This file
```

---

## How it works

```
User sends .blend ──► FastTelethon download (progress bar)
         │
         ▼
  Choose: Bake / Render
         │
         ▼
  Configure settings (inline keyboard)
  Device / Samples / Denoise / Tile size
  [+ Bake type / Bake target for baking]
         │
         ▼
  Added to FIFO queue
         │
         ▼
  Blender subprocess (headless)
  stdout parsed → Telegram progress edits every 5s
         │
         ▼
  Preview image sent
         │
         ▼
  Choose output format + compression
         │
         ▼
  FastTelethon upload (progress bar)
         │
         ▼
  Another operation? / Done (cleanup)
```

---

## Baking requirements

The bot assumes your `.blend` file is already set up for baking:
- ✅ UV maps are unwrapped and packed
- ✅ Each material that should be baked has an **ImageTexture node** that is **selected and active** in the shader editor
- ✅ The image in that node has been created with the desired resolution

**Single image mode**: All material ImageTexture nodes point to the **same** image → everything bakes into one texture.

**Per-material mode**: Each material has its **own** image → each material gets a separate output file.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `BLENDER_PATH not found` | Check the path in `.env`; verify with `blender --version` |
| GPU not detected | Ensure CUDA/ROCm drivers are installed; check `/info` |
| File too large for Telegram | FastTelethon supports up to 2 GB (4 GB with Telegram Premium) |
| Bake fails immediately | Check that ImageTexture nodes are selected/active in all materials |
| Slow transfers | Install `cryptg`: `pip install cryptg` |
| `Session already exists` | Delete `blender_bot.session` and restart |
