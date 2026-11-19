"""ViralVibe reel renderer.

Asks the ViralVibe app for the next fully narrated story, renders it as a
1080x1920 MP4 with the same layout as the in-app reel (title card, caption
pages, follow card), and uploads it back to the app.

Environment:
  APP_URL       e.g. https://industrious-viral-story-snap.base44.app
  RENDER_KEY    the app's RENDER_KEY secret
  MAX_RENDERS   stories to render per run (default 2)
  FONT_DIR      folder holding Outfit.ttf and JetBrainsMono.ttf (default ./fonts)
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
END_CARD_SECONDS = 3.5
PRIMARY = (0, 255, 136)
MAX_UPLOAD_BYTES = 55 * 1024 * 1024  # the app accepts up to 60 MB

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
RENDER_KEY = os.environ.get("RENDER_KEY", "")
MAX_RENDERS = int(os.environ.get("MAX_RENDERS", "2"))
FONT_DIR = Path(os.environ.get("FONT_DIR", Path(__file__).parent / "fonts"))


# ---------------------------------------------------------------------------
# Talking to the app
# ---------------------------------------------------------------------------

def api_headers():
    return {"x-render-key": RENDER_KEY}


def next_story():
    response = requests.get(f"{APP_URL}/api/render/next", headers=api_headers(), timeout=60)
    if response.status_code == 401:
        sys.exit("The app rejected the render key (401). Check the RENDER_KEY secret in GitHub and Base44 match.")
    response.raise_for_status()
    data = response.json()
    return None if data.get("none") else data


def upload_reel(story_id, path):
    with open(path, "rb") as handle:
        response = requests.post(
            f"{APP_URL}/api/render/complete",
            headers=api_headers(),
            data={"script_id": story_id},
            files={"file": ("reel.mp4", handle, "video/mp4")},
            timeout=600,
        )
    if not response.ok:
        raise RuntimeError(f"Upload failed ({response.status_code}): {response.text[:300]}")
    return response.json()


def download(url, dest, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with open(dest, "wb") as handle:
                    for chunk in response.iter_content(1 << 16):
                        handle.write(chunk)
            return dest
        except requests.RequestException as error:
            if attempt == attempts:
                raise RuntimeError(f"Could not download {url}: {error}") from error
            time.sleep(3 * attempt)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed:\n{result.stderr[-1500:]}")
    return result.stdout


def to_wav(src, dest):
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", "44100", "-ac", "2", str(dest)])
    return dest


def silence_wav(seconds, dest):
    run([
        "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{seconds:.3f}", str(dest),
    ])
    return dest


def duration_of(path):
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ])
    return float(out.strip())


# ---------------------------------------------------------------------------
# Drawing (mirrors src/lib/reel.js in the app)
# ---------------------------------------------------------------------------

def font(name, size, weight):
    face = ImageFont.truetype(str(FONT_DIR / name), size)
    face.set_variation_by_axes([weight])
    return face


class Fonts:
    def __init__(self):
        self.title = font("Outfit.ttf", 67, 800)
        self.caption = font("Outfit.ttf", 83, 800)
        self.end = font("Outfit.ttf", 76, 800)
        self.label = font("JetBrainsMono.ttf", 29, 500)
        self.footer_end = font("JetBrainsMono.ttf", 32, 500)


def wrap(draw, text, face, max_width):
    lines, line = [], ""
    for word in str(text or "").split():
        candidate = f"{line} {word}" if line else word
        if line and draw.textlength(candidate, font=face) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def block_positions(lines, centre_y, line_height):
    top = centre_y - ((len(lines) - 1) * line_height) / 2
    return [(line, top + index * line_height) for index, line in enumerate(lines)]


def draw_shadowed_lines(frame, lines, face, centre_y, line_height):
    """White text with the reel's soft black glow (canvas shadowBlur 28, offsetY 2)."""
    placed = block_positions(lines, centre_y, line_height)
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    for line, y in placed:
        shadow_draw.text((W / 2, y + 2), line, font=face, fill=(0, 0, 0, 217), anchor="mm")
    frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    draw = ImageDraw.Draw(frame)
    for line, y in placed:
        draw.text((W / 2, y), line, font=face, fill=(255, 255, 255, 255), anchor="mm")


