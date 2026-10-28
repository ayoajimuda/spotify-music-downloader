"""Check for a newer spotDL, and update it, using tools/spotdl_update_checker.py.

The checker works out the update command from how spotDL is installed (pip,
pipx, brew); this runs it in the background. The pip command updates yt-dlp
too, since spotDL gets the audio through it. A standalone spotDL program (the
release download, or the one bundled with the app) can't update itself: the
status then carries the download page instead of a command.
"""

import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from tools import spotdl_update_checker as checker
from tools.dependency_check import run_program, spotdl_command


@dataclass
class UpdateStatus:
    installed: Optional[str] = None
    latest: Optional[str] = None
    location: str = ""
    method: str = "unknown"
    command: Optional[List[str]] = None
    download_url: str = ""         # set instead of command for a standalone spotDL
    error: str = ""

    @property
    def outdated(self) -> bool:
        def parts(v):
            return tuple(int(p) for p in re.findall(r"\d+", v or ""))
        return bool(self.installed and self.latest and parts(self.installed) < parts(self.latest))


def _split_command(command: str) -> List[str]:
    """'"C:\\...\\python.exe" -m pip install -U spotdl' -> a list for run_program."""
    parts = shlex.split(command, posix=False)          # keeps Windows backslashes intact
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in parts]


def _method(command: str) -> str:
    if checker.is_download(command):
        return "standalone"
    for word in ("pipx", "pip", "brew"):
        if word in command:
            return word
    return "unknown"


def check_spotdl(online: bool = True) -> UpdateStatus:
    """Installed and latest versions, and the command that updates spotDL."""
    status = UpdateStatus()
    command_list, where = spotdl_command()
    status.installed = checker.get_installed_version()
    if command_list:
        status.location = command_list[0] if len(command_list) == 1 else \
            f"{command_list[0]} -m spotdl"
    command = checker.update_command()
    status.method = _method(command or "")
    if checker.is_download(command):
        status.download_url = command[len("download:"):]
    elif command:
        status.command = _split_command(command)
    if not status.installed:
        status.error = f"spotDL isn't installed ({where})"
    if online:
        status.latest = checker.get_latest_version()
        if not status.latest:
            status.error = status.error or "Couldn't check for a newer version (no internet?)."
    return status


def update_spotdl(status: UpdateStatus) -> Tuple[bool, str]:
    """Run the update command. (success, message)."""
    if status.download_url:
        return False, f"This spotDL can't update itself. Download the newest one from {status.download_url}"
    if not status.command:
        return False, "Couldn't tell how spotDL was installed; update it the same way you installed it."
    code, output = run_program(status.command, timeout=900)
    if code != 0:
        tail = "\n".join((output or "").strip().splitlines()[-5:])
        return False, f"Update failed:\n{tail}" if tail else "Update failed."
    after = checker.get_installed_version()
    if after and status.installed and after != status.installed:
        return True, f"Updated: {status.installed} → {after}"
    return (True, f"spotDL is now {after} (yt-dlp updated too).") if after else \
        (False, "The update ran, but spotDL still can't be found.")


class SpotdlUpdateWorker(QThread):
    checked = Signal(object)              # an UpdateStatus
    finished_update = Signal(bool, str)   # success, message

    def __init__(self, mode: str = "check", status=None):
        """mode 'check' looks online for the latest version; 'update' runs the update."""
        super().__init__()
        self.mode = mode
        self.status = status

    def run(self):
        if self.mode == "update":
            try:
                ok, message = update_spotdl(self.status or check_spotdl(online=False))
            except Exception as error:
                ok, message = False, f"Update failed: {type(error).__name__}: {error}"
            self.finished_update.emit(ok, message)
            return
        try:
            self.checked.emit(check_spotdl(online=True))
        except Exception as error:
            self.checked.emit(UpdateStatus(error=f"Couldn't check: {error}"))
