"""Worker-owned image reading. The control plane never imports Pillow."""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import math
import warnings

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
from boring_worker_kit.files import FileTools
from .image_limits import (ADMISSION_SECONDS, ANSWER_BYTES, DEFAULT_PX, IMAGE_SLOTS,
                           JPEG_PIXELS, JPEG_QUALITIES, MAX_PX, MIN_PX, OTHER_PNG_PIXELS,
                           PROGRESSIVE_JPEG_BYTES, RGB_PNG_PIXELS, SOURCE_BYTES, WEBP_PIXELS)
from .outcomes import Refused


def _arguments(request: dict[str, object]) -> tuple[str, str, int, list[int] | None]:
    if set(request) - {"operation", "request_id", "root", "path", "max_px", "crop"}:
        raise Refused("invalid_request", "unknown image arguments")
    root, path = request.get("root"), request.get("path")
    if not isinstance(root, str) or not isinstance(path, str):
        raise Refused("invalid_request", "root and path must be strings")
    maximum = request.get("max_px", DEFAULT_PX)
    if type(maximum) is not int or not MIN_PX <= maximum <= MAX_PX:
        raise Refused("invalid_request", f"max_px must be an integer from {MIN_PX} to {MAX_PX}")
    crop = request.get("crop")
    if crop is not None and (not isinstance(crop, list) or len(crop) != 4
                             or any(type(v) is not int for v in crop)
                             or crop[0] < 0 or crop[1] < 0 or crop[2] <= 0 or crop[3] <= 0):
        raise Refused("invalid_request", "crop must be [x, y, w, h] with nonnegative origin and positive size")
    return root, path, maximum, crop


class ImageReader:
    """Serialize expensive decodes; a disconnected caller cannot free an active slot."""

    def __init__(self, files: FileTools) -> None:
        self.files = files
        self.slots = asyncio.Semaphore(IMAGE_SLOTS)

    async def call(self, request: dict[str, object]) -> dict[str, object]:
        root, path, maximum, crop = _arguments(request)
        try:
            await asyncio.wait_for(self.slots.acquire(), ADMISSION_SECONDS)
        except TimeoutError:
            raise Refused("busy", "all image operation slots are in use") from None

        async def work() -> dict[str, object]:
            data, sha256 = await self.files.read_whole(root, path, SOURCE_BYTES)
            return await asyncio.to_thread(_render, data, sha256, maximum, crop)

        task = asyncio.create_task(work())
        def finished(done: asyncio.Task) -> None:
            self.slots.release()
            # A cancelled requester may no longer await this task's exception.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)


def _to_srgb(image: Image.Image, profile: bytes, alpha: bool) -> Image.Image | None:
    """Interpret tagged colour before discarding its profile; keep alpha separate."""
    # CMYK and grayscale profiles describe those source channels, not a prior
    # generic RGB conversion. Palette/alpha images supply their colour channels.
    if image.mode == "CMYK" or image.mode in ("RGB", "L"):
        colour = image
    elif image.mode == "LA":
        colour = image.getchannel("L")
    elif image.mode in ("1", "I", "I;16", "I;16B", "I;16L"):
        colour = image.convert("L")
    else:
        colour = image.convert("RGB")
    try:
        converted = ImageCms.profileToProfile(
            colour, ImageCms.ImageCmsProfile(BytesIO(profile)), ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB")
    except (ImageCms.PyCMSError, OSError, ValueError):
        # Show usable pixels even when their profile cannot be interpreted;
        # the caller reports the lost colour interpretation in metadata.
        return None
    assert converted is not None  # inPlace=False returns a new image.
    if alpha:
        converted = converted.convert("RGBA")
        converted.putalpha(image.convert("RGBA").getchannel("A"))
    return converted


def _gray8(image: Image.Image) -> Image.Image:
    """Map the full unsigned 16-bit range to 8 bits, with nearest rounding."""
    return image.convert("F").point(lambda value: (value + 128) / 257).convert("L")


def _scale(image: Image.Image, maximum: int) -> Image.Image:
    # Pillow 13 resamples I;16B with native byte order. Integer mode preserves
    # all values for explicit-endian variants, unlike direct I;16 conversion.
    if image.mode in ("I;16B", "I;16L"):
        image = image.convert("I")
    # Native 16-bit resampling works; its reducing pass does not.
    image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS,
                    reducing_gap=None if image.mode.startswith("I;16") else 2.0)
    return image


