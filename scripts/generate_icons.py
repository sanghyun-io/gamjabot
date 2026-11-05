#!/usr/bin/env python3
"""Generate the original GamjaBot conversation mark using only Python's stdlib.

Copyright 2026 sanghyun-io. SPDX-License-Identifier: Apache-2.0
No imported artwork, fonts or personal images are used.
"""
from pathlib import Path
import struct
import zlib
ROOT = Path(__file__).resolve().parents[1]

def png(size):
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            u, v = x * 64 / size, y * 64 / size
            color = (89, 70, 44, 255)
            if (18 <= u < 46 and 24 <= v < 42) or (21 <= u < 29 and 42 <= v < 49 and v < 70 - u):
                color = (255, 244, 213, 255)
            if (u - 27)**2 + (v - 33)**2 < 9 or (u - 38)**2 + (v - 33)**2 < 9:
                color = (89, 70, 44, 255)
            row.extend(color)
        rows.append(bytes(row))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b'')

def main():
    target = ROOT / 'frontend/src-tauri/icons'
    target.mkdir(parents=True, exist_ok=True)
    images = {s: png(s) for s in [16, 32, 64, 128, 256, 512, 1024]}
    for name, size in [('32x32.png', 32), ('128x128.png', 128), ('128x128@2x.png', 256)]:
        (target / name).write_bytes(images[size])
    data = images[256]
    (target / 'icon.ico').write_bytes(struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(data), 22) + data)
    chunks = b''.join(tag + struct.pack('>I', len(images[size]) + 8) + images[size] for tag, size in [(b'icp4', 16), (b'icp5', 32), (b'icp6', 64), (b'ic07', 128), (b'ic08', 256), (b'ic09', 512), (b'ic10', 1024)])
    (target / 'icon.icns').write_bytes(b'icns' + struct.pack('>I', len(chunks) + 8) + chunks)
    print('Generated original native icon assets.')

if __name__ == '__main__':
    main()
