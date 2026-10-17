#!/usr/bin/env python3
"""Live voice-editing backend for web/voice-editor.html.

Runs on the HOST (not inside Docker) deliberately: it needs to write repo
files (voices.yaml, voices/*.pt, openai_mappings.json, docker-compose.yml)
and run `docker compose restart/up`. Doing that from inside a container
would mean mounting the Docker socket into it, which is effectively
root-on-host access -- a real privilege escalation for what is otherwise a
small local editing tool. Running as a plain host process avoids that: it
has exactly the privileges of whoever started it, no more.

It talks to the already-running main server (MAIN_SERVER_URL) to do the
actual voice combining, so saving a change is fast: no temporary container,
just a combine call + a file write + a restart.

SECURITY: this has no authentication and can write repo files and restart
the Docker service. Bind it to loopback (the default) unless you have a
specific reason to reach it from another machine, and never expose it
beyond a trusted network.

Usage:
    pip install -r scripts/requirements.txt
    python scripts/voice_admin.py
    # then open http://localhost:8765/
"""
import os
import subprocess
import time

import requests
from flask import Flask, jsonify, request, send_from_directory

from voice_common import (
    REPO_ROOT,
    WEB_DIR,
    internal_name,
    load_voices_yaml,
    save_voices_yaml,
    parse_blend,
    render_blend,
    update_mappings,
    update_compose_volume,
    generate_custom_voices_page,
)

MAIN_SERVER_URL = os.environ.get("MAIN_SERVER_URL", "http://localhost:8880")
ADMIN_HOST = os.environ.get("ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8765"))

app = Flask(__name__)


@app.get("/")
def editor_page():
    return send_from_directory(WEB_DIR, "voice-editor.html")


@app.get("/api/voices")
def api_list_voices():
    voices = load_voices_yaml()
    out = {}
    for name, entry in voices.items():
        blend = entry["blend"] if isinstance(entry, dict) else entry
        description = entry.get("description", "") if isinstance(entry, dict) else ""
        out[name] = {"blend": blend, "description": description, "components": parse_blend(blend)}
    return jsonify(out)


@app.get("/api/catalog")
def api_catalog():
    try:
        resp = requests.get(f"{MAIN_SERVER_URL}/v1/audio/voices", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach main server at {MAIN_SERVER_URL}: {exc}"}), 502
    ids = sorted(v["id"] for v in resp.json().get("voices", []))
    return jsonify({"voices": ids})


@app.post("/api/preview")
def api_preview():
    body = request.get_json(force=True)
    components = body.get("components") or []
    blend = render_blend(components)
    text = body.get("text") or "Hello, this is a test of this voice."
    try:
        resp = requests.post(
            f"{MAIN_SERVER_URL}/v1/audio/speech",
            json={"model": "kokoro", "voice": blend, "input": text, "response_format": "mp3"},
            timeout=60,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502
    return resp.content, 200, {"Content-Type": "audio/mpeg"}


def _wait_for_server_ready(timeout_s: int = 60) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{MAIN_SERVER_URL}/v1/audio/voices", timeout=2).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


@app.post("/api/voices/<name>")
def api_save_voice(name):
    body = request.get_json(force=True)
    components = body.get("components") or []
    description = body.get("description", "")
    blend = render_blend(components)
    if not blend:
        return jsonify({"error": "At least one voice component is required"}), 400

    voices = load_voices_yaml()
    old_blend = voices.get(name, {}).get("blend") if isinstance(voices.get(name), dict) else voices.get(name)
    old_internal = internal_name(name, old_blend) if old_blend else None
    new_internal = internal_name(name, blend)

    # 1. Combine via the live main server.
    try:
        resp = requests.post(f"{MAIN_SERVER_URL}/v1/audio/voices/combine", json=blend, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return jsonify({"error": f"Combine failed: {exc}"}), 502

    # 2. Write the voicepack, update voices.yaml, the alias table, and the test page.
    (REPO_ROOT / "voices" / f"{new_internal}.pt").write_bytes(resp.content)
    if old_internal and old_internal != new_internal:
        old_path = REPO_ROOT / "voices" / f"{old_internal}.pt"
        old_path.unlink(missing_ok=True)

    voices[name] = {"blend": blend, "description": description}
    save_voices_yaml(voices)
    update_mappings({name: new_internal})
    generate_custom_voices_page(voices)

    # 3. Apply: a plain restart is enough unless the bind-mount path changed
    #    (i.e. the blend's dominant language -- and therefore the internal
    #    filename's prefix letter -- changed), which needs a recreate.
    needs_recreate = update_compose_volume(old_internal, new_internal)
    cmd = ["docker", "compose", "up", "-d"] if needs_recreate else ["docker", "compose", "restart", "voice-server"]
    try:
        subprocess.run(cmd, cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": f"docker command failed: {exc.stderr}"}), 500

    applied = _wait_for_server_ready()
    return jsonify({
        "ok": True,
        "blend": blend,
        "internal_name": new_internal,
        "recreated": needs_recreate,
        "server_ready": applied,
    })


if __name__ == "__main__":
    print(f"Voice admin editor: http://{ADMIN_HOST}:{ADMIN_PORT}/")
    print(f"Talking to main TTS server at {MAIN_SERVER_URL}")
    if ADMIN_HOST not in ("127.0.0.1", "localhost"):
        print("WARNING: bound beyond loopback -- this tool has no auth and can write repo "
              "files and restart the Docker service. Only do this on a trusted network.")
    app.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=False)
