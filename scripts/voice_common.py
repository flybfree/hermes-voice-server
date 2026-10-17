"""Shared logic between build_voices.py (offline batch build) and
voice_admin.py (live editing server). Keep this import-only: no top-level
side effects, since both entry points import it.
"""
import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICES_YAML = REPO_ROOT / "voices.yaml"
VOICES_DIR = REPO_ROOT / "voices"
WEB_DIR = REPO_ROOT / "web"
MAPPINGS_PATH = REPO_ROOT / "openai_mappings.json"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

_BLEND_TOKEN = re.compile(r"^\s*([^()\s]+)(?:\((-?[\d.]+)\))?\s*$")


def internal_name(name: str, blend: str) -> str:
    """The prefixed on-disk voice name that gets Kokoro's language inference
    right -- it reads only the first character of the voice name to pick the
    text-processing language (see tts_service.py: segment_voice[:1].lower()).
    """
    lang_letter = blend.strip()[0].lower()
    return f"{lang_letter}_{name}"


def parse_blend(blend: str) -> list[dict]:
    """"af_kore(3)+jf_nezumi(50)+jf_gongitsune(47)" -> [{"voice": "af_kore", "weight": 3}, ...]
    A bare "bf_lily" (no weight) yields [{"voice": "bf_lily", "weight": None}].
    """
    components = []
    for token in blend.split("+"):
        m = _BLEND_TOKEN.match(token)
        if not m:
            continue
        voice, weight = m.group(1), m.group(2)
        components.append({"voice": voice, "weight": float(weight) if weight is not None else None})
    return components


def render_blend(components: list[dict]) -> str:
    """Inverse of parse_blend."""
    parts = []
    for c in components:
        voice = c["voice"]
        weight = c.get("weight")
        parts.append(f"{voice}({weight})" if weight is not None else voice)
    return "+".join(parts)


def load_voices_yaml() -> dict:
    import yaml
    config = yaml.safe_load(VOICES_YAML.read_text()) or {}
    return config.get("voices") or {}


def save_voices_yaml(voices: dict) -> None:
    import yaml
    header = (
        "# Custom voices for the Hermes TTS server.\n"
        "# Edit via web/voice-editor.html (scripts/voice_admin.py), or by hand and then\n"
        "# `python scripts/build_voices.py`.\n"
    )
    VOICES_YAML.write_text(
        header + yaml.safe_dump({"voices": voices}, sort_keys=False, default_flow_style=False, indent=2)
    )


def update_mappings(aliases: dict) -> None:
    mappings = json.loads(MAPPINGS_PATH.read_text()) if MAPPINGS_PATH.exists() else {
        "models": {"tts-1": "kokoro-v1_0", "tts-1-hd": "kokoro-v1_0", "kokoro": "kokoro-v1_0"},
        "voices": {},
    }
    mappings.setdefault("voices", {}).update(aliases)
    MAPPINGS_PATH.write_text(json.dumps(mappings, indent=4) + "\n")


def update_compose_volume(old_internal: str | None, new_internal: str) -> bool:
    """Swap a voicepack's bind-mount line in docker-compose.yml if its prefix
    letter (and therefore filename) changed. Returns True if the file was
    changed and a full `docker compose up -d` (not just restart) is needed.
    """
    text = COMPOSE_PATH.read_text()
    line = f"./voices/{new_internal}.pt:/app/api/src/voices/v1_0/{new_internal}.pt"
    if line in text:
        return False  # already present, nothing to do

    if old_internal and f"{old_internal}.pt" in text:
        new_text = text.replace(f"{old_internal}.pt", f"{new_internal}.pt")
    else:
        # Brand new voice: add a mount line right after the marker comment.
        marker = "- ./voices/"
        idx = text.rfind(marker)
        if idx == -1:
            raise RuntimeError("Could not find voices/ volume section in docker-compose.yml")
        line_end = text.index("\n", idx) + 1
        indent = text[text.rfind("\n", 0, idx) + 1 : idx]
        new_text = text[:line_end] + f"{indent}- {line}\n" + text[line_end:]

    COMPOSE_PATH.write_text(new_text)
    return True


