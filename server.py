"""Minimal read-only HTTP server on top of genie_tts.

Public endpoints:
- GET /characters : List preloaded character names.
- POST /tts       : Generate and return WAV audio.
"""

import json
import logging
import os
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# GENIE_DATA_DIR must be set before importing genie_tts.
if data_dir := os.getenv("GENIE_DATA_DIR"):
    os.environ["GENIE_DATA_DIR"] = data_dir

import genie_tts as genie  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8100"))
CHARACTERS_FILE = Path(os.getenv("CHARACTERS_FILE", "characters.json"))

# Genie internal state is shared, so guard calls.
GENIE_LOCK = threading.RLock()
AVAILABLE_CHARACTERS: list[str] = []


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _read_characters_config() -> list[dict]:
    if not CHARACTERS_FILE.exists():
        log.warning("No %s found. /characters will be empty.", CHARACTERS_FILE)
        return []

    with CHARACTERS_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, list):
        raise RuntimeError("characters.json must contain a JSON array")
    return data


def preload_characters() -> None:
    global AVAILABLE_CHARACTERS
    configured = _read_characters_config()
    loaded: list[str] = []

    for char in configured:
        name = char.get("character_name")
        if not name:
            log.error("Skipping character with missing character_name")
            continue

        try:
            with GENIE_LOCK:
                if char.get("predefined"):
                    genie.load_predefined_character(name)
                else:
                    genie.load_character(
                        character_name=name,
                        onnx_model_dir=char["onnx_model_dir"],
                        language=char["language"],
                    )

                if ref := char.get("reference_audio"):
                    genie.set_reference_audio(
                        character_name=name,
                        audio_path=ref["audio_path"],
                        audio_text=ref["audio_text"],
                        language=ref.get("language") or char.get("language"),
                    )

            loaded.append(name)
            log.info("Character ready: %s", name)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to preload character %s: %s", name, exc)

    AVAILABLE_CHARACTERS = loaded
    log.info("Preload completed: %d character(s)", len(AVAILABLE_CHARACTERS))


def synthesize_wav(character_name: str, text: str, split_sentence: bool) -> bytes:
    if character_name not in AVAILABLE_CHARACTERS:
        raise ApiError(404, f"character not loaded: {character_name}")

    tmp_wav = Path(tempfile.gettempdir()) / f"genie-tts-{uuid.uuid4().hex}.wav"
    try:
        with GENIE_LOCK:
            genie.tts(
                character_name=character_name,
                text=text,
                split_sentence=split_sentence,
                save_path=str(tmp_wav),
            )

        if not tmp_wav.exists():
            raise ApiError(500, "TTS did not produce an output file")

        return tmp_wav.read_bytes()
    finally:
        try:
            if tmp_wav.exists():
                tmp_wav.unlink()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wav(self, status: int, wav_data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_data)))
        self.end_headers()
        self.wfile.write(wav_data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"invalid JSON: {exc.msg}") from exc

        if not isinstance(payload, dict):
            raise ApiError(400, "JSON body must be an object")

        return payload

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/characters":
            self._json(200, {"characters": AVAILABLE_CHARACTERS})
            return

        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/tts":
            self._json(404, {"error": "not found"})
            return

        try:
            payload = self._read_json_body()
            character_name = payload.get("character_name")
            text = payload.get("text")
            split_sentence = bool(payload.get("split_sentence", True))

            if not isinstance(character_name, str) or not character_name:
                raise ApiError(400, "character_name is required")
            if not isinstance(text, str) or not text.strip():
                raise ApiError(400, "text is required")

            wav_data = synthesize_wav(
                character_name=character_name,
                text=text,
                split_sentence=split_sentence,
            )
            self._wav(200, wav_data)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled /tts error")
            self._json(500, {"error": str(exc)})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("http %s", fmt % args)


if __name__ == "__main__":
    preload_characters()
    log.info("Starting read-only Genie API on %s:%d", HOST, PORT)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
