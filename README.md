# Hermes Voice Server

A locally-hosted, OpenAI-compatible text-to-speech server for Hermes agent
instances. No GPU required. Each Hermes instance gets its own voice,
including custom accent blends (e.g. "American English with a mild Japanese
accent"), by setting a plain `voice` name in its own TTS config -- no routing
or proxy layer needed.

Built on [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), which
wraps the Kokoro-82M model in an OpenAI-compatible `/v1/audio/speech`
endpoint and runs comfortably on CPU. This repo adds one thing on top:
permanent, named, per-agent voice blends.

## How it fits together

- **Base image**: `ghcr.io/remsky/kokoro-fastapi-cpu:latest` -- the TTS
  server itself, unmodified.
- **`voices.yaml`**: one entry per Hermes instance, each a weighted blend of
  Kokoro's built-in voices (e.g. `am_michael(85)+jm_kumo(15)`).
- **`scripts/build_voices.py`**: turns each `voices.yaml` entry into a
  permanent `voices/<name>.pt` voicepack, using the base image's own
  `/v1/audio/voices/combine` endpoint.
- **`Dockerfile`**: bakes those `.pt` files into the base image's voice
  directory, so `voice: "<name>"` just works like any built-in voice.
- **`docker-compose.yml`**: builds and runs the final image on your Linux VM.

## Setup

1. **Build or tweak a voice blend interactively (optional but recommended).**
   Run the base image once and open its built-in web UI:
   ```bash
   docker run --rm -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest
   ```
   Visit `http://localhost:8880/web`, use the voice mixer to search voices,
   set weights, and preview the result live. Copy the blend string it shows
   (e.g. `af_heart(80)+jf_alpha(20)`) into `voices.yaml` under a name of your
   choice. Stop the container (Ctrl+C) when you're done.

2. **Edit `voices.yaml`** -- one entry per Hermes instance:
   ```yaml
   voices:
     jarvis:
       blend: "am_michael(85)+jm_kumo(15)"
       description: "American English, mild Japanese accent"
   ```

3. **Generate the voicepacks:**
   ```bash
   pip install -r scripts/requirements.txt
   python scripts/build_voices.py
   ```
   This spins up a temporary container, asks it to combine each blend, and
   writes `voices/<name>.pt`.

4. **Build and run the server:**
   ```bash
   docker compose up --build -d
   ```
   The server is now listening on port `8880`, with your custom voices baked
   in alongside Kokoro's built-ins.

Whenever you change `voices.yaml`, re-run step 3 and then
`docker compose up --build -d` again to pick up the new voicepacks.

## Configuring each Hermes instance

Point each instance's OpenAI-compatible TTS client at this server and set its
own voice name -- that's the entire integration:

```python
from openai import OpenAI

client = OpenAI(base_url="http://<vm-ip>:8880/v1", api_key="not-needed")

with client.audio.speech.with_streaming_response.create(
    model="kokoro",
    voice="jarvis",          # this instance's voice, from voices.yaml
    input=agent_output_text,
    response_format="mp3",   # also: wav, opus, flac, aac, pcm
) as response:
    response.stream_to_file("output.mp3")
```

Any Kokoro built-in voice name (`af_heart`, `bf_emma`, `jm_kumo`, ...) also
works directly, and ad-hoc blends can be passed inline too (e.g.
`"af_bella(2)+af_sky(1)"`) without touching `voices.yaml` -- useful for quick
experiments before promoting a blend to a permanent named voice.

Full built-in voice catalog: `curl http://<vm-ip>:8880/v1/audio/voices`

## Notes

- **No built-in auth.** Kokoro-FastAPI doesn't authenticate requests. This is
  fine on a private VM/LAN reachable only by your own agents; if the VM's
  port 8880 is reachable more broadly, put a reverse proxy with auth in
  front of it, or restrict `CORS_ORIGINS` and firewall the port.
- **Supported languages/accents**: English (US/GB), Spanish, French, Hindi,
  Italian, Japanese, Brazilian Portuguese, Mandarin Chinese -- any of these
  can be blended together the same way.
- **Multi-speaker / inline voice switching** and SSML are also supported
  natively by the base server if a Hermes instance ever needs to speak as
  multiple voices in one request -- see the
  [Kokoro-FastAPI README](https://github.com/remsky/Kokoro-FastAPI) for
  `[voice:...]` tags and `voice_aliases`.
