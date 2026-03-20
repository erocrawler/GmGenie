"""
Genie-TTS server
~~~~~~~~~~~~~~~~
Pre-loads characters defined in ``characters.json`` then starts the built-in
Genie FastAPI server.

Environment variables (can also be set via .env):
    HOST             - bind address (default: 0.0.0.0)
    PORT             - TCP port     (default: 8100)
    WORKERS          - uvicorn workers (default: 1)
    CHARACTERS_FILE  - path to character config JSON (default: characters.json)
    GENIE_DATA_DIR   - optional path to pre-downloaded GenieData folder
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# GENIE_DATA_DIR must be set before importing genie_tts
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
WORKERS = int(os.getenv("WORKERS", "1"))
CHARACTERS_FILE = Path(os.getenv("CHARACTERS_FILE", "characters.json"))


def load_characters() -> None:
    if not CHARACTERS_FILE.exists():
        log.info("No %s found — no characters pre-loaded.", CHARACTERS_FILE)
        return

    with CHARACTERS_FILE.open(encoding="utf-8") as fh:
        characters = json.load(fh)

    for char in characters:
        name: str = char["character_name"]
        log.info("Loading character: %s", name)

        if char.get("predefined"):
            genie.load_predefined_character(name)
        else:
            genie.load_character(
                character_name=name,
                onnx_model_dir=char["onnx_model_dir"],
                language=char["language"],
            )

        if ref := char.get("reference_audio"):
            # language falls back to the character-level language field
            ref_language = ref.get("language") or char.get("language")
            genie.set_reference_audio(
                character_name=name,
                audio_path=ref["audio_path"],
                audio_text=ref["audio_text"],
                language=ref_language,
            )

        log.info("Character ready: %s", name)


if __name__ == "__main__":
    load_characters()
    log.info("Starting Genie-TTS server on %s:%d (workers=%d)", HOST, PORT, WORKERS)
    genie.start_server(host=HOST, port=PORT, workers=WORKERS)
