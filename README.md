# GmGenie

Minimal read-only HTTP wrapper around `genie_tts`.

The server loads character definitions from `characters.json` at startup, then uses
lazy loading at runtime. It exposes only:

- `GET /characters`
- `POST /tts`
- `GET /health`

No public write/mutation endpoints are exposed.

## Requirements

- Python 3.9+
- `genie-tts` and `python-dotenv` installed (`pip install -r requirements.txt`)

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env`.

Supported env vars used by this server:

- `HOST` (default `0.0.0.0`)
- `PORT` (default `8100`)
- `CHARACTERS_FILE` (default `characters.json`)
- `GENIE_DATA_DIR` (optional, must be set before importing `genie_tts`)
- `IDLE_UNLOAD_SECONDS` (default `600`)
- `JANITOR_INTERVAL_SECONDS` (default `30`)
- `MAX_LOADED_CHARACTERS` (default `1`, LRU eviction)

Runtime behavior:

- Characters are loaded on first `/tts` request.
- Loaded characters are unloaded if idle longer than `IDLE_UNLOAD_SECONDS`.
- If loading a new character would exceed `MAX_LOADED_CHARACTERS`, the
  least-recently-used loaded character is unloaded first.

## Character Config

`characters.json` must be a JSON array.

Example with predefined character:

```json
[
  {
    "character_name": "mika",
    "predefined": true
  }
]
```

Example with custom ONNX character:

```json
[
  {
    "character_name": "arona_jp",
    "onnx_model_dir": "data/arona_jp/onnx/ALuoNa_e8g15",
    "language": "jp",
    "reference_audio": {
      "audio_path": "data/arona_jp/reference.wav",
      "audio_text": "こんにちは、先生",
      "language": "jp"
    }
  }
]
```

## Run

```bash
python server.py
```

## API

### `GET /health`

Returns service health.

```json
{ "status": "ok", "loaded_count": 0, "max_loaded": 1 }
```

### `GET /characters`

Returns configured characters and currently loaded subset.

```json
{ "characters": ["arona_jp"], "loaded": ["arona_jp"], "max_loaded": 1 }
```

### `POST /tts`

Request:

```json
{
  "character_name": "arona_jp",
  "text": "こんにちは、先生",
  "split_sentence": true
}
```

Response:

- `200 OK`
- `Content-Type: audio/wav`
- Body is a playable WAV file.

Example:

```bash
curl -X POST "http://localhost:8100/tts" \
  -H "Content-Type: application/json" \
  -d '{"character_name":"arona_jp","text":"こんにちは、先生"}' \
  --output test.wav
```

## Notes

- Use `http://`, not `https://`, unless you put this behind a TLS proxy.
- If `/characters` is empty, check startup logs for catalog loading failures.
- If `/tts` returns `unknown character`, ensure the name exists in `/characters`.
- First `/tts` request for a character may be slower due to lazy loading.
