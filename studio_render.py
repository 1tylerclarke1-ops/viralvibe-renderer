"""Viral Studios renderer: one episode of a series as a moving, layered video.

Only used for jobs with "motion": true (Viral Studios). Viral Vibe Stories jobs
never reach this file, so their videos are unchanged.

Layers, bottom to top:
  1. Pictures: three shots per scene, each with a moving camera (push in, drift,
     ease out). The part opens on a few seconds of real AI video when there is one,
     and in every-shot studios each shot gets its own AI clip too.
  2. Atmosphere: rain, fog, dust or embers drifting over the picture, film grain
     and a soft vignette, chosen per studio.
  3. Captions: the spoken words, a few at a time, with the word being said
     lit up in colour. Title card at the start, closing card at the end.
Sound: the narration, with a quiet music bed underneath that dips while the
narrator speaks. A thumbnail (first shot, hook line, part number) is made too.
"""

import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import render as core

W, H, FPS = core.W, core.H, core.FPS
HIGHLIGHT = (255, 214, 10, 255)  # the word being spoken
ATMOS_W, ATMOS_H = 540, 960  # atmosphere is drawn at half size, then scaled up
LOOP_SECONDS = 8


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def build_timeline(story, work):
    """Downloads the narration and works out what is on screen when.

    Returns (audio_parts, plates, captions, total, title_seconds, end_start):
      plates:   [(image_path, start, end)] for the moving pictures
      captions: [(kind, payload, start, end)] for the overlay cards
      shot_clips: [(clip_url, start, end)] AI video for a shot (every-shot studios only)
    """
    get = core.download
    backdrop = get(story["backdrop_url"], work / "backdrop.img")
    images = {}

    def image(url):
        if not url:
            return backdrop
        if url not in images:
            try:
                images[url] = get(url, work / f"shot{len(images):03d}.img")
            except Exception as error:  # a missing picture falls back to the opening still
                print(f"  picture unavailable, using backdrop: {error}")
                images[url] = backdrop
        return images[url]

    audio, plates, captions, shot_clips = [], [], [], []
    clock = 0.0

    title_wav = core.to_wav(get(story["title_audio_url"], work / "title.mp3"), work / "title.wav")
    audio.append(title_wav)
    title_seconds = core.duration_of(title_wav)
    plates.append((image(story.get("title_image_url") or ""), 0.0, title_seconds))
    captions.append(("title", None, 0.0, title_seconds))
    clock = title_seconds

    for index, beat in enumerate(story.get("beats") or []):
        wav = core.to_wav(get(beat["audio_url"], work / f"line{index:03d}.mp3"), work / f"line{index:03d}.wav")
        audio.append(wav)
        seconds = core.duration_of(wav)
        # Each picture with its moving clip, if it has one ("clips" lines up with "images").
        given = beat.get("images") or []
        clip_list = beat.get("clips") or []
        reverse_list = beat.get("clips_reverse") or []
        pairs = [
            (url, clip_list[i] if i < len(clip_list) else "", bool(reverse_list[i]) if i < len(reverse_list) else False)
            for i, url in enumerate(given)
            if url
        ]
        pairs = pairs or [(beat.get("image_url") or "", "", False)]
        share = seconds / len(pairs)
        for position, (url, clip_url, reverse) in enumerate(pairs):
            start = clock + share * position
            end = clock + seconds if position == len(pairs) - 1 else start + share
            plates.append((image(url), start, end))
            if clip_url:
                shot_clips.append((clip_url, start, end, reverse))
        for page in beat.get("pages") or []:
            start = clock + seconds * float(page.get("start", 0))
            end = clock + seconds * float(page.get("end", 1))
            captions.append(("page", page.get("text") or "", start, end))
        clock += seconds

    end_start = clock
    if story.get("outro_audio_url"):
        outro = core.to_wav(get(story["outro_audio_url"], work / "outro.mp3"), work / "outro.wav")
        audio.append(outro)
        audio.append(core.silence_wav(0.8, work / "end.wav"))
        end_seconds = core.duration_of(outro) + 0.8
    else:
        audio.append(core.silence_wav(core.END_CARD_SECONDS, work / "end.wav"))
        end_seconds = core.END_CARD_SECONDS
    total = end_start + end_seconds
    plates.append((backdrop, end_start, total))
    captions.append(("end", None, end_start, total))

    if story.get("clip_mode") == "settle":
        settled = []
        for number, (clip_url, start, end, reverse) in enumerate(shot_clips):
            try:
                path = get(clip_url, work / f"clip{number:03d}.mp4")
                length = min(core.duration_of(path), end - start - 0.5)
                if length < 1.0:
                    continue
                settled.append((path, start, length, reverse))
                plates = split_plate(plates, start + length)
            except Exception as error:  # a broken clip leaves that shot a moving still
                print(f"  shot clip {number + 1} skipped: {error}")
        shot_clips = settled
        story["_opener"] = None
        if story.get("opener_url"):
            try:
                path = get(story["opener_url"], work / "opener.mp4")
                length = min(core.duration_of(path), max(2.0, end_start - 0.5))
                story["_opener"] = (path, length, bool(story.get("opener_reverse")))
                plates = split_plate(plates, length)
            except Exception as error:
                print(f"  opening clip skipped: {error}")
    return audio, plates, captions, total, title_seconds, end_start, shot_clips


