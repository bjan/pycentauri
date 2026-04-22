"""Start a print of a file that is already on the printer.

The library refuses write actions unless ``enable_control=True`` is passed
explicitly. Run the printer's own screen or web UI to confirm the file is
staged before you call this.
"""

from __future__ import annotations

import asyncio
import sys

from pycentauri import Printer


async def main(host: str, filename: str) -> None:
    async with await Printer.connect(host, enable_control=True) as printer:
        result = await printer.start_print(filename)
        print("start_print response:", result.inner)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python start_print.py <host> <filename>", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
