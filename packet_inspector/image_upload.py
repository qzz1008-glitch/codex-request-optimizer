"""Optional bounded image downscaling for oversized JSON Images requests."""
import base64
from io import BytesIO
import json
import math
import re
import time
from dataclasses import dataclass

from .analyzer import JsonBytes, decode_body, encode
from .request_types import is_json_image_request
from .transport import prepare_request_info
from .usage_trigger import trigger_threshold

MAX_PIXELS = 32_000_000
MIN_LONG_SIDE = 256
MAX_PASSES = 2
INLINE_IMAGE = re.compile(r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\r\n]+)", re.I)


class UnsupportedImage(ValueError):
    pass


@dataclass
class InputImage:
    path: str
    image: object
    format: str
    original_bytes: int
    is_mask: bool = False


def reference(value, path):
    if isinstance(value, str):
        return value, path
    if isinstance(value, dict):
        for key in ("image_url", "url"):
            if key in value:
                return reference(value[key], path+"/"+key)
    return None, None


def read_image(uri, path, *, is_mask=False):
    from PIL import Image, ImageOps, UnidentifiedImageError
    match = INLINE_IMAGE.fullmatch(uri)
    if not match:
        raise UnsupportedImage("unsupported_image_reference")
    try:
        payload = base64.b64decode(match[2].replace("\r", "").replace("\n", ""), validate=True)
        with Image.open(BytesIO(payload)) as source:
            if source.width*source.height > MAX_PIXELS:
                raise UnsupportedImage("image_pixel_limit")
            if getattr(source, "is_animated", False):
                raise UnsupportedImage("animated_image")
            fmt = source.format
            if fmt not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise UnsupportedImage("unsupported_image_format")
            image = ImageOps.exif_transpose(source)
            image.load()
            alpha = "A" in image.getbands() or "transparency" in image.info
            if is_mask and not alpha:
                image.close()
                raise UnsupportedImage("mask_alpha_missing")
            # Keep transparent and indexed graphics in PNG. JPEG sources stay
            # JPEG; never flatten transparency onto a background.
            output_format = "PNG" if alpha or is_mask or fmt in {"PNG", "GIF"} else fmt
            converted = image.convert("RGBA" if alpha or is_mask else "RGB")
            image.close()
            return InputImage(path, converted, output_format, len(payload), is_mask)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        if isinstance(exc, UnsupportedImage):
            raise
        raise UnsupportedImage("invalid_image") from None


def replace_strings(raw, index, replacements):
    result = raw
    for path, value in sorted(replacements.items(), key=lambda item:index.spans[item[0]][0], reverse=True):
        start, end = index.spans[path]
        result = result[:start]+encode(value)+result[end:]
    return result


def resized_uri(item, size):
    from PIL import Image
    resized = item.image.resize(size, Image.Resampling.LANCZOS)
    try:
        output = BytesIO()
        options = {"compress_level":6} if item.format == "PNG" else {"quality":90}
        if item.format == "WEBP":
            options["method"] = 4
        profile = item.image.info.get("icc_profile")
        if profile:
            options["icc_profile"] = profile
        resized.save(output, format=item.format, **options)
        payload = output.getvalue()
        mime = "jpeg" if item.format == "JPEG" else item.format.lower()
        return "data:image/"+mime+";base64,"+base64.b64encode(payload).decode("ascii"), len(payload)
    finally:
        resized.close()