def split_plate(plates, at):
    """Cuts the picture showing at time `at` in two, so its camera move starts afresh
    (on the whole picture) exactly when a clip that ends on that picture finishes."""
    out = []
    for image, start, end in plates:
        if start < at - 0.05 and end > at + 0.05:
            out.append((image, start, at))
            out.append((image, at, end))
        else:
            out.append((image, start, end))
    return out


# ---------------------------------------------------------------------------
# Captions with the spoken word lit up
# ---------------------------------------------------------------------------

def word_weight(word):
    chars = sum(ch.isalnum() for ch in word)
    weight = max(2, chars) + 1
    if word[-1:] in ".!?":
        weight += 4
    elif word[-1:] in ",;:":
        weight += 2
    return weight


CAPTION_Y = 1300  # centre of the caption block: lower third, clear of faces and of TikTok's buttons


def caption_words_frame(fonts, words, lit, centre_y=CAPTION_Y):
    """The caption page with word number `lit` in the highlight colour."""
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)
    face = fonts.caption
    space = draw.textlength(" ", font=face)
    # Wrap into lines of words (max width 900), remembering each word's index.
    lines, line, width = [], [], 0.0
    for index, word in enumerate(words):
        w = draw.textlength(word, font=face)
        if line and width + space + w > 900:
            lines.append((line, width))
            line, width = [], 0.0
        width = width + (space if line else 0) + w
        line.append((index, word, w))
    if line:
        lines.append((line, width))
    placed = core.block_positions([None] * len(lines), centre_y, 93)
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    for (items, line_width), (_, y) in zip(lines, placed):
        x = W / 2 - line_width / 2
        for index, word, w in items:
            shadow_draw.text((x, y + 2), word, font=face, fill=(0, 0, 0, 230), anchor="lm")
            x += w + space
    frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    for (items, line_width), (_, y) in zip(lines, placed):
        x = W / 2 - line_width / 2
        for index, word, w in items:
            fill = HIGHLIGHT if index == lit else (255, 255, 255, 255)
            draw.text((x, y), word, font=face, fill=fill, anchor="lm", stroke_width=3, stroke_fill=(0, 0, 0, 200))
            x += w + space
    return frame


def build_overlay(story, captions, work):
    """Transparent caption cards, each with the time span it is on screen."""
    fonts = core.Fonts()
    clear = core.build_base(None, None)
    spans = []
    count = 0

    def save(image, start, end):
        nonlocal count
        path = work / f"o_{count:05d}.png"
        count += 1
        image.save(path, optimize=False)
        spans.append((path, start, end))

    word_mode = bool(story.get("word_captions"))
    centre_y = int(story.get("caption_y") or CAPTION_Y)
    for kind, text, start, end in captions:
        if kind == "title":
            save(core.title_frame(clear, fonts, story), start, end)
        elif kind == "end":
            save(core.end_frame(clear, fonts, None, story), start, end)
        elif not word_mode:
            save(core.caption_frame(clear, fonts, text), start, end)
        else:
            words = str(text or "").upper().split()
            if not words:
                continue
            weights = [word_weight(word) for word in words]
            total = sum(weights)
            clock = start
            for index, weight in enumerate(weights):
                stop = end if index == len(words) - 1 else clock + (end - start) * weight / total
                save(caption_words_frame(fonts, words, index, centre_y), clock, stop)
                clock = stop

    lines = []
    for path, start, end in spans:
        frames = round(end * FPS) - round(start * FPS)
        if frames <= 0:
            continue
        lines.append(f"file '{path.name}'\nduration {frames / FPS:.6f}")
    lines.append(f"file '{spans[-1][0].name}'")
    (work / "overlay.txt").write_text("\n".join(lines) + "\n")
    return work / "overlay.txt"


