"""
Build capture-conflict-review-flow.mp4 from capture-conflict-review-flow.html.

Frames: headless Chrome renders the page at ?t=<seconds>. Voice: macOS `say`.
Join: ffmpeg. 12 frames per second. The page is the only animation source.

  python docs/visuals/make_video.py
"""
import hashlib
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "capture-conflict-review-flow.html"
OUT = HERE / "capture-conflict-review-flow.mp4"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FPS = 12

# One narration line per step, in the same order as STEPS in the page.
NARRATION = [
    "You paste a clip into Capture. The app reads it and finds its main claims.",
    "Today, the app puts the clip in a queue. It compares the clip with your pages only after you press Approve. You do not see that comparison.",
    "With this change, the app finds the closest page first. It labels each claim as same, new, changed, or conflicts.",
    "You see the page and the clip side by side. A conflict shows the exact page quote. An exact duplicate is skipped, with a link to the page.",
    "Then you choose: add to the page, keep both, replace, or skip. Replace saves a copy of the old page first.",
]


def step_durations() -> list[int]:
    html = PAGE.read_text(encoding="utf-8")
    return [int(d) for d in re.findall(r"\{ dur: (\d+),", html)]


def render_frame(args: tuple[int, Path]) -> None:
    i, frames = args
    t = i / FPS
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=960,540",
         f"--screenshot={frames / f'{i:04d}.png'}", f"file://{PAGE}?t={t:.4f}"],
        check=True, capture_output=True,
    )


def build_audio(durs: list[int], work: Path) -> Path:
    parts = []
    for n, (text, dur) in enumerate(zip(NARRATION, durs)):
        aiff, wav = work / f"s{n}.aiff", work / f"s{n}.wav"
        subprocess.run(["say", "-o", str(aiff), text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "44100", "-ac", "1", str(wav)], check=True)
        length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                                      check=True, capture_output=True, text=True).stdout)
        if length > dur - 0.5:
            raise SystemExit(f"Narration for step {n + 1} is {length:.1f}s. Step lasts {dur}s. Shorten the text.")
        padded = work / f"p{n}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-af", f"apad=whole_dur={dur}", str(padded)], check=True)
        parts.append(padded)
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    audio = work / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(audio)], check=True)
    return audio


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    durs = step_durations()
    assert len(durs) == len(NARRATION), "one narration line per step"
    total = sum(durs)
    assert total < 60, f"video is {total}s, limit is 60s"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        frames = work / "frames"
        frames.mkdir()
        n_frames = total * FPS
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(render_frame, [(i, frames) for i in range(n_frames)]))
        # The picture must move: nearby frames inside each step must differ.
        start = 0
        for s, d in enumerate(durs):
            mid = (start + d // 2) * FPS
            moved = digest(frames / f"{mid:04d}.png") != digest(frames / f"{mid + 1:04d}.png")
            print(f"step {s + 1}: frames {mid} and {mid + 1} {'differ' if moved else 'ARE IDENTICAL'}")
            start += d
        audio = build_audio(durs, work)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames / "%04d.png"), "-i", str(audio),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-shortest", str(OUT)],
            check=True,
        )
    print(f"wrote {OUT} ({total}s, {FPS} fps)")


if __name__ == "__main__":
    main()
