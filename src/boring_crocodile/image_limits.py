"""Image-tool bounds; independent of the ordinary file-tool byte policy."""
# Bound input memory and the amount of data presented to Pillow's parsers.
SOURCE_BYTES = 25 * 1024 * 1024
# Format/mode bounds measured against the proposed 512 MiB shared budget.
JPEG_PIXELS = 50_000_000
# Progressive JPEG retains coefficient arrays as well as its output raster.
PROGRESSIVE_JPEG_BYTES = 210 * 1024 * 1024
RGB_PNG_PIXELS = 40_000_000
OTHER_PNG_PIXELS = 16_000_000
WEBP_PIXELS = 12_000_000
# Base64 expands this to ~1.4 MiB, leaving room in the 2 MiB worker frame.
ANSWER_BYTES = 1024 * 1024
MIN_PX = 64
MAX_PX = 2048
DEFAULT_PX = 1568
# One decode at a time per worker, including cancelled callers.
IMAGE_SLOTS = 1
ADMISSION_SECONDS = 5
JPEG_QUALITIES = (85, 75, 65, 50)