# ---------------------------------------------------------------------------
# Atmosphere (a seamless 8-second loop, drawn with numpy)
# ---------------------------------------------------------------------------

def _soft_dot(radius):
    size = radius * 2 + 1
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    return np.clip(1 - np.sqrt(xx ** 2 + yy ** 2) / (radius + 0.01), 0, 1) ** 2


def _stamp(canvas, x, y, sprite, strength):
    r = sprite.shape[0] // 2
    x, y = int(x), int(y)
    x0, y0, x1, y1 = x - r, y - r, x + r + 1, y + r + 1
    if x1 <= 0 or y1 <= 0 or x0 >= canvas.shape[1] or y0 >= canvas.shape[0]:
        return
    sx0, sy0 = max(0, -x0), max(0, -y0)
    sx1 = sprite.shape[1] - max(0, x1 - canvas.shape[1])
    sy1 = sprite.shape[0] - max(0, y1 - canvas.shape[0])
    canvas[max(0, y0):min(canvas.shape[0], y1), max(0, x0):min(canvas.shape[1], x1)] += sprite[sy0:sy1, sx0:sx1] * strength


def atmosphere_frames(kind):
    """Yields RGB uint8 frames (ATMOS_H x ATMOS_W) of a seamless loop, drawn on black
    so it can be screened over the picture."""
    rng = random.Random(7)
    n = LOOP_SECONDS * FPS
    if kind == "rain":
        drops = [(rng.random() * ATMOS_W, rng.random(), rng.choice([2, 3, 4]), rng.uniform(0.25, 0.6)) for _ in range(260)]
        for f in range(n):
            t = f / n
            img = Image.new("L", (ATMOS_W, ATMOS_H), 0)
            d = ImageDraw.Draw(img)
            for x, y0, laps, alpha in drops:
                y = ((y0 + laps * t) % 1.0) * (ATMOS_H + 60) - 30
                xx = x + (y / ATMOS_H) * 25  # a slight slant
                d.line([(xx, y), (xx - 4, y - 26 - laps * 6)], fill=int(255 * alpha), width=1)
            arr = np.asarray(img.filter(ImageFilter.GaussianBlur(0.6)), dtype=np.float32)
            rgb = np.stack([arr * 0.75, arr * 0.82, arr * 0.95], axis=-1)
            yield np.clip(rgb, 0, 255).astype(np.uint8)
    elif kind in ("embers", "dust"):
        embers = kind == "embers"
        count = 110 if embers else 90
        dots = [(rng.random(), rng.random(), rng.choice([1, 2]) * (1 if embers else 1), rng.uniform(0, 2 * math.pi),
                 rng.choice([2, 3, 4]), rng.uniform(0.35, 1.0)) for _ in range(count)]
        sprites = {r: _soft_dot(r) for r in (3, 5, 7, 9, 12)}
        for f in range(n):
            t = f / n
            canvas = np.zeros((ATMOS_H, ATMOS_W), dtype=np.float32)
            for x0, y0, laps, phase, size, alpha in dots:
                if embers:  # rising and flickering
                    y = ((y0 - laps * t) % 1.0) * ATMOS_H
                    flicker = 0.6 + 0.4 * math.sin(2 * math.pi * (3 * t) + phase)
                else:  # slow drifting motes
                    y = ((y0 + 0.5 * laps * t) % 1.0) * ATMOS_H
                    flicker = 0.7 + 0.3 * math.sin(2 * math.pi * t + phase)
                x = (x0 * ATMOS_W + 18 * math.sin(2 * math.pi * t + phase)) % ATMOS_W
                radius = ({2: 5, 3: 9, 4: 12} if embers else {2: 3, 3: 5, 4: 7})[size]
                _stamp(canvas, x, y, sprites[radius], (330 if embers else 255) * alpha * flicker)
            canvas = np.clip(canvas, 0, 255)
            tint = (1.0, 0.55, 0.2) if embers else (0.9, 0.85, 0.7)
            yield np.stack([canvas * tint[0], canvas * tint[1], canvas * tint[2]], axis=-1).astype(np.uint8)
    elif kind == "fog":
        noise = Image.fromarray((np.random.default_rng(3).random((48, 54)) * 255).astype(np.uint8))
        field = np.asarray(noise.resize((ATMOS_W * 2, ATMOS_H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(40)), dtype=np.float32)
        field = (field - field.min()) / (field.max() - field.min() + 1e-6)
        # The fog sits mostly in the lower half of the frame.
        weight = np.linspace(0.25, 1.0, ATMOS_H, dtype=np.float32)[:, None]
        for f in range(n):
            shift = int((f / n) * ATMOS_W)
            window = np.roll(field, -shift, axis=1)[:, :ATMOS_W]
            value = np.clip(window * weight * 120, 0, 255)
            yield np.stack([value * 0.85, value * 0.9, value], axis=-1).astype(np.uint8)
    else:
        return


def make_atmosphere(kind, work):
    if kind not in ("rain", "fog", "dust", "embers"):
        return None
    path = work / "atmos.mp4"
    proc = subprocess.Popen([
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{ATMOS_W}x{ATMOS_H}", "-r", str(FPS), "-i", "-",
        "-vf", f"scale={W}:{H}:flags=bicubic,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(path),
    ], stdin=subprocess.PIPE)
    for frame in atmosphere_frames(kind):
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("Could not make the atmosphere layer.")
    return path


ATMOS_STRENGTH = {"rain": 0.55, "fog": 0.45, "dust": 0.6, "embers": 0.8}


# ---------------------------------------------------------------------------
# Music bed (made from scratch, so there is nothing to licence)
# ---------------------------------------------------------------------------

MUSIC = {
    # A low, uneasy drone with a slow pulse.
    "dark": "sine=f=55:r=44100[a];sine=f=82.41:r=44100[b];sine=f=58.27:r=44100[c];"
            "anoisesrc=color=brown:r=44100:a=0.25[n];"
            "[a][b][c][n]amix=inputs=4:normalize=0,lowpass=f=420,tremolo=f=0.15:d=0.6,volume=0.55",
    # A soft, warm minor chord, slowly breathing.
    "warm": "sine=f=110:r=44100[a];sine=f=164.81:r=44100[b];sine=f=220:r=44100[c];sine=f=261.63:r=44100[d];"
            "[a][b][c][d]amix=inputs=4:normalize=0,lowpass=f=900,tremolo=f=0.1:d=0.5,volume=0.35",
    # A gentle, bright major chord.
    "light": "sine=f=261.63:r=44100[a];sine=f=329.63:r=44100[b];sine=f=392:r=44100[c];sine=f=523.25:r=44100[d];"
             "[a][b][c][d]amix=inputs=4:normalize=0,lowpass=f=1500,tremolo=f=0.25:d=0.5,volume=0.22",
}


def mix_audio(story, narration, total, work):
    """Narration plus the studio's music bed, which dips while the narrator speaks."""
    mood = story.get("music") or "none"
    if mood not in MUSIC:
        return narration
    bed = work / "bed.wav"
    core.run([
        "ffmpeg", "-y", "-v", "error", "-filter_complex",
        f"{MUSIC[mood]},aformat=channel_layouts=stereo,"
        f"afade=t=in:d=2,afade=t=out:st={max(0.0, total - 2.5):.2f}:d=2.5",
        "-t", f"{total:.3f}", "-ar", "44100", str(bed),
    ])
    mixed = work / "mixed.wav"
    core.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(narration), "-i", str(bed),
        "-filter_complex",
        "[1:a]volume=0.22[m];[m][0:a]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=600[duck];"
        "[0:a][duck]amix=inputs=2:normalize=0:duration=first[out]",
        "-map", "[out]", "-ar", "44100", str(mixed),
    ])
    return mixed


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------

