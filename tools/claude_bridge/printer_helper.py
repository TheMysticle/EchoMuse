#!/usr/bin/env python3
"""Talk to an Elegoo Centauri Carbon for the Claude bridge (docs/claude-bridge.md).

Run with the Python of a venv holding pycentauri (Apache-2.0); the bridge
itself stays standard-library only. One command per run, one JSON object on
stdout; exit status 0 only when the printer accepted the command:

  printer_helper.py HOST status
  printer_helper.py HOST upload LOCAL_FILE REMOTE_NAME
  printer_helper.py HOST start REMOTE_NAME
  printer_helper.py HOST pause|resume|stop

The pycentauri CLI prints the printer's answer and exits 0 even when the
printer refuses a start, so this checks the SDCP `Ack` itself.
"""
import asyncio
import json
import sys

from pycentauri.connect import connect_auto
from pycentauri.discovery import discover as discover_printers

TIMEOUT_S = 20.0


def ack_of(message):
    """The SDCP Ack of a command response (0 = accepted), or None if it carries none."""
    inner = getattr(message, "inner", None) or {}
    for layer in (inner.get("Data") if isinstance(inner.get("Data"), dict) else None, inner):
        if isinstance(layer, dict) and "Ack" in layer:
            try:
                return int(layer["Ack"])
            except (TypeError, ValueError):
                return -1
    return None


def status_view(status):
    info = status.print_info
    return {
        "state": status.state,
        "print_status": status.print_status,
        "progress": status.progress,
        "filename": status.filename or "",
        "layer": getattr(info, "current_layer", None),
        "total_layers": getattr(info, "total_layer", None),
        "nozzle_c": round(status.temp_nozzle, 1) if status.temp_nozzle is not None else None,
        "bed_c": round(status.temp_bed, 1) if getattr(status, "temp_bed", None) is not None else None,
    }


async def run(host, command, args):
    control = command != "status"
    # The printer pushes no Attributes while paused or errored, and every
    # command needs its mainboard id, so learn it first (as pycentauri's CLI does).
    mainboard = None
    for found in await discover_printers(timeout=1.0, retries=2):
        if found.host == host and found.mainboard_id:
            mainboard = found.mainboard_id
    printer = await asyncio.wait_for(
        connect_auto(host, enable_control=control, mainboard_id=mainboard), TIMEOUT_S)
    try:
        if command == "status":
            return status_view(await asyncio.wait_for(printer.status(), TIMEOUT_S))
        if command == "upload":
            local, remote = args
            name = await printer.upload_file(local, remote_name=remote, timeout=300.0)
            return {"uploaded": name}
        if command == "start":
            message = await asyncio.wait_for(printer.start_print(args[0]), TIMEOUT_S)
        elif command in ("pause", "resume", "stop"):
            message = await asyncio.wait_for(getattr(printer, command)(), TIMEOUT_S)
        else:
            raise ValueError("unknown command " + command)
        ack = ack_of(message)
        if ack not in (0, None):
            raise RuntimeError(f"the printer refused {command} (Ack {ack})")
        return {command: True, "ack": ack}
    finally:
        await printer.close()


def main(argv):
    if len(argv) < 3:
        print(json.dumps({"error": "usage: printer_helper.py HOST COMMAND [ARGS]"}))
        return 2
    try:
        result = asyncio.run(run(argv[1], argv[2], argv[3:]))
    except Exception as error:   # one line the bridge can show; never a traceback
        print(json.dumps({"error": f"{type(error).__name__}: {error}"[:300]}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
