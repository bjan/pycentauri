"""Stream live status pushes from the printer until Ctrl-C."""

from __future__ import annotations

import asyncio
import contextlib
import sys

from pycentauri import Printer


async def main(host: str) -> None:
    async with await Printer.connect(host) as printer:
        async for st in printer.watch():
            print(
                f"state={st.print_status or '-':>3}  "
                f"progress={st.progress or 0:>3}%  "
                f"nozzle={st.temp_nozzle or 0:5.1f}°C  "
                f"bed={st.temp_bed or 0:5.1f}°C  "
                f"file={st.filename or '-'}"
            )


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.209"
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(host))
