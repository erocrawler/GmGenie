"""Minimal read-only HTTP server on top of genie_tts.

Public endpoints:
- GET /characters : List preloaded character names.
- POST /tts       : Generate and return WAV audio.
"""

import io
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
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
IDLE_UNLOAD_SECONDS = int(os.getenv("IDLE_UNLOAD_SECONDS", "600"))
JANITOR_INTERVAL_SECONDS = int(os.getenv("JANITOR_INTERVAL_SECONDS", "30"))
MAX_LOADED_CHARACTERS = int(os.getenv("MAX_LOADED_CHARACTERS", "1"))

# Genie internal state is shared, so guard calls.
GENIE_LOCK = threading.RLock()
CHARACTER_CONFIGS: dict[str, dict] = {}
AVAILABLE_CHARACTERS: list[str] = []
LOADED_CHARACTERS: set[str] = set()
LAST_USED_AT: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Sentence splitting and WAV assembly helpers
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(r'(?<=[。！？…\u2026.!?])\s*')


def _split_sentences(text: str) -> list[str]:
    """Split text on CJK/ASCII sentence-ending punctuation."""
    parts = _SENTENCE_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def _wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, tuple[int, int, int]]:
    """Return (pcm_frames, (channels, sampwidth, framerate)) parsed from WAV bytes."""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        return wf.readframes(wf.getnframes()), (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())


def _build_wav_header(channels: int, sampwidth: int, framerate: int, data_size: int = 0xFFFFFFFF) -> bytes:
    """Build a 44-byte WAV header. data_size=0xFFFFFFFF produces a streaming/open-ended WAV."""
    riff_size = (36 + data_size) if data_size != 0xFFFFFFFF else 0xFFFFFFFF
    return (
        struct.pack('<4sI4s', b'RIFF', riff_size, b'WAVE')
        + struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, channels, framerate,
                      framerate * channels * sampwidth, channels * sampwidth, sampwidth * 8)
        + struct.pack('<4sI', b'data', data_size)
    )


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


def load_character_catalog() -> None:
    global AVAILABLE_CHARACTERS, CHARACTER_CONFIGS
    configured = _read_characters_config()
    catalog: dict[str, dict] = {}

    for char in configured:
        name = char.get("character_name")
        if not name:
            log.error("Skipping character with missing character_name")
            continue
        if name in catalog:
            log.error("Skipping duplicate character_name in config: %s", name)
            continue
        catalog[name] = char

    CHARACTER_CONFIGS = catalog
    AVAILABLE_CHARACTERS = sorted(catalog.keys())
    log.info("Loaded character catalog: %d character(s)", len(AVAILABLE_CHARACTERS))


def _load_character_locked(character_name: str) -> None:
    char = CHARACTER_CONFIGS.get(character_name)
    if not char:
        raise ApiError(404, f"unknown character: {character_name}")

    if character_name in LOADED_CHARACTERS:
        LAST_USED_AT[character_name] = time.time()
        return

    # Enforce a load cap with LRU eviction before loading a new character.
    if MAX_LOADED_CHARACTERS > 0 and len(LOADED_CHARACTERS) >= MAX_LOADED_CHARACTERS:
        lru_name = min(LOADED_CHARACTERS, key=lambda n: LAST_USED_AT.get(n, 0.0))
        try:
            _unload_character_locked(lru_name)
            log.info("Evicted least-recently-used character: %s", lru_name)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(503, f"failed to evict loaded character {lru_name}: {exc}") from exc

    if char.get("predefined"):
        genie.load_predefined_character(character_name)
    else:
        genie.load_character(
            character_name=character_name,
            onnx_model_dir=char["onnx_model_dir"],
            language=char["language"],
        )

    if ref := char.get("reference_audio"):
        genie.set_reference_audio(
            character_name=character_name,
            audio_path=ref["audio_path"],
            audio_text=ref["audio_text"],
            language=ref.get("language") or char.get("language"),
        )

    LOADED_CHARACTERS.add(character_name)
    LAST_USED_AT[character_name] = time.time()
    log.info("Character loaded on demand: %s", character_name)


def _unload_character_locked(character_name: str) -> None:
    if character_name not in LOADED_CHARACTERS:
        return

    genie.unload_character(character_name=character_name)
    LOADED_CHARACTERS.discard(character_name)
    LAST_USED_AT.pop(character_name, None)
    log.info("Character unloaded due to idle timeout: %s", character_name)


