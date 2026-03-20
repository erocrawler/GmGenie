# genie-tts-server

Lightweight deployment wrapper for [Genie-TTS](https://github.com/High-Logic/Genie-TTS)
(GPT-SoVITS ONNX inference engine). Pre-loads characters at startup and exposes the
Genie FastAPI HTTP server for use by other services (e.g. the kokkoro napcat-qq plugin).

---

## Requirements

- Python >= 3.9 (Genie-TTS requirement)
- ~391 MB disk space for model data (downloaded automatically on first run)

> **Note:** Run in Administrator mode on Windows to avoid potential performance
> degradation (Genie recommendation).

---

## Setup

```bash
git clone <this-repo-url> genie-tts-server
cd genie-tts-server

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` to `.env` and edit as needed:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8100` | TCP port |
| `WORKERS` | `1` | Uvicorn workers |
| `CHARACTERS_FILE` | `characters.json` | Path to character definitions |
| `GENIE_DATA_DIR` | *(unset)* | Path to pre-downloaded GenieData folder |

### Characters

Copy `characters.example.json` to `characters.json` and configure your voices.
`characters.json` is gitignored so local model paths are never committed.

**Predefined character** (no model files needed, downloads automatically):
```json
[
  { "character_name": "mika", "predefined": true }
]
```

**Custom GPT-SoVITS character** (convert your model to ONNX first — see Genie docs):
```json
[
  {
    "character_name": "my-char",
    "onnx_model_dir": "/path/to/onnx/my-char",
    "language": "jp",
    "reference_audio": {
      "audio_path": "/path/to/reference.wav",
      "audio_text": "The text spoken in the reference audio"
    }
  }
]
```

Available predefined characters:
[HuggingFace — CharacterModels](https://huggingface.co/High-Logic/Genie/tree/main/CharacterModels)

---

## Running

```bash
python server.py
```

The server will:
1. Download required base data on first run (~391 MB, saved to `GENIE_DATA_DIR` if set)
2. Load all characters defined in `characters.json`
3. Start the FastAPI server on `http://HOST:PORT`

### Running with PM2

PM2 can keep the Python process running and restart it automatically.

Install PM2 first:

```bash
npm install -g pm2
```

Start the server with the included PM2 config:

```bash
pm2 start ecosystem.config.cjs
```

Useful PM2 commands:

```bash
pm2 status
pm2 logs genie-tts
pm2 restart genie-tts
pm2 stop genie-tts
pm2 delete genie-tts
pm2 save
```

By default, the PM2 config uses the virtualenv interpreter at `.venv/Scripts/python.exe`
on Windows or `.venv/bin/python` on Linux/macOS. To override it, set `PYTHON_INTERPRETER`
before starting PM2.

Windows PowerShell:

```powershell
$env:PYTHON_INTERPRETER = "C:\path\to\python.exe"
pm2 start ecosystem.config.cjs
```

Linux / macOS:

```bash
PYTHON_INTERPRETER=/path/to/python pm2 start ecosystem.config.cjs
```

---

## API Reference

All endpoints accept and return JSON unless noted otherwise.

### `POST /load_character`
Load a character model at runtime.
```json
{
  "character_name": "my-char",
  "onnx_model_dir": "/path/to/onnx",
  "language": "jp"
}
```

### `POST /set_reference_audio`
Set the reference audio for voice cloning on a loaded character.
```json
{
  "character_name": "my-char",
  "audio_path": "/path/to/ref.wav",
  "audio_text": "Text spoken in the reference audio",
  "language": "jp"
}
```

### `POST /tts`
Synthesize text. Returns a streaming `audio/wav` response (PCM, 32 kHz, mono, 16-bit).
```json
{
  "character_name": "mika",
  "text": "Hello, world!",
  "split_sentence": true
}
```
Optional: `"save_path": "/path/to/output.wav"` to also save on the server.

### `POST /unload_character`
Free a character from memory.
```json
{ "character_name": "my-char" }
```

### `POST /stop`
Immediately stop all ongoing synthesis tasks. No body required.

### `POST /clear_reference_audio_cache`
Clear the reference audio cache. No body required.

---

## Running as a systemd service (Linux)

Create `/etc/systemd/system/genie-tts.service`:

```ini
[Unit]
Description=Genie TTS Server
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/genie-tts-server
EnvironmentFile=/home/<your-user>/genie-tts-server/.env
ExecStart=/home/<your-user>/genie-tts-server/.venv/bin/python server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now genie-tts
sudo journalctl -u genie-tts -f   # follow logs
```

---

## Integration — kokkoro napcat-qq plugin

Set `genieTtsUrl` in `~/.openclaw/config.json`:
```json
"napcat-qq": {
  "genieTtsUrl": "http://<server-host>:8100"
}
```

The `speak` tool in the napcat-qq plugin will `POST /tts`, receive the WAV stream,
encode it as `base64://...`, and send it as a OneBot v11 `record` voice message.
