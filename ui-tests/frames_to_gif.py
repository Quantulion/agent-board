"""Собирает GIF из кадров ui-tests/readme-media.mjs.

Кадры записаны только при изменении экрана и неравномерно; здесь они приводятся к постоянной частоте,
одинаковые подряд склеиваются, а палитра общая на весь ролик, чтобы цвета интерфейса не мерцали.
Нужен Pillow: python3 -m pip install pillow.
Запуск: python3 ui-tests/frames_to_gif.py ui-tests/out/readme/frames docs/media/demo.gif [--fps 12] [--colors 128]
"""
import argparse
import json
from pathlib import Path

from PIL import Image


def timeline(frames, fps):
    """Индексы кадров, видимых в моменты 0, 1/fps, 2/fps… до конца записи."""
    starts, t = [], 0
    for f in frames:
        starts.append(t)
        t += f["duration"]
    step, picked, i = 1000 / fps, [], 0
    moment = 0.0
    while moment < t:
        while i + 1 < len(starts) and starts[i + 1] <= moment:
            i += 1
        picked.append(i)
        moment += step
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("frames_dir")
    parser.add_argument("output")
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--colors", type=int, default=128)
    args = parser.parse_args()

    src = Path(args.frames_dir)
    frames = json.loads((src / "frames.json").read_text())
    picked = timeline(frames, args.fps)

    # одинаковые подряд кадры склеиваются в один с суммарной длительностью
    tick = round(1000 / args.fps)
    runs = []
    for idx in picked:
        if runs and runs[-1][0] == idx:
            runs[-1][1] += tick
        else:
            runs.append([idx, tick])
    images, durations = [], []
    for idx, duration in runs:
        img = Image.open(src / frames[idx]["file"]).convert("RGB")
        if images and img.tobytes() == images[-1].tobytes():
            durations[-1] += duration
            continue
        images.append(img)
        durations.append(duration)

    # общая палитра по выборке кадров со всего ролика
    sample = images[:: max(1, len(images) // 12)]
    w, h = sample[0].size
    sheet = Image.new("RGB", (w, h * len(sample)))
    for n, img in enumerate(sample):
        sheet.paste(img, (0, h * n))
    palette = sheet.quantize(colors=args.colors, method=Image.Quantize.MEDIANCUT)
    quantized = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in images]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    quantized[0].save(out, save_all=True, append_images=quantized[1:], duration=durations, loop=0, optimize=True, disposal=1)
    print(f"{out}: {len(quantized)} кадров, {sum(durations) / 1000:.1f} с, {out.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()
