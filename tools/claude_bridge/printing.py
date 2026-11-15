"""Printing for the Claude bridge (docs/claude-bridge.md, "Printing").

Claude may prepare a print: `slice` turns its model into G-code with the
slicer's own Centauri Carbon profiles, and `upload` puts the file on the
printer. Starting is never Claude's: the bridge starts a print only on the
user's spoken request (the controller's ClaudePrint tool), and only when the
printer reports itself idle, stopped or finished. The printer is reached
through printer_helper.py, run in the venv that holds pycentauri, so the
bridge stays standard-library only.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

MACHINE = "Elegoo Centauri Carbon 0.4 nozzle"
QUALITIES = {
    "draft": "0.24mm Draft @Elegoo CC 0.4 nozzle",
    "standard": "0.20mm Standard @Elegoo CC 0.4 nozzle",
    "fine": "0.16mm Optimal @Elegoo CC 0.4 nozzle",
    "strong": "0.20mm Strength @Elegoo CC 0.4 nozzle",
}
MATERIALS = {
    "PLA": "Elegoo PLA @ECC",
    "PLA+": "Elegoo PLA+ @ECC",
    "Rapid PLA+": "Elegoo RAPID PLA+ @ECC",
    "PLA Matte": "Elegoo PLA Matte @ECC",
    "PETG": "Elegoo PETG @ECC",
    "Rapid PETG": "Elegoo RAPID PETG @ECC",
    "PETG PRO": "Elegoo PETG PRO @ECC",
    "ABS": "Elegoo ABS @ECC",
    "ASA": "Elegoo ASA @ECC",
    "TPU": "Elegoo TPU 95A @ECC",
    "PLA-CF": "Elegoo PLA-CF @ECC",
    "PETG-CF": "Elegoo PETG-CF @ECC",
}
MODEL_SUFFIXES = (".stl", ".3mf", ".obj")
MAX_MODEL_BYTES = 200 * 1024 * 1024
SLICE_TIMEOUT_S = 600

# PrintInfo.Status codes (pycentauri's PrintStatus, from Elegoo's own SDK).
IDLE, PAUSED, STOPPED, COMPLETED, PRINTING, ERROR = 0, 6, 8, 9, 13, 14
STARTABLE = (IDLE, STOPPED, COMPLETED)
WORDS = {
    0: "idle", 1: "homing", 5: "pausing", 6: "paused", 7: "stopping", 8: "stopped",
    9: "finished", 10: "checking the file", 11: "checking itself", 12: "resuming",
    13: "printing", 14: "reporting an error", 15: "levelling the bed", 16: "heating up",
    17: "testing resonance", 18: "starting", 19: "levelling done", 20: "heated",
    21: "homed", 22: "resonance test done",
}
_ESTIMATE = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M)


class PrinterError(Exception):
    """Something the user can be told: one plain sentence."""


def parse_minutes(gcode_head):
    """Minutes from the slicer's estimate line ('1d 2h 3m 4s', any part optional), or None."""
    match = _ESTIMATE.search(gcode_head)
    if not match:
        return None
    total = 0
    found = False
    for value, unit in re.findall(r"(\d+)\s*([dhms])", match.group(1)):
        found = True
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return max(1, round(total / 60)) if found else None


def startable_problem(status):
    """Why a print cannot be started on this status now, or None."""
    state, code = status.get("state"), status.get("print_status")
    if state not in (0, None) or code not in STARTABLE:
        return f"The printer is {WORDS.get(code, 'busy')} right now."
    return None


def describe(status, minutes=None):
    """A status the voice model can say: words, not codes."""
    code = status.get("print_status")
    out = {"printer": WORDS.get(code, "busy") if code is not None else "unknown"}
    progress = status.get("progress")
    if code not in (IDLE, None) and isinstance(progress, (int, float)):
        out["progress_percent"] = int(progress)
        if minutes and code in (PRINTING, PAUSED) and 0 <= progress < 100:
            out["about_minutes_left"] = max(1, round(minutes * (100 - progress) / 100))
    if status.get("total_layers"):
        out["layer"] = f"{status.get('layer')} of {status.get('total_layers')}"
    return out