def make_thumbnail(story, plates, work):
    """The part's first shot with its hook line and part number, as a JPEG."""
    picture = Image.open(plates[0][0]).convert("RGB")
    scale = max(W / picture.width, H / picture.height)
    picture = picture.resize((round(picture.width * scale), round(picture.height * scale)), Image.LANCZOS)
    left, top = (picture.width - W) // 2, (picture.height - H) // 2
    frame = picture.crop((left, top, left + W, top + H)).convert("RGBA")
    shade = Image.new("RGBA", (1, H))
    for y in range(H):
        t = y / (H - 1)
        alpha = 0.75 * max(0.0, 1 - t / 0.5) + 0.6 * max(0.0, (t - 0.7) / 0.3)
        shade.putpixel((0, y), (0, 0, 0, round(min(1.0, alpha) * 255)))
    frame.alpha_composite(shade.resize((W, H)))
    draw = ImageDraw.Draw(frame)
    face = core.font("Outfit.ttf", 118, 900)
    lines = core.wrap(draw, str(story.get("thumb_text") or story.get("title") or "").upper(), face, 960)[:4]
    y = 260
    for line in lines:
        draw.text((W / 2, y), line, font=face, fill=(255, 255, 255, 255), anchor="mm", stroke_width=8, stroke_fill=(0, 0, 0, 255))
        y += 132
    label = str(story.get("part_label") or "").upper()
    if label:
        badge = core.font("Outfit.ttf", 70, 900)
        width = draw.textlength(label, font=badge) + 80
        x0, y0 = (W - width) / 2, H - 330
        draw.rounded_rectangle((x0, y0, x0 + width, y0 + 120), radius=26, fill=HIGHLIGHT)
        draw.text((W / 2, y0 + 60), label, font=badge, fill=(10, 10, 10, 255), anchor="mm")
    path = work / "thumb.jpg"
    frame.convert("RGB").save(path, quality=90)
    return path


