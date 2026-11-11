#!/usr/bin/env python3
"""Draws the cutest cat to /output/entry.png. Deterministic, no network, stdlib only."""
import os, struct, zlib

W, H = 256, 256

def cat_bitmap():
    # returns list of rows; each row = list of (r,g,b)
    px = [[(255, 236, 179)] * W for _ in range(H)]  # cream background
    ORANGE, DARK, PINK, WHITE, BLACK, GREEN = (232, 148, 58), (120, 62, 18), (240, 130, 150), (255, 255, 255), (40, 30, 25), (60, 130, 80)

    def ellipse(cx, cy, rx, ry, col):
        for y in range(max(0, cy - ry), min(H, cy + ry + 1)):
            for x in range(max(0, cx - rx), min(W, cx + rx + 1)):
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                    px[y][x] = col

    # ears
    for dx, sgn in ((0, -1), (0, 1)):
        cx = 128 + sgn * 52
        for t in range(34):
            w = 30 - t
            for x in range(cx - w // 2, cx + w // 2):
                y = 40 + t
                if 0 <= x < W and 0 <= y < H:
                    px[y][x] = ORANGE
    # inner ears
    for sgn in (-1, 1):
        cx = 128 + sgn * 52
        for t in range(18):
            w = 14 - t
            for x in range(cx - w // 2, cx + w // 2):
                y = 52 + t
                if 0 <= x < W and 0 <= y < H:
                    px[y][x] = PINK
    # head
    ellipse(128, 110, 84, 74, ORANGE)
    # muzzle
    ellipse(128, 148, 46, 30, WHITE)
    # eyes (big sparkly)
    for sgn in (-1, 1):
        cx = 128 + sgn * 38
        ellipse(cx, 102, 17, 19, BLACK)
        ellipse(cx, 100, 13, 15, GREEN)
        ellipse(cx - 4, 95, 5, 6, WHITE)
        ellipse(cx + 4, 106, 2, 3, WHITE)
    # nose + mouth
    ellipse(128, 138, 8, 6, PINK)
    ellipse(128, 146, 3, 2, DARK)
    # blush
    for sgn in (-1, 1):
        ellipse(128 + sgn * 60, 130, 12, 7, PINK)
    # whiskers
    for sgn in (-1, 1):
        for dy in (-6, 2, 10):
            for t in range(36):
                x = 128 + sgn * (52 + t)
                y = 140 + dy + int(t * 0.12 * (1 if dy > 0 else -1))
                if 0 <= x < W and 0 <= y < H:
                    px[y][x] = DARK
    # body / paws at bottom
    ellipse(128, 226, 62, 28, ORANGE)
    ellipse(100, 232, 16, 10, WHITE)
    ellipse(156, 232, 16, 10, WHITE)
    for sgn in (-1, 1):
        ellipse(128 + sgn * 30, 226, 3, 2, PINK)
    # tail
    for t in range(40):
        x, y = 186 + int(t * 0.9), 218 - int(t * 0.55)
        ellipse(x, y, 7 - t // 12, 7 - t // 12, ORANGE)
    return px

def png(rows):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(v for p in row for v in p) for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))

def main():
    os.makedirs("/output", exist_ok=True)
    with open("/output/entry.png", "wb") as f:
        f.write(png(cat_bitmap()))

if __name__ == "__main__":
    main()