class Printer:
    def __init__(self, config, cache_dir):
        conf = config.get("printer") or {}
        self.host = (conf.get("host") or "").strip()
        default_python = "~/.local/share/echomuse-claude-bridge/venv/bin/python"
        self.python = os.path.expanduser(conf.get("python") or default_python)
        self.helper = os.path.expanduser(conf.get("helper") or str(Path(__file__).with_name("printer_helper.py")))
        self.slicer = conf.get("slicer") or "elegoo-slicer"
        # What is usually loaded; offered when Claude asks, used when no material is given.
        self.material = conf.get("material") or "PLA"
        self.profiles = Path(os.path.expanduser(conf.get("profiles") or "/opt/elegoo-slicer/resources/profiles/Elegoo"))
        self.datadir = Path(cache_dir) / "slicer"
        # The CC1's control port takes few connections and dislikes churn: one at a time.
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.host)

    def problems(self):
        """What is missing for printing, checked at start-up so it is said once, early."""
        if not self.enabled:
            return []
        out = []
        if not Path(self.python).is_file():
            out.append(f"no Python at {self.python} (make the venv and pip install pycentauri)")
        if not Path(self.helper).is_file():
            out.append(f"no helper at {self.helper}")
        if self.material not in MATERIALS:
            out.append(f"material {self.material!r} is not one of: " + ", ".join(MATERIALS))
            self.material = "PLA"
        if not shutil.which(self.slicer):
            out.append(f"slicer `{self.slicer}` not found")
        for path in self._profile_paths(self.material, "standard"):
            if not path.is_file():
                out.append(f"missing slicer profile {path}")
        return out

    def _profile_paths(self, material, quality):
        return (self.profiles / "machine" / "ECC" / f"{MACHINE}.json",
                self.profiles / "process" / "ECC" / f"{QUALITIES[quality]}.json",
                self.profiles / "filament" / "ECC" / f"{MATERIALS[material]}.json")

    def _standalone(self, path):
        """A preset the slicer's command line will load.

        Elegoo's Draft, Optimal and Strength presets inherit from the Standard
        one beside them and omit `from` and `compatible_printers`; the command
        line refuses such a file ("The input preset file is invalid"), so it
        gets a copy with its parents merged in, as the slicer's own UI does.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return path
        if data.get("from"):
            return path
        merged, seen, current = dict(data), {path}, data
        while not current.get("from") and current.get("inherits"):
            parent = path.with_name(current["inherits"] + ".json")
            if parent in seen or not parent.is_file():
                return path
            seen.add(parent)
            current = json.loads(parent.read_text(encoding="utf-8"))
            merged = {**current, **merged}
            merged["inherits"] = current.get("inherits", "")
        merged["from"] = current.get("from", "system")
        merged["name"] = data.get("name", path.stem)
        self.datadir.mkdir(parents=True, exist_ok=True)
        out = self.datadir / ("standalone-" + path.name)
        out.write_text(json.dumps(merged, indent=1), encoding="utf-8")
        return out

    def _helper(self, *args, timeout=60):
        cmd = [self.python, self.helper, self.host, *args]
        with self.lock:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise PrinterError("The printer did not answer in time.")
            except OSError as error:
                raise PrinterError(f"The printer helper could not run ({error.strerror}).")
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        try:
            data = json.loads(lines[-1]) if lines else {}
        except ValueError:
            data = {}
        if proc.returncode != 0 or "error" in data:
            raise PrinterError("The printer could not be reached or refused: "
                               + str(data.get("error") or (proc.stderr.strip().splitlines() or ["no answer"])[-1])[:200])
        return data

    def status(self):
        return self._helper("status", timeout=45)

    def slice(self, model, out_dir, material="PLA", quality="standard"):
        """G-code for `model`, sliced with the printer's own profiles. Returns (path, minutes)."""
        if material not in MATERIALS:
            raise PrinterError("Unknown material; use one of " + ", ".join(MATERIALS))
        if quality not in QUALITIES:
            raise PrinterError("Unknown quality; use one of " + ", ".join(QUALITIES))
        machine, process, filament = self._profile_paths(material, quality)
        process = self._standalone(process)
        out_dir = Path(out_dir)
        work = out_dir / ".slicing"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        self.datadir.mkdir(parents=True, exist_ok=True)
        cmd = [self.slicer, "--datadir", str(self.datadir), "--slice", "0", "--outputdir", str(work),
               "--load-settings", f"{machine};{process}", "--load-filaments", str(filament), str(model)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SLICE_TIMEOUT_S, cwd=str(work))
        except subprocess.TimeoutExpired:
            raise PrinterError("Slicing took more than ten minutes and was stopped.")
        gcode = work / "plate_1.gcode"
        try:
            result = json.loads((work / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {}
        if proc.returncode != 0 or result.get("return_code", 0) != 0 or not gcode.is_file():
            why = result.get("error_string") or "the slicer failed"
            warnings = [p.get("warning_message") for p in result.get("sliced_plates", []) if p.get("warning_message")]
            raise PrinterError(f"Slicing failed: {why}" + (f" ({warnings[0]})" if warnings else ""))
        final = out_dir / (Path(model).stem + f"-{re.sub(r'[^A-Za-z0-9]', '', material.replace('+', 'plus'))}-{quality}.gcode")
        gcode.replace(final)
        shutil.rmtree(work, ignore_errors=True)
        # The estimate is in the footer, ahead of the settings dump; a header
        # read finds it only for a part small enough to fit the read.
        with open(final, "rb") as handle:
            handle.seek(max(0, final.stat().st_size - 1024 * 1024))
            minutes = parse_minutes(handle.read().decode("utf-8", "replace"))
        return final, minutes

    def upload(self, gcode, remote_name):
        return self._helper("upload", str(gcode), remote_name, timeout=400)

    def start(self, remote_name):
        return self._helper("start", remote_name, timeout=60)

    def control(self, action):
        if action not in ("pause", "resume", "stop"):
            raise PrinterError("Unknown printer action.")
        return self._helper(action, timeout=60)