CUSTOM_VOICES_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Custom Voices</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 48rem; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ margin-bottom: 0.25rem; }}
  p.sub {{ color: #666; margin-top: 0; }}
  textarea {{ width: 100%; box-sizing: border-box; font-size: 1rem; padding: 0.5rem; }}
  .voice {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
  .voice h2 {{ margin: 0 0 0.25rem 0; font-size: 1.1rem; }}
  .voice .desc {{ color: #555; margin: 0 0 0.75rem 0; }}
  .voice .blend {{ color: #888; font-family: monospace; font-size: 0.85rem; }}
  button {{ font-size: 1rem; padding: 0.4rem 1rem; cursor: pointer; }}
  button:disabled {{ opacity: 0.6; cursor: wait; }}
  audio {{ display: block; margin-top: 0.5rem; width: 100%; }}
  .status {{ font-size: 0.85rem; color: #a00; margin-top: 0.4rem; }}
  a.back {{ font-size: 0.9rem; }}
</style>
</head>
<body>
  <h1>Custom Voices</h1>
  <p class="sub">Permanent, named voices baked into this server (from voices.yaml). Each is usable directly as the <code>voice</code> field by any OpenAI-compatible client. <a class="back" href="/web/">&larr; full voice mixer</a> &middot; <a class="back" href="http://localhost:8765/">edit blends &rarr;</a></p>

  <label for="sample-text"><strong>Sample text</strong></label>
  <textarea id="sample-text" rows="2">{sample_text}</textarea>

  <div id="voices">
{voice_cards}
  </div>

  <script>
    async function playVoice(name, btn) {{
      const text = document.getElementById('sample-text').value;
      const audioEl = document.getElementById('audio-' + name);
      const statusEl = document.getElementById('status-' + name);
      statusEl.textContent = '';
      btn.disabled = true;
      btn.textContent = 'Generating...';
      try {{
        const resp = await fetch('/v1/audio/speech', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ model: 'kokoro', voice: name, input: text, response_format: 'mp3' }})
        }});
        if (!resp.ok) {{
          const errText = await resp.text();
          throw new Error('HTTP ' + resp.status + ': ' + errText);
        }}
        const blob = await resp.blob();
        audioEl.src = URL.createObjectURL(blob);
        audioEl.hidden = false;
        audioEl.play();
      }} catch (err) {{
        statusEl.textContent = String(err);
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Play sample';
      }}
    }}
  </script>
</body>
</html>
"""

VOICE_CARD_TEMPLATE = """    <div class="voice">
      <h2>{name}</h2>
      <p class="desc">{description}</p>
      <p class="blend">{blend}</p>
      <button onclick="playVoice('{name_js}', this)">Play sample</button>
      <audio id="audio-{name_html}" controls hidden></audio>
      <div class="status" id="status-{name_html}"></div>
    </div>
"""


def generate_custom_voices_page(voices: dict) -> None:
    WEB_DIR.mkdir(exist_ok=True)
    cards = []
    for name, entry in voices.items():
        blend = entry["blend"] if isinstance(entry, dict) else entry
        description = entry.get("description", "") if isinstance(entry, dict) else ""
        cards.append(
            VOICE_CARD_TEMPLATE.format(
                name=html.escape(name),
                name_html=html.escape(name),
                name_js=name.replace("\\", "\\\\").replace("'", "\\'"),
                description=html.escape(description),
                blend=html.escape(blend),
            )
        )
    page = CUSTOM_VOICES_PAGE_TEMPLATE.format(
        sample_text=html.escape("Hello, this is a test of this voice."),
        voice_cards="".join(cards),
    )
    (WEB_DIR / "custom-voices.html").write_text(page, encoding="utf-8")
