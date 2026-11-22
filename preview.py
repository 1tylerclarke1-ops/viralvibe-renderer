"""Builds a short Viral Studios preview from finished pictures and clips (no app needed).

Run by the Preview workflow; writes previews/preview.mp4.
"""
import shutil
import tempfile
from pathlib import Path

from studio_render import render_studio

B = "https://base44.app/api/apps/6b021b03e93ffe8d1d017341/files/mp/public/6b021b03e93ffe8d1d017341/"
TEXT = ("I grab my emergency bag from behind the seat, the tire iron from under it, and I climb down "
        "onto the asphalt between two idling cars. I sling the bag over my shoulder, grip the tire iron, "
        "and start walking toward Millhaven, twelve miles ahead.")

words = TEXT.upper().split()
pages, size = [], 6
for i in range(0, len(words), size):
    pages.append({"text": " ".join(words[i:i + size]), "start": i / len(words), "end": min(1, (i + size) / len(words))})

story = {
    "id": "preview", "episode": 0, "title": "The Trucker Who Walked Into the Outbreak for His Daughter",
    "trope_label": "Zombie Outbreak · Preview", "brand": "Zombie Outbreak", "motion": True,
    "backdrop_url": B + "40dd95235_studio-1795308217724.png",
    "title_image_url": B + "6c6dac3f0_studio-1795354080805.png",
    "opener_url": "", "clip_mode": "settle", "atmosphere": "rain", "music": "dark", "word_captions": True,
    "watermark": "@thezombiefiles", "thumb_text": "PREVIEW", "part_label": "PREVIEW",
    "title_audio_url": "kokoro:am_michael", "title_text": "Part 2. The Trucker Who Walked Into the Outbreak for His Daughter",
    "outro_audio_url": "", "end_text": "",
    "beats": [{
        "audio_url": "kokoro:am_michael", "text": TEXT,
        "image_url": "",
        "images": [B + "6c6dac3f0_studio-1795354080805.png", B + "a7890d8ee_studio-1795354261089.png",
                   B + "268429e2f_studio-1795354783001.png"],
        "clips": [B + "c56b07e0b_opener-6b023738c090c4029ccc5d69-part2.mp4",
                  B + "92ba2fd8b_opener-6b023738c090c4029ccc5d69-part3.mp4",
                  B + "7e3ceabbc_opener-6b023738c090c4029ccc5d69-part4.mp4"],
        "clips_reverse": [True, True, True], "speaker": None, "pages": pages,
    }],
}

work = Path(tempfile.mkdtemp())
video, _ = render_studio(story, work)
Path("previews").mkdir(exist_ok=True)
shutil.copy(video, "previews/preview.mp4")
print("preview written")
