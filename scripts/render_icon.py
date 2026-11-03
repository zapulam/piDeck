"""Render the piDeck raspberry pixel sprite into window-icon files."""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pideck" / "assets"
ROWS = [
    "............",
    "....G..G....",
    "...GGGGGG...",
    "..RrRRrRRR..",
    ".RRRrRRrRRR.",
    ".RrRRrRRrRR.",
    ".RRRrRRrRRR.",
    ".RrRRrRRrRR.",
    ".RRRrRRrRRR.",
    "..RRrRRrRR..",
    "............",
    "............",
]
COLORS = {
    "R": (0xD7, 0x26, 0x4C, 255),
    "r": (0xFF, 0x7B, 0x9C, 255),
    "d": (0x8F, 0x12, 0x32, 255),
    "G": (0x4C, 0xC2, 0x6A, 255),
    ".": (0, 0, 0, 0),
}


"""# --- Pixel image encoding ---"""


def sprite_pixels(
        size: int,
) -> bytes:
    """Scale the 12x12 raspberry onto a square RGBA buffer.

    Args:
        size: Output width and height in pixels.

    Returns:
        Packed RGBA bytes, row-major.
    """
    grid = 12
    out = bytearray()
    for y in range(size):
        row = ROWS[min(grid - 1, y * grid // size)]
        for x in range(size):
            out.extend(COLORS[row[min(grid - 1, x * grid // size)]])
    return bytes(out)


def png_bytes(
        size: int,
) -> bytes:
    """Encode one nearest-neighbor raspberry PNG.

    Args:
        size: Output width and height in pixels.

    Returns:
        A complete PNG file.
    """
    def chunk(
            tag: bytes,
            data: bytes,
    ) -> bytes:
        """Wrap one PNG chunk with its length and CRC."""
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + sprite_pixels(size)[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(
        sizes: tuple[int, ...],
) -> bytes:
    """Pack PNG images into a Windows icon.

    Args:
        sizes: Square icon sizes to embed.

    Returns:
        A complete .ico file.
    """
    images = [(size, png_bytes(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    payload = b""
    offset = 6 + 16 * len(images)
    for size, image in images:
        edge = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(image), offset)
        payload += image
        offset += len(image)
    return header + entries + payload


"""# --- Command entry point ---"""


def main() -> None:
    """Write pideck.png and pideck.ico beside the desktop package."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pideck.png").write_bytes(png_bytes(256))
    (OUT / "pideck.ico").write_bytes(ico_bytes((16, 32, 48, 256)))
    print(OUT)


if __name__ == "__main__":
    main()