def draw_spaced(frame, text, face, y, fill, spacing):
    """A centred single line with letter spacing, like canvas letterSpacing."""
    draw = ImageDraw.Draw(frame)
    widths = [draw.textlength(char, font=face) for char in text]
    total = sum(widths) + spacing * len(text)
    x = W / 2 - total / 2
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=face, fill=fill, anchor="lm")
        x += width + spacing


def build_base(backdrop_path, logo):
    plate = Image.open(backdrop_path).convert("RGBA")
    scale = max(W / plate.width, H / plate.height)
    size = (round(plate.width * scale), round(plate.height * scale))
    plate = plate.resize(size, Image.LANCZOS)
    base = Image.new("RGBA", (W, H), (11, 12, 16, 255))
    base.alpha_composite(plate, ((W - size[0]) // 2, (H - size[1]) // 2))

    # Vertical gradient: 0.9 at the top, 0.25 at 45%, 0.95 at the bottom.
    gradient = Image.new("RGBA", (1, H))
    for y in range(H):
        t = y / (H - 1)
        if t <= 0.45:
            alpha = 0.9 + (0.25 - 0.9) * (t / 0.45)
        else:
            alpha = 0.25 + (0.95 - 0.25) * ((t - 0.45) / 0.55)
        gradient.putpixel((0, y), (11, 12, 16, round(alpha * 255)))
    base.alpha_composite(gradient.resize((W, H)))

    top_logo = logo.resize((203, 203), Image.LANCZOS)
    base.alpha_composite(top_logo, ((W - 203) // 2, 60))
    return base


def title_frame(base, fonts, story):
    frame = base.copy()
    draw = ImageDraw.Draw(frame)
    lines = wrap(draw, str(story.get("title") or "").upper(), fonts.title, 880)
    draw_shadowed_lines(frame, lines, fonts.title, 960, 79)
    # Label and footer sit a fixed gap from the title block (the app's positions
    # for a 3-line title), so a long title never runs over them.
    placed = block_positions(lines or [""], 960, 79)
    label = str(story.get("trope_label") or "STORY").upper()
    draw_spaced(frame, label, fonts.label, placed[0][1] - 81, PRIMARY + (255,), 5)
    draw_spaced(frame, "VIRAL VIBE STORIES", fonts.label, placed[-1][1] + 91, (255, 255, 255, 115), 5)
    return frame


def caption_frame(base, fonts, text):
    frame = base.copy()
    draw = ImageDraw.Draw(frame)
    lines = wrap(draw, str(text or "").upper(), fonts.caption, 900)
    draw_shadowed_lines(frame, lines, fonts.caption, 990, 93)
    return frame


def end_frame(base, fonts, logo):
    frame = base.copy()
    big = logo.resize((254, 254), Image.LANCZOS)
    glow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    silhouette = Image.new("RGBA", big.size, (0, 0, 0, 217))
    glow.paste(silhouette, ((W - 254) // 2, 702), big.split()[3])
    frame.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    frame.alpha_composite(big, ((W - 254) // 2, 700))
    draw = ImageDraw.Draw(frame)
    lines = wrap(draw, "Please like and follow for more videos", fonts.end, 880)
    draw_shadowed_lines(frame, lines, fonts.end, 1090, 88)
    draw_spaced(frame, "VIRAL VIBE STORIES", fonts.footer_end, 1230, (255, 255, 255, 153), 6)
    return frame


# ---------------------------------------------------------------------------
# Rendering one story
# ---------------------------------------------------------------------------

def render_story(story, work):
    fonts = Fonts()
    backdrop = download(story["backdrop_url"], work / "backdrop.img")
    logo = Image.open(download(story["logo_url"], work / "logo.img")).convert("RGBA")
    base = build_base(backdrop, logo)

    # Narration: title, every beat, then silence under the follow card.
    audio_parts = []
    title_wav = to_wav(download(story["title_audio_url"], work / "title.mp3"), work / "title.wav")
    audio_parts.append(title_wav)

    beats = []
    for index, beat in enumerate(story.get("beats") or []):
        mp3 = download(beat["audio_url"], work / f"beat{index}.mp3")
        wav = to_wav(mp3, work / f"beat{index}.wav")
        audio_parts.append(wav)
        beats.append((beat, duration_of(wav)))
    audio_parts.append(silence_wav(END_CARD_SECONDS, work / "end.wav"))

    # Frames, each with the time span it covers on the narration timeline.
    spans = []  # (png path, start seconds, end seconds)
    clock = 0.0

    title_seconds = duration_of(title_wav)
    path = work / "f_title.png"
    title_frame(base, fonts, story).convert("RGB").save(path, optimize=False)
    spans.append((path, clock, clock + title_seconds))
    clock += title_seconds

    for index, (beat, seconds) in enumerate(beats):
        pages = beat.get("pages") or [{"text": "", "start": 0, "end": 1}]
        for page_index, page in enumerate(pages):
            path = work / f"f_{index:02d}_{page_index:03d}.png"
            caption_frame(base, fonts, page.get("text")).convert("RGB").save(path, optimize=False)
            start = clock + seconds * float(page.get("start", 0))
            end = clock + seconds * float(page.get("end", 1))
            if page_index == len(pages) - 1:
                end = clock + seconds
            spans.append((path, start, end))
        clock += seconds

    path = work / "f_end.png"
    end_frame(base, fonts, logo).convert("RGB").save(path, optimize=False)
    spans.append((path, clock, clock + END_CARD_SECONDS))
    total = clock + END_CARD_SECONDS

    # Snap every boundary to the frame grid so the picture never drifts from the voice.
    lines = []
    for path, start, end in spans:
        frames = round(end * FPS) - round(start * FPS)
        if frames <= 0:
            continue
        lines.append(f"file '{path.name}'\nduration {frames / FPS:.6f}")
    lines.append(f"file '{spans[-1][0].name}'")
    (work / "frames.txt").write_text("\n".join(lines) + "\n")
    (work / "audio.txt").write_text("".join(f"file '{p.name}'\n" for p in audio_parts))

    run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(work / "audio.txt"), "-c:a", "pcm_s16le", str(work / "narration.wav"),
    ])

    output = work / "reel.mp4"
    for crf in (23, 28, 33):
        run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(work / "frames.txt"),
            "-i", str(work / "narration.wav"),
            "-map", "0:v", "-map", "1:a",
            "-vf", f"fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k",
            "-t", f"{total:.3f}", "-movflags", "+faststart",
            str(output),
        ])
        size = output.stat().st_size
        print(f"  encoded at crf {crf}: {size / 1048576:.1f} MB, {total:.1f}s")
        if size <= MAX_UPLOAD_BYTES:
            return output
    raise RuntimeError("The reel is still over 55 MB at the lowest quality setting.")


def main():
    if not APP_URL or not RENDER_KEY:
        sys.exit("APP_URL and RENDER_KEY must be set.")
    for name in ("Outfit.ttf", "JetBrainsMono.ttf"):
        if not (FONT_DIR / name).exists():
            sys.exit(f"Missing font {FONT_DIR / name}")

    rendered = 0
    while rendered < MAX_RENDERS:
        story = next_story()
        if not story:
            print("Nothing waiting to render.")
            break
        print(f"Rendering: {story['title']} ({story['id']})")
        work = Path(tempfile.mkdtemp(prefix="reel-"))
        try:
            video = render_story(story, work)
            result = upload_reel(story["id"], video)
            print(f"  uploaded: {result.get('reel_url')}")
            rendered += 1
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print(f"Done. Rendered {rendered} reel(s).")


if __name__ == "__main__":
    main()