def janitor_unload_idle_characters() -> None:
    while True:
        time.sleep(JANITOR_INTERVAL_SECONDS)
        cutoff = time.time() - IDLE_UNLOAD_SECONDS
        try:
            with GENIE_LOCK:
                to_unload = [
                    name for name in LOADED_CHARACTERS if LAST_USED_AT.get(name, 0.0) < cutoff
                ]
                for name in to_unload:
                    try:
                        _unload_character_locked(name)
                    except Exception as exc:  # noqa: BLE001
                        log.error("Failed to unload character %s: %s", name, exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Idle janitor loop failed: %s", exc)


def _synthesize_sentence_locked(character_name: str, text: str) -> bytes:
    """Synthesise one sentence and return its WAV bytes. Caller must hold GENIE_LOCK."""
    tmp_wav = Path(tempfile.gettempdir()) / f"genie-tts-{uuid.uuid4().hex}.wav"
    try:
        genie.tts(
            character_name=character_name,
            text=text,
            split_sentence=False,
            save_path=str(tmp_wav),
        )
        if not tmp_wav.exists():
            raise ApiError(500, f"TTS produced no output for: {text!r}")
        return tmp_wav.read_bytes()
    finally:
        try:
            if tmp_wav.exists():
                tmp_wav.unlink()
        except OSError:
            pass


def synthesize_wav(character_name: str, text: str, split_sentence: bool) -> bytes:
    if character_name not in CHARACTER_CONFIGS:
        raise ApiError(404, f"unknown character: {character_name}")

    sentences = _split_sentences(text) if split_sentence else [text]
    if not sentences:
        sentences = [text]

    pcm_chunks: list[bytes] = []
    wav_params: tuple[int, int, int] | None = None

    with GENIE_LOCK:
        _load_character_locked(character_name)
        for sentence in sentences:
            wav_bytes = _synthesize_sentence_locked(character_name, sentence)
            pcm, params = _wav_to_pcm(wav_bytes)
            pcm_chunks.append(pcm)
            if wav_params is None:
                wav_params = params
        LAST_USED_AT[character_name] = time.time()

    combined_pcm = b''.join(pcm_chunks)
    channels, sampwidth, framerate = wav_params or (1, 2, 32000)
    return _build_wav_header(channels, sampwidth, framerate, len(combined_pcm)) + combined_pcm


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

    # ------------------------------------------------------------------
    # Streaming helpers (chunked transfer encoding)
    # ------------------------------------------------------------------

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f'{len(data):x}\r\n'.encode())
        self.wfile.write(data)
        self.wfile.write(b'\r\n')
        self.wfile.flush()

    def _end_chunks(self) -> None:
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def _handle_tts_stream(self, payload: dict) -> None:
        """POST /tts/stream — synthesise sentence-by-sentence and stream WAV via chunked encoding.

        Keeps the upstream connection alive so CF Tunnel (and other proxies) do not
        time out with 524 on long texts.  The client receives a valid streaming WAV
        (RIFF size = 0xFFFFFFFF) that players and NapCat/ffmpeg handle correctly.
        """
        character_name = payload.get("character_name")
        text = payload.get("text")
        split_sentence = bool(payload.get("split_sentence", True))

        if not isinstance(character_name, str) or not character_name:
            raise ApiError(400, "character_name is required")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "text is required")
        if character_name not in CHARACTER_CONFIGS:
            raise ApiError(404, f"unknown character: {character_name}")

        sentences = _split_sentences(text) if split_sentence else [text]
        if not sentences:
            sentences = [text]

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        header_sent = False
        try:
            with GENIE_LOCK:
                _load_character_locked(character_name)
                for i, sentence in enumerate(sentences):
                    log.info("[stream] sentence %d/%d: %r", i + 1, len(sentences), sentence[:60])
                    wav_bytes = _synthesize_sentence_locked(character_name, sentence)
                    pcm, params = _wav_to_pcm(wav_bytes)
                    if not header_sent:
                        self._write_chunk(_build_wav_header(*params))  # streaming WAV header
                        header_sent = True
                    self._write_chunk(pcm)
                LAST_USED_AT[character_name] = time.time()
        except Exception as exc:  # noqa: BLE001
            log.exception("[stream] error mid-stream")
            if not header_sent:
                raise  # let do_POST handle it as a proper JSON error
            # Headers already sent — we can only terminate the stream abruptly
            try:
                self._end_chunks()
            except Exception:
                pass
            return

        self._end_chunks()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/characters":
            with GENIE_LOCK:
                loaded = sorted(LOADED_CHARACTERS)
            self._json(
                200,
                {
                    "characters": AVAILABLE_CHARACTERS,
                    "loaded": loaded,
                    "max_loaded": MAX_LOADED_CHARACTERS,
                },
            )
            return

        if self.path == "/health":
            with GENIE_LOCK:
                loaded_count = len(LOADED_CHARACTERS)
            self._json(
                200,
                {
                    "status": "ok",
                    "loaded_count": loaded_count,
                    "max_loaded": MAX_LOADED_CHARACTERS,
                },
            )
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/tts/stream":
            try:
                payload = self._read_json_body()
                self._handle_tts_stream(payload)
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001
                log.exception("Unhandled /tts/stream error")
                self._json(500, {"error": str(exc)})
            return

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
    load_character_catalog()
    threading.Thread(target=janitor_unload_idle_characters, daemon=True).start()
    log.info(
        "Starting read-only Genie API on %s:%d (idle unload: %ds)",
        HOST,
        PORT,
        IDLE_UNLOAD_SECONDS,
    )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
