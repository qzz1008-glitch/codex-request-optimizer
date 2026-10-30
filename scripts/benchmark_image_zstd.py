"""Measure image JSON at zstd levels 1-22; emit statistics, never image data."""
import argparse
import base64
import csv
from io import BytesIO
import json
from pathlib import Path
import random
import statistics
import time

from PIL import Image, ImageDraw
import zstandard


def samples():
    rng = random.Random(1030)
    texture = Image.frombytes("RGB", (1400, 1000), rng.randbytes(1400*1000*3))
    screen = Image.new("RGB", (2000, 1400), "#f7f8fa")
    draw = ImageDraw.Draw(screen)
    for row in range(68):
        y = 18+row*20
        draw.rectangle((15, y, 1985, y+17), fill="#ffffff" if row%2 else "#e9edf2")
        draw.text((30, y+1), f"{row:03} Original request 1,775,538 B  input tokens 164244  image edit", fill="#25304a")
    logo = Image.new("RGBA", (1600, 1600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    for position, color in enumerate(("#00ba7c", "#368dff", "#ffaa2d", "#ad67e9")):
        x = 160+position*340
        draw.rounded_rectangle((x, 230, x+260, 1360-position*110), radius=80, fill=color)
    for name, image, fmt, options in (
        ("photo-texture-png", texture, "PNG", {}),
        ("photo-texture-jpeg", texture, "JPEG", {"quality":90}),
        ("photo-texture-webp", texture, "WEBP", {"quality":90}),
        ("text-screen-png", screen, "PNG", {}),
        ("text-screen-jpeg", screen, "JPEG", {"quality":90}),
        ("transparent-logo-png", logo, "PNG", {}),
        ("transparent-logo-webp", logo, "WEBP", {"lossless":True}),
    ):
        buffer = BytesIO()
        image.save(buffer, format=fmt, **options)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        body = {"model":"synthetic-image-benchmark", "images":[{"image_url":f"data:image/{fmt.lower()};base64,{encoded}"}],
                "prompt":"Preserve text, transparency, layout and reference details. "*80}
        yield name, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bodies = list(samples())
    timings = {level:[] for level in range(1, 23)}
    sizes = {}
    order = list(range(1, 23))
    rng = random.Random(1030)
    for round_index in range(args.rounds):
        rng.shuffle(order)
        for level in order:
            compressor = zstandard.ZstdCompressor(level=level)
            for name, body in bodies:
                start = time.perf_counter()
                compressed = compressor.compress(body)
                timings[level].append((time.perf_counter()-start)*1000)
                assert zstandard.ZstdDecompressor().decompress(compressed) == body
                sizes[level, name] = len(compressed)
        print(json.dumps({"benchmark_round":round_index+1,"samples":len(bodies)}), flush=True)
    rows = []
    total = sum(len(body) for _, body in bodies)
    for level in range(1, 23):
        rows.append({"level":level, "original_bytes":total, "compressed_bytes":sum(sizes[level, name] for name, _ in bodies),
                     "size_percent":round(sum(sizes[level, name] for name, _ in bodies)/total*100, 3),
                     "compression_ms_median":round(statistics.median(timings[level]), 3),
                     "compression_ms_total":round(sum(timings[level])/args.rounds, 3)})
    with (args.output_dir/"zstd-levels.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = {"fixture":"seven generated image fixtures: texture, text UI and transparent logo, PNG/JPEG/WebP",
              "rounds":args.rounds,"samples":[{"name":name,"original_bytes":len(body),
                "compressed_bytes_by_level":{level:sizes[level,name] for level in range(1,23)}} for name,body in bodies],"levels":rows}
    (args.output_dir/"zstd-levels.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"results":[row for row in rows if row["level"] in {1,3,6,9,12,16,17,19,22}]}), flush=True)


if __name__ == "__main__":
    main()
