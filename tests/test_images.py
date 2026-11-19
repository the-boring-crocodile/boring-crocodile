"""Image fixtures are generated here; assertions inspect the worker and MCP boundaries."""
import asyncio
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageCms, PngImagePlugin
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent
from boring_worker_kit.file_policy import FilePolicy
from boring_crocodile.control_plane import refuse_unknown_arguments
from boring_crocodile.file_tools import register_file_tools
from boring_crocodile.image_limits import ANSWER_BYTES, SOURCE_BYTES
from boring_crocodile.outcomes import Refused, RefusalError
from boring_crocodile.settings import EnvironmentSettings
from boring_crocodile.worker import WorkerServer
from boring_crocodile.worker_client import WorkerClient


def generated_colour_profile():
    """A generated RGB profile with a non-sRGB transfer curve; no external asset."""
    data = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    for index in range(struct.unpack_from(">I", data, 128)[0]):
        tag, offset, size = struct.unpack_from(">4sII", data, 132 + index * 12)
        if tag in (b"rTRC", b"gTRC", b"bTRC"):
            assert data[offset:offset + 4] == b"para" and size >= 16
            struct.pack_into(">i", data, offset + 12, round(1.8 * 65536))
    return bytes(data)


def generated_gray_profile():
    """Gray ICC from freshly generated sRGB tags; no external profile fixture."""
    source = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    tags = {}
    for index in range(struct.unpack_from(">I", source, 128)[0]):
        tag, offset, size = struct.unpack_from(">4sII", source, 132 + index * 12)
        tags[tag] = source[offset:offset + size]
    entries = [(b"wtpt", tags[b"wtpt"]), (b"kTRC", tags[b"rTRC"]), (b"desc", tags[b"desc"])]
    header = bytearray(source[:128])
    header[16:20] = b"GRAY"
    header[84:100] = b"\0" * 16
    table = bytearray(struct.pack(">I", len(entries)))
    body = bytearray()
    start = 128 + 4 + 12 * len(entries)
    for tag, payload in entries:
        table += struct.pack(">4sII", tag, start + len(body), len(payload))
        body += payload
        body += b"\0" * (-len(body) % 4)
    result = header + table + body
    struct.pack_into(">I", result, 0, len(result))
    return bytes(result)


class ImageBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.allowed = self.root / "allowed"
        self.allowed.mkdir()
        (self.allowed / "private").mkdir()
        policy = FilePolicy.parse({"roots": {"work": {"path": str(self.allowed),
                                   "read": True, "write": True, "deny": ["private"]}}})
        self.settings = EnvironmentSettings("test", "Test", self.root / "worker.sock",
            self.root, (self.root,), os.getuid(), 128, command_execution_enabled=False, files=policy)
        self.worker = WorkerServer(self.settings)
        self.socket = await asyncio.start_unix_server(self.worker.handle, self.settings.socket_path)
        self.client = WorkerClient({"test": self.settings})
        self.mcp = MCPServer("image-test")
        register_file_tools(self.mcp, self.client, lambda: "test")
        refuse_unknown_arguments(self.mcp)

    async def asyncTearDown(self):
        self.socket.close()
        await self.socket.wait_closed()
        self.worker.files.pool.shutdown(wait=True)

    def save(self, image, name="image.png", **kwargs):
        image.save(self.allowed / name, **kwargs)
        return name

    async def call(self, path="image.png", **kwargs):
        return await self.mcp.call_tool("file_read_image", {"root": "work", "path": path, **kwargs})

    def unpack(self, result):
        self.assertIsInstance(result, CallToolResult)
        self.assertFalse(result.is_error)
        self.assertIsNone(result.structured_content)
        self.assertEqual([b.type for b in result.content], ["image", "text"])
        self.assertIsInstance(result.content[0], ImageContent)
        self.assertIsInstance(result.content[1], TextContent)
        self.assertNotIn("\n", result.content[1].text)
        wire = json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
        self.assertNotIn("structuredContent", wire)
        self.assertIn("mimeType", wire["content"][0])
        raw = base64.b64decode(result.content[0].data, validate=True)
        self.assertLessEqual(len(raw), ANSWER_BYTES)
        image = Image.open(BytesIO(raw))
        image.load()
        return image, json.loads(result.content[1].text), raw

    async def refusal(self, code, path="image.png", **kwargs):
        result = await self.call(path, **kwargs)
        self.assertTrue(result.is_error)
        data = json.loads(result.content[0].text)
        self.assertEqual(data, result.structured_content)
        self.assertEqual(data["status"], "refused")
        self.assertEqual(data["code"], code)
        return data

    async def test_schema_and_alpha_metadata_stripped(self):
        schema = {t.name: t for t in await self.mcp.list_tools()}["file_read_image"]
        self.assertIsNone(schema.output_schema)
        self.assertEqual(schema.input_schema["properties"]["max_px"]["minimum"], 64)
        self.assertEqual(schema.input_schema["properties"]["max_px"]["maximum"], 2048)
        info = PngImagePlugin.PngInfo()
        info.add_text("private", "DO-NOT-RETURN")
        name = self.save(Image.new("RGBA", (80, 40), (10, 20, 30, 71)), pnginfo=info)
        original = (self.allowed / name).read_bytes()
        image, meta, raw = self.unpack(await self.call())
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0)), (10, 20, 30, 71))
        self.assertEqual(image.info, {})
        self.assertNotIn(b"DO-NOT-RETURN", raw)
        self.assertEqual(meta, {"format": "PNG", "width": 80, "height": 40,
            "alpha_flattened": False, "colour": "untagged", "frames": 1, "crop": None,
            "source": {"bytes": len(original), "sha256": hashlib.sha256(original).hexdigest(),
                       "format": "PNG", "width": 80, "height": 40}})
        self.assertEqual((self.allowed / name).read_bytes(), original)

    async def test_generated_icc_converts_colour_keeps_alpha_and_drops_profile(self):
        profile = generated_colour_profile()
        self.save(Image.new("RGBA", (12, 8), (80, 100, 120, 73)), icc_profile=profile)
        image, meta, raw = self.unpack(await self.call(crop=[0, 0, 10, 6]))
        self.assertEqual(meta["colour"], "converted")
        self.assertEqual(image.getpixel((0, 0)), (108, 127, 145, 73))
        self.assertEqual(image.size, (10, 6))
        self.assertNotIn("icc_profile", image.info)
        self.assertNotIn(profile, raw)
        self.assertEqual(meta["source"]["sha256"],
                         hashlib.sha256((self.allowed / "image.png").read_bytes()).hexdigest())

    async def test_palette_icc_converts_colour_and_keeps_transparency(self):
        source = Image.new("P", (8, 8))
        source.putpalette([80, 100, 120] + [0, 0, 0] * 255)
        self.save(source, transparency=0, icc_profile=generated_colour_profile())
        image, meta, _ = self.unpack(await self.call())
        self.assertEqual(meta["colour"], "converted")
        self.assertEqual(image.getpixel((0, 0)), (108, 127, 145, 0))

    async def test_broken_icc_renders_pixels_and_reports_unusable_profile(self):
        self.save(Image.new("RGBA", (8, 8), (80, 100, 120, 73)), icc_profile=b"broken profile")
        image, meta, _ = self.unpack(await self.call())
        self.assertEqual(meta["colour"], "profile_unusable")
        self.assertEqual(image.getpixel((0, 0)), (80, 100, 120, 73))
        self.assertNotIn("icc_profile", image.info)

    async def test_incompatible_icc_renders_without_conversion(self):
        self.save(Image.new("CMYK", (8, 8)), "incompatible.jpg",
                  icc_profile=generated_colour_profile())
        image, meta, _ = self.unpack(await self.call("incompatible.jpg"))
        self.assertEqual(meta["colour"], "profile_unusable")
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (8, 8))

    async def test_cmyk_profile_sees_original_channels(self):
        self.save(Image.new("CMYK", (256, 128), (10, 20, 30, 40)),
                  "cmyk-tagged.jpg", icc_profile=generated_colour_profile())
        # Transport fake checks the seam: a real CMYK profile must see CMYK input.
        seen = []
        def cms(image, input_profile, output_profile, **kwargs):
            seen.append((image.mode, image.size, kwargs["renderingIntent"], kwargs["outputMode"]))
            return Image.new("RGB", image.size, (70, 90, 110))
        with patch("boring_crocodile.image_tools.ImageCms.profileToProfile", cms):
            image, meta, _ = self.unpack(await self.call("cmyk-tagged.jpg", max_px=64))
        self.assertEqual(seen, [("CMYK", (64, 32), ImageCms.Intent.PERCEPTUAL, "RGB")])
        self.assertEqual(meta["colour"], "converted")
        self.assertEqual(image.mode, "RGB")

    async def test_icc_sees_resampled_source_channels(self):
        for mode, expected in [("RGB", "RGB"), ("RGBA", "RGB"), ("L", "L"),
                               ("LA", "L"), ("P", "RGB"), ("1", "L"), ("I;16", "L")]:
            with self.subTest(mode=mode):
                source = Image.new(mode, (256, 128))
                self.save(source, icc_profile=generated_colour_profile())
                seen = []
                def cms(image, input_profile, output_profile, **kwargs):
                    seen.append((image.mode, image.size))
                    return image.convert("RGB")
                with patch("boring_crocodile.image_tools.ImageCms.profileToProfile", cms):
                    image, meta, _ = self.unpack(await self.call(max_px=64))
                self.assertEqual(seen, [(expected, (64, 32))])
                self.assertEqual(meta["colour"], "converted")
                self.assertEqual(image.size, (64, 32))

    def test_gray16_endian_variants_keep_the_midpoint(self):
        from boring_crocodile.image_tools import _gray8
        for mode in ("I;16", "I;16L", "I;16B"):
            with self.subTest(mode=mode):
                order = ">" if mode.endswith("B") else "<"
                image = Image.frombytes(mode, (5, 1),
                    struct.pack(order + "5H", 0, 16384, 32768, 49152, 65535))
                values = list(_gray8(image).get_flattened_data())
                self.assertEqual(values[:3], [0, 64, 128])
                self.assertAlmostEqual(values[3], 192, delta=1)
                self.assertEqual(values[4], 255)

    def test_gray16_endian_variants_resample_without_swapping_bytes(self):
        from boring_crocodile.image_tools import _gray8, _scale
        for mode in ("I;16", "I;16L", "I;16B"):
            with self.subTest(mode=mode):
                order = ">" if mode.endswith("B") else "<"
                row = struct.pack(order + "256H", *(value * 257 for value in range(256)))
                image = Image.frombytes(mode, (256, 32), row * 32)
                preview = _gray8(_scale(image, 64))
                self.assertEqual(preview.size, (64, 8))
                self.assertLessEqual(preview.getpixel((0, 4)), 3)
                self.assertLessEqual(abs(preview.getpixel((32, 4)) - 128), 3)
                self.assertGreaterEqual(preview.getpixel((63, 4)), 252)

    async def test_16_bit_gray_ramp_is_scaled_not_clipped_tagged_and_untagged(self):
        row = struct.pack("<256H", *(value * 257 for value in range(256)))
        for tagged in (False, True):
            with self.subTest(tagged=tagged):
                self.save(Image.frombytes("I;16", (256, 32), row * 32),
                          **({"icc_profile": generated_gray_profile()} if tagged else {}))
                image, metadata, _ = self.unpack(await self.call())
                self.assertEqual(metadata["colour"], "converted" if tagged else "untagged")
                samples = [image.getpixel((x, 16))[0] for x in (0, 64, 128, 192, 255)]
                for observed, expected in zip(samples, (0, 64, 128, 192, 255)):
                    self.assertLessEqual(abs(observed - expected), 2)
                self.assertGreater(len(set(image.get_flattened_data())), 200)

    async def test_crop_matches_oriented_pixels_for_every_exif_transform(self):
        source = Image.frombytes("RGB", (40, 24), random.Random(19).randbytes(40 * 24 * 3))
        from PIL import ImageOps
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                exif = Image.Exif()
                exif[274] = orientation
                self.save(source, exif=exif)
                crop = [3, 5, 12, 10]
                with Image.open(self.allowed / "image.png") as opened:
                    expected = ImageOps.exif_transpose(opened).crop((3, 5, 15, 15)).convert("RGB")
                reference = BytesIO()
                expected.save(reference, format="JPEG", quality=85)
                image, metadata, raw = self.unpack(await self.call(crop=crop))
                self.assertEqual(raw, reference.getvalue())
                self.assertEqual(image.size, (12, 10))
                self.assertEqual((metadata["source"]["width"], metadata["source"]["height"]),
                                 (24, 40) if orientation in (5, 6, 7, 8) else (40, 24))

    async def test_only_allowed_decoder_formats_are_opened(self):
        self.save(Image.new("RGB", (20, 10), "red"))
        original = Image.open
        formats = []
        def open_image(*args, **kwargs):
            formats.append(kwargs.get("formats"))
            return original(*args, **kwargs)
        with patch("boring_crocodile.image_tools.Image.open", open_image):
            result = await self.call()
        self.unpack(result)
        self.assertEqual(formats, [["JPEG", "PNG", "WEBP"], ["JPEG", "PNG", "WEBP"]])

    async def test_exif_orientation_then_crop_then_scale(self):
        source = Image.new("RGB", (120, 80), "red")
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "PRIVATE DESCRIPTION"
        self.save(source, "rotated.jpg", exif=exif)
        image, meta, raw = self.unpack(await self.call("rotated.jpg", crop=[0, 40, 80, 80], max_px=64))
        self.assertEqual(image.size, (64, 64))
        self.assertEqual((meta["source"]["width"], meta["source"]["height"]), (80, 120))
        self.assertEqual(meta["crop"], [0, 40, 80, 80])
        self.assertEqual(dict(image.getexif()), {})
        self.assertNotIn(b"PRIVATE DESCRIPTION", raw)
        self.assertNotIn("exif", image.info)

    async def test_crop_edges_and_never_upscale(self):
        self.save(Image.new("RGB", (100, 80), "red"))
        for crop in ([0, 0, 1, 1], [99, 0, 1, 1], [0, 79, 1, 1], [99, 79, 1, 1], [0, 0, 100, 80]):
            with self.subTest(crop=crop):
                image, meta, _ = self.unpack(await self.call(crop=crop))
                self.assertEqual(image.size, tuple(crop[2:]))
                self.assertEqual(meta["crop"], crop)
        for crop in ([100, 0, 1, 1], [0, 80, 1, 1], [99, 79, 2, 1], [0, 0, 101, 80]):
            with self.subTest(crop=crop):
                await self.refusal("invalid_request", crop=crop)
        for crop in ([-1, 0, 1, 1], [0, 0, 0, 1], [0, 0, 1, 0]):
            await self.refusal("invalid_request", crop=crop)

    async def test_animation_png_webp_first_frame_and_palette_cmyk(self):
        for fmt, suffix in (("PNG", "png"), ("WEBP", "webp")):
            with self.subTest(format=fmt):
                name = "animated." + suffix
                self.save(Image.new("RGBA", (32, 24), "red"), name, format=fmt, save_all=True,
                          append_images=[Image.new("RGBA", (32, 24), "blue")], duration=100, loop=0)
                image, meta, _ = self.unpack(await self.call(name))
                self.assertEqual(meta["frames"], 2)
                r, g, b = image.convert("RGB").getpixel((0, 0))
                self.assertGreater(r, 240)
                self.assertLess(b, 15)
        palette = Image.new("P", (16, 16))
        palette.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        self.save(palette, transparency=0)
        image, _, _ = self.unpack(await self.call())
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.save(Image.new("CMYK", (16, 16)), "cmyk.jpg")
        image, meta, _ = self.unpack(await self.call("cmyk.jpg"))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(meta["format"], "JPEG")

    async def test_format_corruption_and_header_limits_before_load(self):
        self.save(Image.new("RGB", (10, 10)), "bad.gif")
        data = await self.refusal("unsupported", "bad.gif")
        self.assertEqual(data["format"], "GIF")
        (self.allowed / "garbage").write_bytes(b"not an image")
        await self.refusal("unreadable", "garbage")
        self.save(Image.new("RGB", (100, 100)), "truncated.jpg")
        p = self.allowed / "truncated.jpg"
        p.write_bytes(p.read_bytes()[:-100])
        await self.refusal("unreadable", p.name)
        self.save(Image.new("RGB", (10, 10)))
        p = self.allowed / "image.png"
        original = p.read_bytes()
        p.write_bytes(original[:-16])
        await self.refusal("unreadable")
        for size in ((60000, 60000), (8000, 5001), (10000, 5001), (10000, 9000)):
            payload = struct.pack(">IIBBBBB", *size, 8, 2, 0, 0, 0)
            chunk = b"IHDR" + payload
            p.write_bytes(original[:8] + struct.pack(">I", 13) + chunk
                          + struct.pack(">I", zlib.crc32(chunk)) + original[33:])
            with self.subTest(size=size), patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
                data = await self.refusal("limit")
                if size == (8000, 5001):
                    self.assertEqual(data["max_pixels"], 40_000_000)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 89478485)

    async def test_phone_and_50mp_jpeg_use_the_actual_format_not_the_suffix(self):
        for size in ((4032, 3024), (4160, 3120), (10000, 5000)):
            with self.subTest(size=size):
                self.save(Image.new("RGB", size, (90, 140, 200)), "phone.png", format="JPEG")
                image, metadata, _ = self.unpack(await self.call("phone.png", max_px=64))
                self.assertEqual(metadata["source"]["format"], "JPEG")
                self.assertEqual((metadata["source"]["width"], metadata["source"]["height"]), size)
                self.assertEqual(max(image.size), 64)

    async def test_50mp_jpeg_crops_do_not_copy_a_full_size_region(self):
        self.save(Image.new("RGB", (10000, 5000), (90, 140, 200)), "large.jpg")
        original = Image.Image.crop
        def crop(image, box=None):
            if box is not None:
                self.assertLessEqual((box[2] - box[0]) * (box[3] - box[1]), 4_000_000)
            return original(image, box)
        for region in ([0, 0, 10000, 5000], [3000, 2000, 1000, 1000], [1000, 500, 4000, 4000]):
            with self.subTest(crop=region), patch.object(Image.Image, "crop", crop):
                image, metadata, _ = self.unpack(await self.call("large.jpg", max_px=2048, crop=region))
                self.assertEqual(metadata["crop"], region)
                self.assertEqual(max(image.size), min(2048, max(region[2:])))

    async def test_jpeg_draft_uses_the_largest_crop_safe_reduction(self):
        from PIL import JpegImagePlugin
        self.save(Image.new("RGB", (1601, 1103)), "draft.jpg")
        original = JpegImagePlugin.JpegImageFile.draft
        seen = []
        def draft(image, mode, size):
            width = image.width
            result = original(image, mode, size)
            seen.append(width / result[1][2])
            return result
        for width, factor in ((513, 8), (511, 4), (255, 2), (127, 1)):
            with self.subTest(width=width), patch.object(JpegImagePlugin.JpegImageFile, "draft", draft):
                seen.clear()
                self.unpack(await self.call("draft.jpg", max_px=64, crop=[23, 31, width, 63]))
                self.assertEqual(seen, [factor])

    async def test_png_mode_and_transparency_limits_are_checked_before_load(self):
        for mode, transparent in (("RGB", False), ("RGB", True), ("RGBA", False),
                                  ("P", True), ("L", False), ("I;16", False)):
            with self.subTest(mode=mode, transparent=transparent):
                options = {"transparency": (1, 2, 3) if mode == "RGB" else 0} if transparent else {}
                self.save(Image.new(mode, (10, 10)), **options)
                path = self.allowed / "image.png"
                original = path.read_bytes()
                limit = 40_000_000 if mode == "RGB" and not transparent else 16_000_000
                size = (8000, 5001) if limit == 40_000_000 else (4000, 4001)
                chunk = b"IHDR" + struct.pack(">II", *size) + original[24:29]
                path.write_bytes(original[:8] + struct.pack(">I", 13) + chunk
                                 + struct.pack(">I", zlib.crc32(chunk)) + original[33:])
                with patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
                    result = await self.refusal("limit")
                self.assertEqual(result["max_pixels"], limit)
                self.assertEqual(result["format"], "PNG")
                self.assertIn("PNG", result["message"])
                self.assertIn(f"at most {limit // 1_000_000} MP", result["message"])

    async def test_large_transparent_webp_names_its_own_limit_before_load(self):
        self.save(Image.new("RGBA", (4000, 3001), (90, 140, 200, 128)),
                  "actually-webp.jpg", format="WEBP")
        with patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
            result = await self.refusal("limit", "actually-webp.jpg")
        self.assertEqual(result["max_pixels"], 12_000_000)
        self.assertEqual(result["format"], "WEBP")
        self.assertIn("WebP with transparency: at most 12 MP", result["message"])

    async def test_progressive_jpeg_budget_refuses_unsafe_detail_before_load(self):
        for mode, subsampling, crop, limit in (("RGB", 2, [1000, 1000, 1000, 1000], 31_000_000),
                ("RGB", 0, None, 35_000_000), ("CMYK", 0, None, 26_000_000),
                ("CMYK", 0, [1000, 1000, 1000, 1000], 18_000_000)):
            with self.subTest(mode=mode, subsampling=subsampling, crop=crop):
                self.save(Image.new(mode, (16, 16)), "progressive.jpg",
                          progressive=True, subsampling=subsampling)
                path = self.allowed / "progressive.jpg"
                data = path.read_bytes()
                offset = data.index(b"\xff\xc2")
                path.write_bytes(data[:offset + 5] + struct.pack(">HH", 5000, 10000) + data[offset + 9:])
                with patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
                    result = await self.refusal("limit", "progressive.jpg", max_px=2048, crop=crop)
                self.assertEqual(result["max_pixels"], limit)
                self.assertIn(f"Progressive {mode} JPEG", result["message"])
                self.assertIn(f"at most {limit // 1_000_000} MP", result["message"])

    async def test_50mp_progressive_rgb_whole_image_remains_admitted(self):
        self.save(Image.new("RGB", (10000, 5000)), "progressive.jpg", progressive=True, subsampling=2)
        image, metadata, _ = self.unpack(await self.call("progressive.jpg", max_px=2048))
        self.assertEqual(max(image.size), 2048)
        self.assertEqual(metadata["source"]["width"] * metadata["source"]["height"], 50_000_000)

    async def test_jpeg_reduced_crop_position_for_all_exif_orientations(self):
        from PIL import ImageChops, ImageOps, ImageStat
        width, height = 1601, 1103
        data = bytes(channel for y in range(height) for x in range(width)
                     for channel in (x * 255 // (width - 1), y * 255 // (height - 1),
                                     (x + y) * 255 // (width + height - 2)))
        original = Image.frombytes("RGB", (width, height), data)
        for orientation in range(1, 9):
            for maximum in (64, 1024):
                with self.subTest(orientation=orientation, maximum=maximum):
                    exif = Image.Exif()
                    exif[274] = orientation
                    self.save(original, "position.jpg", exif=exif, quality=95)
                    with Image.open(self.allowed / "position.jpg") as source:
                        reference = ImageOps.exif_transpose(source).crop((39, 47, 1039, 747))
                        reference.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
                    encoded = BytesIO()
                    reference.save(encoded, format="JPEG", quality=85)
                    reference = Image.open(BytesIO(encoded.getvalue()))
                    image, metadata, raw = self.unpack(await self.call("position.jpg",
                        max_px=maximum, crop=[39, 47, 1000, 700]))
                    self.assertEqual(image.size, reference.size)
                    self.assertEqual(metadata["crop"], [39, 47, 1000, 700])
                    if maximum == 1024:
                        self.assertEqual(raw, encoded.getvalue())  # full detail: no scaling difference
                    difference = ImageChops.difference(image, reference)
                    self.assertLess(max(ImageStat.Stat(difference).mean), 2.5)
                    self.assertLess(max(value[1] for value in difference.getextrema()), 15)

    async def test_file_cap_and_policy(self):
        self.save(Image.new("RGB", (100, 100)))
        p = self.allowed / "image.png"
        with p.open("ab") as stream:
            stream.truncate(SOURCE_BYTES + 1)
        await self.refusal("limit")
        p.unlink()
        await self.refusal("not_found")
        self.save(Image.new("RGB", (10, 10)), "private/image.png")
        for path in ("private/image.png", "../outside.png", ".git/config"):
            await self.refusal("forbidden", path)
        (self.allowed / "symlink").symlink_to(self.allowed / "private/image.png")
        await self.refusal("forbidden", "symlink")
        (self.allowed / "directory").mkdir()
        await self.refusal("forbidden", "directory")
        # Separate cap: a valid image above ordinary max_file_bytes can still be viewed.
        self.save(Image.frombytes("RGB", (600, 600), random.Random(1).randbytes(600 * 600 * 3)))
        self.assertGreater((self.allowed / "image.png").stat().st_size, 512 * 1024)
        self.unpack(await self.call())

    async def test_jpeg_quality_steps_flatten_and_limit(self):
        # Real codec, real size thresholds; inspect each encoding attempt.
        noise = Image.frombytes("RGB", (2048, 2048), random.Random(2).randbytes(2048 * 2048 * 3))
        self.save(noise, "photo.png")
        original_save = Image.Image.save
        qualities = []
        def save(image, fp, format=None, **kwargs):
            if format == "JPEG":
                qualities.append(kwargs["quality"])
            return original_save(image, fp, format=format, **kwargs)
        with patch.object(Image.Image, "save", save):
            await self.refusal("limit", "photo.png", max_px=2048)
        self.assertEqual(qualities, [85, 75, 65, 50])
        # At 1728, the same high-entropy image fits only at the last quality step.
        qualities.clear()
        with patch.object(Image.Image, "save", save):
            image, meta, _ = self.unpack(await self.call("photo.png", max_px=1728))
        self.assertEqual(qualities, [85, 75, 65, 50])
        self.assertEqual(meta["format"], "JPEG")
        rgba = noise.resize((1300, 1300)).convert("RGBA")
        rgba.putalpha(128)
        self.save(rgba)
        image, meta, _ = self.unpack(await self.call())
        self.assertTrue(meta["alpha_flattened"])
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (1300, 1300))

    async def test_worker_revalidates_inputs_and_no_batch_image(self):
        self.save(Image.new("RGB", (10, 10)))
        for kwargs in ({"max_px": True}, {"max_px": 63}, {"max_px": 2049},
                       {"max_px": "64"}, {"crop": [0, 0, True, 1]},
                       {"crop": [0, 0, 1]}, {"unexpected": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RefusalError) as caught:
                await self.client.file_call("test", "file_read_image", root="work", path="image.png", **kwargs)
            self.assertEqual(caught.exception.data["code"], "invalid_request")
        self.assertNotIn("file_read_image", self.worker._capabilities()["batch"]["methods"])
        with self.assertRaises(Refused) as caught:
            await self.worker.batch.run([{"id": "image", "method": "file_read_image",
                                          "arguments": {"root": "work", "path": "image.png"}}])
        self.assertEqual(caught.exception.code, "invalid_request")

    async def test_unexpected_decode_error_is_a_failure(self):
        self.save(Image.new("RGB", (10, 10)))
        with patch("boring_crocodile.image_tools._render", side_effect=RuntimeError("decoder bug")):
            result = await self.call()
        self.assertTrue(result.is_error)
        data = json.loads(result.content[0].text)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["code"], "worker_failed")

    async def test_cancelled_decode_retains_admission_slot(self):
        self.save(Image.new("RGB", (10, 10)))
        entered, release = threading.Event(), threading.Event()
        from boring_crocodile.image_tools import _render
        def slow(*args):
            entered.set()
            release.wait(5)
            return _render(*args)
        with patch("boring_crocodile.image_tools._render", slow):
            task = asyncio.create_task(self.worker.images.call({"root": "work", "path": "image.png"}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(self.worker.images.slots.locked())
            finally:
                release.set()
            # Drain the worker-owned task by waiting for its slot.
            await asyncio.wait_for(self.worker.images.slots.acquire(), 3)
            self.worker.images.slots.release()