def _jpeg_factor(size: tuple[int, int], region: tuple[int, int], maximum: int) -> int:
    return next((value for value in (8, 4, 2)
                 if max(region) >= maximum * value and min(size) >= value), 1)


def _source_limit(source: Image.Image, maximum: int, crop: list[int] | None) -> tuple[str, int]:
    transparent = "A" in source.getbands() or "transparency" in source.info
    if source.format == "JPEG":
        if source.info.get("progressive") or source.info.get("progression"):
            layers = source.layer
            if not layers or any(h <= 0 or v <= 0 for _, h, v, _ in layers):
                raise ValueError("invalid JPEG sampling factors")
            samples = sum(h * v for _, h, v, _ in layers) / (max(p[1] for p in layers) * max(p[2] for p in layers))
            region = (crop[2], crop[3]) if crop is not None else source.size
            factor = _jpeg_factor(source.size, region, maximum)
            # libjpeg coefficients are 16-bit; Pillow RGB/CMYK rasters use 4 B/px.
            bytes_per_pixel = 2 * samples + (1 if source.mode == "L" else 4) / factor ** 2
            limit = int(PROGRESSIVE_JPEG_BYTES / bytes_per_pixel / 1_000_000) * 1_000_000
            return f"Progressive {source.mode} JPEG at this crop/detail", min(JPEG_PIXELS, limit)
        return "JPEG", JPEG_PIXELS
    if source.format == "PNG":
        if source.mode == "RGB" and not transparent:
            return "RGB PNG without transparency", RGB_PNG_PIXELS
        return ("PNG with transparency" if transparent else f"PNG in {source.mode} mode"), OTHER_PNG_PIXELS
    return ("WebP with transparency" if transparent else "WebP"), WEBP_PIXELS


def _preview_size(size: tuple[int, int], maximum: int) -> tuple[int, int]:
    """Match thumbnail aspect rounding without allocating a crop raster."""
    width, height = size
    if max(size) <= maximum:
        return size
    aspect = width / height
    if width >= height:
        value = maximum / aspect
        short = max(min(math.floor(value), math.ceil(value),
                        key=lambda n: 0 if n == 0 else abs(aspect - maximum / n)), 1)
        return maximum, short
    value = maximum * aspect
    short = max(min(math.floor(value), math.ceil(value),
                    key=lambda n: abs(aspect - n / maximum)), 1)
    return short, maximum


def _region(source: Image.Image, box: tuple[float, float, float, float],
            size: tuple[int, int]) -> Image.Image:
    if (box[2] - box[0], box[3] - box[1]) == size:
        return source.crop(box)  # no resampling: at most one preview-sized region
    return source.resize(size, Image.Resampling.LANCZOS, box=box, reducing_gap=2.0)


