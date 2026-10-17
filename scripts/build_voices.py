#!/usr/bin/env python3
"""Offline batch build: turn every voices.yaml blend into a permanent
Kokoro voicepack, wire up its alias, and regenerate the standalone
"Custom Voices" test page. Spins up a temporary instance of the base
Kokoro-FastAPI image to do the combining (so it works even if the main
server isn't running yet).

For live, one-voice-at-a-time editing with auto-restart, use
scripts/voice_admin.py instead -- it talks to the already-running server
and is what web/voice-editor.html drives.

Usage:
    pip install -r scripts/requirements.txt
    python scripts/build_voices.py
"""
import subprocess
import sys
import time

import requests

from voice_common import (
    VOICES_DIR,
    internal_name,
    load_voices_yaml,
    update_mappings,
    generate_custom_voices_page,
)

BASE_IMAGE = "ghcr.io/remsky/kokoro-fastapi-cpu:latest"
CONTAINER_NAME = "kokoro-voicebuilder"
BUILDER_PORT = 8881


def start_builder_container() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--name", CONTAINER_NAME,
            "-p", f"{BUILDER_PORT}:8880",
            "-e", "ALLOW_LOCAL_VOICE_SAVING=true",
            BASE_IMAGE,
        ],
        check=True,
    )


def wait_for_ready(timeout_s: int = 90) -> None:
    url = f"http://localhost:{BUILDER_PORT}/v1/audio/voices"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=2).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError(f"Kokoro builder container did not become ready within {timeout_s}s")


def stop_builder_container() -> None:
    subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True, check=False)


def build_voicepacks(voices: dict) -> dict:
    """Builds each blend, returns {name: internal_name} for the alias table."""
    VOICES_DIR.mkdir(exist_ok=True)
    aliases = {}

    print(f"Starting temporary builder container ({BASE_IMAGE})...")
    start_builder_container()
    try:
        print("Waiting for it to become ready...")
        wait_for_ready()

        print(f"Building {len(voices)} voicepack(s):")
        for name, entry in voices.items():
            blend = entry["blend"] if isinstance(entry, dict) else entry
            resp = requests.post(
                f"http://localhost:{BUILDER_PORT}/v1/audio/voices/combine",
                json=blend,
                timeout=60,
            )
            resp.raise_for_status()
            iname = internal_name(name, blend)
            (VOICES_DIR / f"{iname}.pt").write_bytes(resp.content)
            aliases[name] = iname
            print(f"  {iname}.pt  <-  {blend}   (alias: {name})")
    finally:
        print("Stopping builder container...")
        stop_builder_container()

    return aliases


def main() -> None:
    voices = load_voices_yaml()
    if not voices:
        print("No voices defined in voices.yaml -- nothing to build.")
        return

    aliases = build_voicepacks(voices)
    update_mappings(aliases)
    generate_custom_voices_page(voices)

    print(f"\nDone. {len(voices)} voicepack(s) in {VOICES_DIR}/, aliases in openai_mappings.json, "
          f"custom voices page in web/")
    print("Next: docker compose up -d   (first time, or after adding/removing a voice)")
    print("  or: docker compose restart voice-server   (after only changing an existing blend)")
    print("Then open: http://localhost:8880/web/custom-voices.html")


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, TimeoutError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
