"""Records each candidate Kokoro voice reading the same line (previews/voices/*.mp3)."""
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

import studio_render

LINE = ("I pull the rig onto the shoulder and dial Carol's number with shaking fingers. "
        "It rings twice, then drops to voicemail. Whatever is happening, I am not waiting for a callback. "
        "I am going to her.")
import sys
VOICES = sys.argv[1:] or ["am_michael", "am_fenrir", "am_puck", "am_echo", "am_eric", "am_liam", "am_onyx", "am_adam",
                          "af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore", "af_sarah"]

out = Path("previews/voices")
out.mkdir(parents=True, exist_ok=True)
pipeline = KPipeline(lang_code="a")
studio_render._KOKORO = pipeline
for voice in VOICES:
    audio = np.concatenate([chunk for _, _, chunk in pipeline(LINE, voice=studio_render.kokoro_voice(voice), speed=1.0)])
    safe = voice.replace("*", "x").replace("+", "_plus_")
    wav = out / f"{safe}.wav"
    sf.write(wav, audio, 24000)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-b:a", "128k", str(wav.with_suffix(".mp3"))], check=True)
    wav.unlink()
    print("recorded", voice)
