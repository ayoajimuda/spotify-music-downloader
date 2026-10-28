"""
spotDL update checker module.
Checks if a newer version of spotDL is available and tells the user how to update it.

spotDL finds the audio on YouTube Music through yt-dlp, so the pip update
command upgrades both: an old yt-dlp is the usual cause of failed downloads.

Usage:
    info = check_spotdl_updates()
    notify_update_available(info)
    check_on_startup()            # one line: up to date, or the update notice
"""

import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from managers import log_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import program_version, spotdl_command

PYPI_URL = "https://pypi.org/pypi/spotdl/json"
RELEASES_URL = "https://github.com/spotDL/spotify-downloader/releases/latest"


def get_installed_version() -> Optional[str]:
    """The installed spotDL's version (e.g. "4.6.0"), or None if it isn't installed."""
    version, _ = program_version("spotdl")
    return version or None


def get_latest_version(timeout: float = 5) -> Optional[str]:
    """The newest spotDL on PyPI, or None if it can't be fetched."""
    request = urllib.request.Request(PYPI_URL, headers={"User-Agent": "spotify-music-downloader"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.loads(response.read().decode("utf-8"))["info"]["version"])
    except (OSError, ValueError, KeyError):
        return None


def parse_version(version_str: str) -> Tuple[int, ...]:
    """"4.6.0" -> (4, 6, 0), for comparing."""
    return tuple(int(part) for part in re.findall(r"\d+", version_str or ""))


def is_update_available(current: str, latest: str) -> bool:
    """True if `latest` is newer than `current`. False if either is unknown."""
    current_tuple, latest_tuple = parse_version(current), parse_version(latest)
    return bool(current_tuple and latest_tuple and latest_tuple > current_tuple)


def _pip(python: str = None) -> str:
    python = python or Path(sys.executable).name
    if " " in python:
        python = f'"{python}"'
    return f"{python} -m pip install --upgrade spotdl yt-dlp"


def update_command(path: Optional[str] = None) -> str:
    """
    The update command that matches how spotDL was installed. A standalone
    spotDL program (the release download, or the one bundled with this app)
    can't update itself: that gives "download:<page>" instead.
    """
    if path is None:
        command, _ = spotdl_command()
        if command and len(command) > 1:            # python -m spotdl
            return _pip(command[0])
        path = command[0] if command else None
    if not path:
        return _pip()
    low = str(path).replace("\\", "/").lower()
    if "/pipx/" in low:
        return "pipx upgrade spotdl"
    if "/homebrew/" in low or "/cellar/" in low or "/linuxbrew/" in low:
        return "brew upgrade spotdl"
    if "/scripts/" in low or _is_script(path):
        return _pip()                               # pip's launcher
    if getattr(sys, "frozen", False) or not _is_script(path):
        return f"download:{RELEASES_URL}"
    return _pip()


def is_download(command: str) -> bool:
    """True when the update is a manual download rather than a command."""
    return (command or "").startswith("download:")


def describe_command(command: str) -> str:
    """The update command as shown to the user."""
    if is_download(command):
        return f"download the newest spotDL from {command[len('download:'):]}"
    return command


def _is_script(path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def check_spotdl_updates() -> dict:
    """
    Check if spotDL has updates available.

    Returns a dict with:
        'update_available', 'current_version', 'latest_version',
        'update_command' (how to update or install it), 'message'
    """
    command_list, where = spotdl_command()
    current_version = get_installed_version()
    command = update_command()

    if not current_version:
        reason = where if not command_list else "it's there but didn't report a version"
        return {
            'update_available': False,
            'current_version': None,
            'latest_version': None,
            'update_command': command,
            'message': f"spotDL isn't working ({reason}). Install it with: {_pip()}",
        }

    latest_version = get_latest_version()
    if not latest_version:
        return {
            'update_available': False,
            'current_version': current_version,
            'latest_version': None,
            'update_command': command,
            'message': 'Could not check for spotDL updates (network unavailable?)',
        }

    has_update = is_update_available(current_version, latest_version)
    return {
        'update_available': has_update,
        'current_version': current_version,
        'latest_version': latest_version,
        'update_command': command,
        'message': (f"spotDL update available: {current_version} → {latest_version}"
                    if has_update else f"spotDL is up to date ({current_version})"),
    }


def notify_update_available(update_info: dict) -> None:
    """Display the result of check_spotdl_updates()."""
    if not update_info:
        return
    if update_info['update_available']:
        Enhanced_Menu.print_section("spotDL UPDATE AVAILABLE")
        print(f"  Current version: {update_info['current_version']}")
        print(f"  Latest version:  {update_info['latest_version']}")
        how = update_info['update_command']
        if is_download(how):
            print(f"\n  To update, {describe_command(how)}\n")
        else:
            print(f"\n  To update, run:\n    {how}\n")
        log_manager.log_warning(update_info['message'], console=False)
    elif update_info['current_version']:
        Enhanced_Menu.print_status(update_info['message'],
                                   "success" if update_info['latest_version'] else "warning")
    else:
        Enhanced_Menu.print_status(update_info['message'], "error")


def check_on_startup() -> Optional[dict]:
    """
    For program start: one line saying spotDL is up to date, or the update
    notice. Nothing is saved to disk; offline starts are skipped quietly, and a
    missing spotDL is already reported by the dependency check.
    """
    try:
        info = check_spotdl_updates()
    except Exception:
        return None
    if info.get('update_available'):
        notify_update_available(info)
    elif info.get('current_version') and info.get('latest_version'):
        Enhanced_Menu.print_status(info['message'], "success")
    return info


# Same names as the yt-dlp checker, for code written against it.
check_ytdlp_updates = check_spotdl_updates
