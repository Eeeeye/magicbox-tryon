"""Garment preparation and mask geometry, independent of the inference runtime."""
import numpy as np
from PIL import Image, ImageFilter


class GarmentRegionMissing(ValueError):
    """The segmentation model could not locate clothing of the requested category."""


def prepare_garment(image):
    """Trim uniform catalogue margins and estimate whether the cut leaves arms exposed."""
    preview = image.copy()
    preview.thumbnail((300, 300))
    rgb = np.asarray(preview, dtype=np.int16)
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]))
    background = np.median(border, axis=0)
    variation = np.percentile(np.abs(border - background).max(axis=-1), 80)
    if variation > 24:
        return image, False
    foreground = np.abs(rgb - background).max(axis=-1) > max(12, 2 * variation)
    mask = np.asarray(Image.fromarray(foreground.astype("uint8")).filter(ImageFilter.MedianFilter(3)))
    rows = np.flatnonzero(mask.sum(axis=1) > max(2, preview.width * 0.018))
    cols = np.flatnonzero(mask.sum(axis=0) > max(2, preview.height * 0.018))
    if len(rows) < 16 or len(cols) < 12:
        return image, False
    top, bottom, left, right = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    silhouette = mask[top:bottom, left:right]
    widths = np.where(silhouette.any(axis=1),
        silhouette.shape[1] - silhouette.argmax(axis=1) - silhouette[:, ::-1].argmax(axis=1), 0)
    height = len(widths)
    shoulder_width = np.median(widths[int(.18 * height):int(.36 * height)])
    torso_width = np.median(widths[int(.46 * height):int(.72 * height)])
    exposed_arms = torso_width > 0 and shoulder_width < .95 * torso_width
    margin = max(2, round(.03 * max(right - left, bottom - top)))
    xscale, yscale = image.width / preview.width, image.height / preview.height
    box = (max(0, int((left - margin) * xscale)), max(0, int((top - margin) * yscale)),
           min(image.width, round((right + margin) * xscale)), min(image.height, round((bottom + margin) * yscale)))
    return image.crop(box), exposed_arms


def inference_region(mask, category):
    """Give the denoiser useful garment detail when a full-length person fills a small frame."""
    width, height = mask.size
    if category == "dress":
        return (0, 0, width, height)
    left, top, right, bottom = mask.getbbox()
    fraction = min(1.0, max(.55, (bottom - top) * 1.45 / height, (right - left) * 1.45 / width))
    w, h = round(width * fraction), round(height * fraction)
    x = min(max(0, (left + right - w) // 2), width - w)
    y = min(max(0, (top + bottom - h) // 2), height - h)
    return (x, y, x + w, y + h)


def garment_mask(labels, category, exposed_arms=False):
    clothes = {"upper": (4, 7), "lower": (5, 6), "dress": (4, 5, 6, 7, 8)}
    selected = np.isin(labels, clothes[category])
    if selected.mean() < 0.006:
        raise GarmentRegionMissing(category)
    # Front-facing photos can have hands just below or overlapping the shirt hem.
    # Keep distal skin on downward arms so denoising cannot redraw fingers.
    radius = max(2, round(labels.shape[1] / 48))
    fixed = np.isin(labels, (1, 2, 3, 9, 10, 11))
    if category in {"lower", "dress"}:
        selected |= np.isin(labels, (12, 13))
    if category != "lower" and not exposed_arms:
        rows = np.arange(labels.shape[0])[:, None]
        for arm_id in (14, 15):
            arm = labels == arm_id
            occupied = np.flatnonzero(arm.any(axis=1))
            if not occupied.size:
                continue
            selected |= arm
            top, bottom = occupied[0], occupied[-1]
            if bottom - top >= 0.12 * labels.shape[0]:
                hand = arm & (rows >= top + 0.75 * (bottom - top))
                fixed |= np.asarray(Image.fromarray(hand.astype("uint8")).filter(ImageFilter.MaxFilter(5))) > 0
    else:
        fixed |= np.isin(labels, (14, 15))
    if category in {"lower", "dress"}:
        # A skirt can cover the background gap between the legs and extend beyond trousers.
        occupied = selected.any(axis=1)[:, None]
        left = selected.argmax(axis=1)[:, None]
        right = labels.shape[1] - 1 - selected[:, ::-1].argmax(axis=1)[:, None]
        columns = np.arange(labels.shape[1])
        selected |= occupied & (columns >= left) & (columns <= right)
        radius = max(radius, round(labels.shape[1] / 29))
    if category == "lower":
        fixed |= labels == 4
    expanded = Image.fromarray(selected.astype("uint8") * 255).filter(ImageFilter.MaxFilter(2 * radius + 1))
    data = np.asarray(expanded).copy()
    data[fixed] = 0
    return Image.fromarray(data)


def blending_mask(synthesis_mask, labels, short_lower):
    # Give diffusion context for the leg pose. Preserve the photographed calves only when pasting.
    if not short_lower:
        return synthesis_mask
    clothing_rows = np.flatnonzero(np.isin(labels, (5, 6)).any(axis=1))
    if not clothing_rows.size:
        return synthesis_mask
    paste_mask = np.asarray(synthesis_mask).copy()
    keep_from = clothing_rows[-1] + max(1, round(labels.shape[0] * .09))
    paste_mask[keep_from:] = 0
    return Image.fromarray(paste_mask)