# ---------------------------------------------------------------------------
# The episode
# ---------------------------------------------------------------------------

def render_studio(story, work):
    audio, plates, captions, total, title_seconds, end_start, shot_clips = build_timeline(story, work)
    (work / "audio.txt").write_text("".join(f"file '{p.name}'\n" for p in audio))
    core.run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
        "-i", str(work / "audio.txt"), "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(work / "narration.wav"),
    ])
    sound = mix_audio(story, work / "narration.wav", total, work)

    background = core.moving_background(plates, work)
    overlay = build_overlay(story, captions, work)
    atmos = make_atmosphere(story.get("atmosphere") or "grain", work)

    inputs = ["-i", str(background), "-f", "concat", "-safe", "0", "-i", str(overlay), "-i", str(sound)]
    chain = f"[0:v]fps={FPS},format=gbrp[pic0]"
    current = "pic0"
    next_input = 3

    settle = story.get("clip_mode") == "settle"

    def settle_clip(path, start, length, reverse, label):
        """A clip that plays once and comes to rest on its picture (backwards if it was
        made starting from the picture), with a short blend into the moving still."""
        nonlocal chain, current, next_input
        blend = min(0.3, length / 5)
        inputs.extend(["-i", str(path)])
        chain += (
            f";[{next_input}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"trim=0:{length:.3f},setpts=PTS-STARTPTS{',reverse' if reverse else ''},format=rgba,"
            f"fade=t=out:st={length - blend:.3f}:d={blend:.3f}:alpha=1,"
            f"setpts=PTS+{start:.3f}/TB[{label}];"
            f"[{current}][{label}]overlay=0:0:eof_action=pass:format=auto,format=gbrp[{label}o]"
        )
        current = f"{label}o"
        next_input += 1

    if settle:
        if story.get("_opener"):
            path, length, reverse = story["_opener"]
            settle_clip(path, 0.0, length, reverse, "op")
            print(f"  opening clip: {length:.1f}s (settles on the picture)")
        for number, (path, start, length, reverse) in enumerate(shot_clips):
            settle_clip(path, start, length, reverse, f"sc{number}")
        if shot_clips:
            print(f"  shot clips: {len(shot_clips)} (each settles on its picture)")

    # The opening clip: real AI video for the first few seconds, fading into the moving shots.
    if not settle and story.get("opener_url"):
        try:
            clip = core.download(story["opener_url"], work / "opener.mp4")
            length = min(core.duration_of(clip), max(2.0, end_start - 0.5))
            fade = min(0.8, length / 3)
            inputs += ["-i", str(clip)]
            chain += (
                f";[{next_input}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"trim=0:{length:.3f},setpts=PTS-STARTPTS,format=rgba,"
                f"fade=t=out:st={length - fade:.3f}:d={fade:.3f}:alpha=1[clip];"
                f"[{current}][clip]overlay=0:0:eof_action=pass:format=auto,format=gbrp[pic1]"
            )
            current = "pic1"
            next_input += 1
            print(f"  opening clip: {length:.1f}s")
        except Exception as error:  # a broken clip never stops the episode
            print(f"  opening clip skipped: {error}")

    # Every-shot clips: each shot cuts straight to its AI video, which plays forward then
    # back (a seamless loop, so a 5-second clip covers up to 10 seconds of the shot), then
    # fades into the moving still for whatever is left of the shot.
    for number, (clip_url, start, end, _reverse) in enumerate([] if settle else shot_clips):
        try:
            clip = core.download(clip_url, work / f"clip{number:03d}.mp4")
            one_way = core.duration_of(clip)
            length = min(end - start, one_way * 2)
            if length < 1.0:
                continue
            fade = min(0.8, length / 4)
            label = f"sc{number}"
            inputs += ["-i", str(clip)]
            chain += (
                f";[{next_input}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"setpts=PTS-STARTPTS,split[{label}a][{label}b];[{label}b]reverse[{label}r];"
                f"[{label}a][{label}r]concat=n=2:v=1:a=0,trim=0:{length:.3f},setpts=PTS-STARTPTS,format=rgba,"
                f"fade=t=out:st={length - fade:.3f}:d={fade:.3f}:alpha=1,"
                f"setpts=PTS+{start:.3f}/TB[{label}];"
                f"[{current}][{label}]overlay=0:0:eof_action=pass:format=auto,format=gbrp[{label}o]"
            )
            current = f"{label}o"
            next_input += 1
        except Exception as error:  # a broken clip leaves that shot as a moving still
            print(f"  shot clip {number + 1} skipped: {error}")
    if shot_clips and not settle:
        print(f"  shot clips: {len(shot_clips)}")

    kind = story.get("atmosphere") or "grain"
    if atmos is not None:
        inputs = inputs[:0] + inputs + ["-stream_loop", "-1", "-i", str(atmos)]
        chain += (
            f";[{next_input}:v]fps={FPS},format=gbrp[fx];"
            f"[{current}][fx]blend=all_mode=screen:all_opacity={ATMOS_STRENGTH[kind]}:shortest=0[pic2]"
        )
        current = "pic2"
        next_input += 1

    # Film grain and a soft vignette for every studio except the clean cartoon ones.
    if kind != "none":
        chain += f";[{current}]format=yuv420p,noise=alls=7:allf=t,vignette=angle=PI/4.5[pic3]"
        current = "pic3"

    # Watermark: the handle, half see-through, swapping between the top corners every
    # 12 seconds so it cannot simply be cropped or blurred out of a stolen copy.
    mark = str(story.get("watermark") or "").strip()
    if mark:
        safe = "".join(ch for ch in mark if ch.isalnum() or ch in "@._- ")
        font = str((core.FONT_DIR / "Outfit.ttf").resolve()).replace(":", "\\:")
        chain += (
            f";[{current}]drawtext=fontfile='{font}':text='{safe}':fontsize=42:fontcolor=white@0.6:"
            f"shadowcolor=black@0.6:shadowx=2:shadowy=2:"
            f"x='if(lt(mod(t,24),12),70,w-tw-70)':y=250[wm]"
        )
        current = "wm"

    chain += f";[1:v]fps={FPS},format=rgba[ov];[{current}][ov]overlay=0:0:format=auto,format=yuv420p[v]"

    output = work / "reel.mp4"
    for crf in (23, 27, 31):
        core.run([
            "ffmpeg", "-y", "-v", "error", *inputs,
            "-filter_complex", chain,
            "-map", "[v]", "-map", "2:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "160k",
            "-t", f"{total:.3f}", "-movflags", "+faststart",
            str(output),
        ])
        size = output.stat().st_size
        print(f"  encoded at crf {crf}: {size / 1048576:.1f} MB, {total:.1f}s")
        if size <= core.MAX_UPLOAD_BYTES:
            break
    else:
        raise RuntimeError("The episode is still over 55 MB at the lowest quality setting.")

    thumb = None
    try:
        thumb = make_thumbnail(story, [p for p in plates[1:2]] or plates, work)
    except Exception as error:
        print(f"  thumbnail skipped: {error}")
    return output, thumb
