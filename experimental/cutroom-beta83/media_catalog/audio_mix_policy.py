"""One audio policy shared by recipe compilation and the TypeScript renderers."""
import json
from pathlib import Path


def audio_mix_policy():
    return json.loads((Path(__file__).resolve().parents[1] /
                       "src/audio-mix-policy-data.json").read_text(encoding="utf-8"))