def optimize_image_request(raw, headers, path, prepared, *, budget_bytes, zstd_level):
    """Estimate once, correct at most once; every pass starts from originals.

    No image files or caches are created. The caller has already selected the
    single zstd level used throughout this request, including resized bodies.
    """
    if not is_json_image_request(path, headers) or len(prepared.body) <= budget_bytes:
        return prepared, {}
    encoding = next((value for key, value in headers.items() if key.lower() == "content-encoding"), "identity")
    if encoding.strip().lower() not in {"", "identity", "zstd"}:
        return prepared, {}
    started = time.monotonic()
    metrics = {"image_upload_optimized":False, "image_upload_iterations":0,
               "image_upload_before_bytes":len(prepared.body), "image_upload_target_bytes":trigger_threshold(budget_bytes)}
    images = []
    try:
        decoded = decode_body(raw, encoding)
        value = json.loads(decoded)
        if not isinstance(value, dict) or not isinstance(value.get("images"), list):
            raise UnsupportedImage("no_inline_images")
        for position, item in enumerate(value["images"]):
            uri, pointer = reference(item, "/images/"+str(position))
            if isinstance(uri, str) and uri.lower().startswith("data:"):
                images.append(read_image(uri, pointer))
                if sum(image.image.width*image.image.height for image in images) > MAX_PIXELS:
                    raise UnsupportedImage("image_pixel_limit")
        if not images:
            raise UnsupportedImage("no_inline_images")
        if value.get("mask") is not None:
            uri, pointer = reference(value["mask"], "/mask")
            if not uri or not (images[0].path == "/images/0" or images[0].path.startswith("/images/0/")):
                raise UnsupportedImage("unsupported_mask_reference")
            mask = read_image(uri, pointer, is_mask=True)
            images.append(mask)
            if mask.image.size != images[0].image.size:
                raise UnsupportedImage("mask_dimension_mismatch")
            images[0].format = "PNG"
        if sum(image.image.width*image.image.height for image in images) > MAX_PIXELS:
            raise UnsupportedImage("image_pixel_limit")
        index = JsonBytes(decoded)
        empty = replace_strings(decoded, index, {item.path:"" for item in images})
        fixed_bytes = len(prepare_request_info(empty, {"Content-Type":"application/json"}, "POST", path, "zstd", zstd_level).body)
        target = metrics["image_upload_target_bytes"]
        if fixed_bytes >= target:
            raise UnsupportedImage("non_image_body_exceeds_target")
        scale = min(.98, math.sqrt((target-fixed_bytes)/max(1, len(prepared.body)-fixed_bytes))*.92)
        best, details = prepared, []
        previous_sizes = None
        for attempt in range(1, MAX_PASSES+1):
            replacements, current_details = {}, []
            sizes = []
            for item in images:
                factor = max(scale, min(1, MIN_LONG_SIDE/max(item.image.size)))
                sizes.append(tuple(max(1, round(length*factor)) for length in item.image.size))
            if sizes == previous_sizes:
                break
            previous_sizes = sizes
            for item, size in zip(images, sizes):
                uri, count = resized_uri(item, size)
                replacements[item.path] = uri
                current_details.append({"field":item.path, "original_width":item.image.width, "original_height":item.image.height,
                                        "width":size[0], "height":size[1], "format":item.format,
                                        "original_bytes":item.original_bytes, "bytes":count, "mask":item.is_mask})
            changed = replace_strings(decoded, index, replacements)
            candidate = prepare_request_info(changed, {"Content-Type":"application/json"}, "POST", path, "zstd", zstd_level)
            metrics["image_upload_iterations"] = attempt
            if len(candidate.body) < len(best.body):
                best, details = candidate, current_details
            if len(best.body) <= target:
                break
            # One measured correction, with a margin for non-linear image
            # encoding. Resize originals again instead of resampling a result.
            scale *= min(.9, math.sqrt((target-fixed_bytes)/max(1, len(candidate.body)-fixed_bytes))*.9)
        changed = best is not prepared
        metrics.update(image_upload_optimized=changed, image_upload_after_bytes=len(best.body), image_upload_images=details,
                       image_upload_outcome="within_budget" if len(best.body) <= budget_bytes else "over_budget")
        return best, metrics
    except UnsupportedImage as exc:
        metrics.update(image_upload_outcome=str(exc), image_upload_after_bytes=len(prepared.body))
        return prepared, metrics
    except (ValueError, KeyError):
        metrics.update(image_upload_outcome="invalid_image_request", image_upload_after_bytes=len(prepared.body))
        return prepared, metrics
    finally:
        for item in images:
            item.image.close()
        metrics["image_upload_duration_ms"] = round((time.monotonic()-started)*1000, 1)
