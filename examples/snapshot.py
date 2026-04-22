"""Save a webcam snapshot to a JPEG file."""

from __future__ import annotations

import asyncio
import sys

from pycentauri import Printer


async def main(host: str, out: str) -> None:
    async with await Printer.connect(host) as printer:
        jpeg = await printer.snapshot()
    with open(out, "wb") as f:
        f.write(jpeg)
    print(f"wrote {len(jpeg)} bytes to {out}")


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.209"
    out = sys.argv[2] if len(sys.argv) > 2 else "shot.jpg"
    asyncio.run(main(host, out))