def _jpeg_frame(source: Image.Image, box: tuple[int, int, int, int], maximum: int) -> Image.Image:
    width, height = source.size
    region = box[2] - box[0], box[3] - box[1]
    factor = _jpeg_factor(source.size, region, maximum)
    drafted = source.draft(None, (max(1, width // factor), max(1, height // factor)))
    # draft reports the exact image extent, excluding padded edge pixels.
    scale = drafted[1][2] / width if drafted is not None else 1.0
    reduced = tuple(value * scale for value in box)
    return _region(source, reduced, _preview_size(region, maximum))


def _raw_crop(crop: list[int], size: tuple[int, int], orientation: int) -> tuple[int, int, int, int]:
    """Inverse EXIF geometry, so cropping need not rotate a full-size raster."""
    x, y, w, h = crop
    width, height = size
    return {
        1: (x, y, x + w, y + h),
        2: (width - x - w, y, width - x, y + h),
        3: (width - x - w, height - y - h, width - x, height - y),
        4: (x, height - y - h, x + w, height - y),
        5: (y, x, y + h, x + w),
        6: (y, height - x - w, y + h, height - x),
        7: (width - y - h, height - x - w, width - y, height - x),
        8: (width - y - h, x, width - y, x + w),
    }[orientation]


def _render(data: bytes, sha256: str, maximum: int, crop: list[int] | None) -> dict[str, object]:
    # Keep Pillow's default bomb limit and promote its warning to a refusal.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Preserve useful unsupported-format refusals without loading other decoders.
            for magic, name in ((b"GIF87a", "GIF"), (b"GIF89a", "GIF"), (b"BM", "BMP"),
                                (b"II*\0", "TIFF"), (b"MM\0*", "TIFF"), (b"\0\0\1\0", "ICO")):
                if data.startswith(magic):
                    raise Refused("unsupported", "unsupported image format", format=name)
            with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
                source_format = source.format
                if source_format not in ("JPEG", "PNG", "WEBP"):
                    raise Refused("unsupported", "unsupported image format", format=source_format)
                label, limit = _source_limit(source, maximum, crop)
                if source.width * source.height > limit:
                    raise Refused("limit", f"{label}: at most {limit // 1_000_000} MP",
                                  format=source_format, mode=source.mode, max_pixels=limit)
                source.verify()
            # WebP's decoder can own a full canvas even before load. Do not retain
            # the verified image while constructing the second decoder.
            del source
            with Image.open(BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
                frames = getattr(source, "n_frames", 1)
                profile = source.info.get("icc_profile")
                source.seek(0)
                orientation = source.getexif().get(274, 1)
                if orientation not in (2, 3, 4, 5, 6, 7, 8):
                    orientation = 1
                raw_size = source.size
                width, height = raw_size[::-1] if orientation in (5, 6, 7, 8) else raw_size
                if crop is not None:
                    x, y, w, h = crop
                    if x + w > width or y + h > height:
                        raise Refused("invalid_request", "crop is outside the oriented image",
                                      width=width, height=height)
                    box = _raw_crop(crop, raw_size, orientation)
                else:
                    box = (0, 0, *raw_size)
                if source_format == "JPEG":
                    frame = _jpeg_frame(source, box, maximum)
                elif crop is not None and source.mode == "RGB" and "transparency" not in source.info:
                    frame = _region(source, box, _preview_size((box[2] - box[0], box[3] - box[1]), maximum))
                elif crop is not None:
                    frame = source.crop(box)
                else:
                    frame = source
                if frame is not source:
                    source.close()  # release the decoder before resampling/colour copies
                alpha = "A" in frame.getbands() or "transparency" in frame.info
                if frame.mode in ("P", "PA"):
                    frame = frame.convert("RGBA" if alpha else "RGB")
                elif frame.mode == "1":
                    frame = frame.convert("L")
                frame = _scale(frame, maximum)
                ImageOps.exif_transpose(frame, in_place=True)
                if frame.mode == "I" or frame.mode.startswith("I;16"):
                    frame = _gray8(frame)
                colour = "untagged"
                converted = None
                if profile is not None:
                    converted = _to_srgb(frame, profile, alpha)
                    colour = "profile_unusable" if converted is None else "converted"
                if converted is None:
                    converted = frame.convert("RGBA" if alpha else "RGB")
                # Only the bounded preview gets a pixel-only copy; source metadata
                # and decoder-owned buffers do not travel into the output.
                pixels = Image.new(converted.mode, converted.size)
                pixels.paste(converted)
            del source, frame, converted
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise Refused("limit",
                      f"source image exceeds the pixel limit: JPEG up to {JPEG_PIXELS // 1_000_000} MP, "
                      f"opaque RGB PNG {RGB_PNG_PIXELS // 1_000_000} MP, "
                      f"other PNG {OTHER_PNG_PIXELS // 1_000_000} MP, "
                      f"WebP {WEBP_PIXELS // 1_000_000} MP") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError):
        raise Refused("unreadable", "image is corrupt, truncated or unreadable") from None

    flattened = False
    encoded: bytes | None = None
    output_format = "PNG" if alpha else "JPEG"
    if alpha:
        buffer = BytesIO()
        pixels.save(buffer, format="PNG")
        encoded = buffer.getvalue()
        if len(encoded) > ANSWER_BYTES:
            background = Image.new("RGB", pixels.size, "white")
            background.paste(pixels, mask=pixels.getchannel("A"))
            pixels = background
            output_format, flattened, encoded = "JPEG", True, None
    if encoded is None:
        for quality in JPEG_QUALITIES:
            buffer = BytesIO()
            pixels.save(buffer, format="JPEG", quality=quality)
            candidate = buffer.getvalue()
            if len(candidate) <= ANSWER_BYTES:
                encoded = candidate
                break
    if encoded is None:
        raise Refused("limit", "image cannot fit the answer byte limit", max_bytes=ANSWER_BYTES)
    return {"data": base64.b64encode(encoded).decode("ascii"),
            "mime_type": "image/png" if output_format == "PNG" else "image/jpeg",
            "metadata": {"format": output_format, "width": pixels.width, "height": pixels.height,
                         "alpha_flattened": flattened, "colour": colour, "frames": frames,
                         "source": {"bytes": len(data), "sha256": sha256, "format": source_format,
                                    "width": width, "height": height},
                         "crop": crop}}
