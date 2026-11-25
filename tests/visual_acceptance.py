"""Fixed-fixture sanity checks for visible garment transfer. These are not model-quality benchmarks."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
CASES = {
    "upper": ("person.jpg", (0.39, 0.22, 0.58, 0.37)),
    "lower": ("person-lower.png", (0.43, 0.52, 0.56, 0.62)),
    "dress": ("person.jpg", (0.39, 0.23, 0.58, 0.50)),
}


def verify_receipts():
    assets = {Path(item["destination"]).name: item for item in
              json.loads((ROOT / "inference/assets.lock.json").read_text())
              if item["destination"].startswith("samples/")}
    fixtures = [
        (EVIDENCE, "upper", "person.jpg", "garment.jpg"),
        (EVIDENCE / "lower", "lower", "person-lower.png", "lower.jpg"),
        (EVIDENCE / "dress", "dress", "person.jpg", "dress.jpg"),
        (EVIDENCE / "high-resolution", "upper", "person.jpg", "garment.jpg"),
        (EVIDENCE / "difficult-lower", "lower", "person.jpg", "lower.jpg"),
    ]
    for directory, category, person, garment in fixtures:
        receipt = json.loads((directory / "acceptance.json").read_text())
        task, downloaded = receipt["task"], receipt["downloaded"]
        assert (task["status"], task["mode"], task["category"]) == ("SUCCEEDED", "catvton", category)
        for role, name in (("person", person), ("garment", garment)):
            assert task["inputs"][role]["sha256"] == assets[name]["sha256"], (directory, role)
            assert task["inputs"][role]["bytes"] == assets[name]["bytes"], (directory, role)
        result = directory / "tryon-result.png"
        assert hashlib.sha256(result.read_bytes()).hexdigest() == downloaded["sha256"], result
        assert result.stat().st_size == downloaded["bytes"], result
        with Image.open(result) as image:
            assert image.format == "PNG" and list(image.size) == downloaded["dimensions"], result
            assert image.size == (receipt["model"]["width"], receipt["model"]["height"]), result
            image.verify()
    print(f"Verified {len(fixtures)} published PNGs against their receipts and pinned input manifest.")


def crop_array(image, roi):
    left, top, right, bottom = roi
    return np.asarray(image.crop((round(image.width * left), round(image.height * top),
                                  round(image.width * right), round(image.height * bottom))), dtype=np.float32)


def assess(category, source, roi):
    directory = EVIDENCE if category == "upper" else EVIDENCE / category
    receipt = json.loads((directory / "acceptance.json").read_text())
    result = directory / "tryon-result.png"
    assert hashlib.sha256(result.read_bytes()).hexdigest() == receipt["downloaded"]["sha256"]
    original_file = ROOT / "samples" / source
    assert hashlib.sha256(original_file.read_bytes()).hexdigest() == receipt["task"]["inputs"]["person"]["sha256"]
    with Image.open(result) as file:
        generated = file.convert("RGB")
    with Image.open(original_file) as file:
        original = ImageOps.fit(ImageOps.exif_transpose(file).convert("RGB"), generated.size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(original, generated)
    # Both public portraits leave their leftmost 5% as background.
    assert difference.crop((0, 0, generated.width // 20, generated.height)).getbbox() is None
    before, after = crop_array(original, roi), crop_array(generated, roi)
    pixel_change = np.abs(after - before).mean(axis=2)
    changed = float((pixel_change > 12).mean())
    assert changed > .40, (category, "garment area hardly changed", changed)

    if category == "upper":  # The purple T-shirt should become the warmer, cream printed top.
        metric = "red_minus_blue_increase"
        gain = float(((after[..., 0] - after[..., 2]) - (before[..., 0] - before[..., 2])).mean())
        assert gain > 20, (category, metric, gain)
    elif category == "lower":  # Navy denim shorts should become the much lighter reference skirt.
        metric = "mean_rgb_brightness_increase"
        gain = float(after.mean() - before.mean())
        assert gain > 30, (category, metric, gain)
    else:  # The floral dress contains prominent red petals absent from the original shirt.
        metric = "red_pixel_fraction_increase"
        def red_fraction(pixels):
            red, green, blue = pixels.transpose(2, 0, 1)
            return ((red > 98) & (red > green * 1.4) & (red > blue * 1.3)).mean()
        gain = float(red_fraction(after) - red_fraction(before))
        assert gain > .07, (category, metric, gain)
    return {"taskId": receipt["task"]["taskId"], "normalized_roi": roi,
            "background_left_5_percent_pixel_identical": True, "changed_fraction": round(changed, 4),
            metric: round(gain, 4)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts-only", action="store_true", help="verify published evidence without downloading inputs")
    args = parser.parse_args()
    verify_receipts()
    if not args.receipts_only:
        findings = {category: assess(category, *case) for category, case in CASES.items()}
        (EVIDENCE / "visual-checks.json").write_text(json.dumps(findings, indent=2) + "\n")
        for category, finding in findings.items():
            print(category, finding)
