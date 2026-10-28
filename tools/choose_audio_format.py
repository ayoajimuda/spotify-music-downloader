"""Pick the audio format and bitrate, with what each choice means for size and quality.

Saves through config_manager, so the choice is kept, and applies it to the
downloader straight away if one is given.

Usage:
    from tools.choose_audio_format import choose_audio_format, set_audio_format
    choose_audio_format(downloader)                 # interactive
    ok, message = set_audio_format("opus", "auto")  # no prompts
"""

from typing import Optional, Tuple

from managers import config_manager
from menu.colorful_menu import Enhanced_Menu

# format: (summary, lossless?)
FORMATS = {
    "mp3": ("Plays on everything (cars, old players, every app).", False),
    "m4a": ("AAC. Great on Apple devices; can usually keep YouTube Music's own audio "
            "without re-encoding.", False),
    "opus": ("Best quality for its size; can usually keep YouTube Music's audio without "
             "re-encoding. Some older players and cars can't play it.", False),
    "ogg": ("Vorbis. Open format, good on Android and Linux; less common elsewhere.", False),
    "flac": ("Lossless: no further quality loss after download, but about 5x the size "
             "of a 320k MP3.", True),
    "wav": ("Uncompressed: biggest files, and no tags or album art.", True),
}

BITRATES = (
    ("320k", "highest MP3/AAC setting"),
    ("256k", "very good"),
    ("192k", "good; hard to tell apart from higher for most listeners"),
    ("160k", "about what YouTube Music streams at (spotDL's source)"),
    ("128k", "standard; smaller files"),
    ("96k", "small files, speech and podcasts"),
    ("auto", "let spotDL pick (keeps the source quality)"),
)

# Typical average bitrates (kbps) for size estimates where there's no bitrate setting.
TYPICAL_KBPS = {"flac": 900, "wav": 1411, "auto": 160}
SONG_MINUTES = 4


def estimate_mb(fmt: str, quality: str, minutes: float = SONG_MINUTES) -> float:
    """Rough file size in MB for a song of `minutes` length."""
    if fmt in TYPICAL_KBPS:
        kbps = TYPICAL_KBPS[fmt]
    elif quality in ("auto", "disable"):
        kbps = TYPICAL_KBPS["auto"]
    else:
        kbps = int(quality.rstrip("k") or 0)
    return kbps * 60 * minutes / 8 / 1024


def set_audio_format(fmt: str, quality: Optional[str] = None,
                     downloader=None) -> Tuple[bool, str]:
    """Save the format (and bitrate, if given) and apply them. (success, message)."""
    ok, message = config_manager.update_config("audio_format", fmt)
    if not ok:
        return False, message
    if quality is not None:
        ok, message = config_manager.update_config("audio_quality", quality)
        if not ok:
            return False, message
    config = config_manager.load()
    if downloader is not None:
        downloader.audio_format = config["audio_format"]
        downloader.audio_quality = config["audio_quality"]
    shown = config["audio_format"] + ("" if FORMATS[config["audio_format"]][1]
                                      else f" at {config['audio_quality']}")
    return True, f"Audio format set to {shown}"


def choose_audio_format(downloader=None) -> Optional[Tuple[str, str]]:
    """Interactive picker. Returns (format, bitrate) as saved, or None if cancelled."""
    import questionary

    config = config_manager.load()
    current_fmt, current_quality = config["audio_format"], config["audio_quality"]
    lossless_now = FORMATS[current_fmt][1]
    Enhanced_Menu.print_key_value("Current", current_fmt + ("" if lossless_now
                                                            else f" at {current_quality}"),
                                  width=8)
    print()

    fmt = questionary.select(
        "Audio format:",
        choices=[questionary.Choice(f"{name:<5} {summary}", value=name)
                 for name, (summary, _) in FORMATS.items()] + ["Back"],
        default=current_fmt).ask()
    if fmt in (None, "Back"):
        return None

    quality = current_quality
    if FORMATS[fmt][1]:
        Enhanced_Menu.print_status(f"{fmt} is lossless, so there's no bitrate to choose. "
                                   f"A {SONG_MINUTES}-minute song is about "
                                   f"{estimate_mb(fmt, quality):.0f} MB.", "info")
        Enhanced_Menu.print_status("spotDL gets the audio from YouTube Music, which is already "
                                   "compressed, so lossless keeps "
                                   "it exactly, but can't make it better than the source.",
                                   "info")
    else:
        choices = [questionary.Choice(
            f"{rate:<5} {about}  (~{estimate_mb(fmt, rate):.1f} MB per {SONG_MINUTES}-min song)",
            value=rate) for rate, about in BITRATES]
        choices.append(questionary.Choice("other  type a value (8k-320k)", value="other"))
        default = current_quality if current_quality in [r for r, _ in BITRATES] else None
        quality = questionary.select("Bitrate:", choices=choices, default=default).ask()
        if quality is None:
            return None
        if quality == "other":
            def valid(text: str):
                _, error = config_manager.check_value("audio_quality", text)
                return True if error is None else "Use a value like 64k, 112k or 224k"
            quality = questionary.text("Bitrate:", default=current_quality,
                                       validate=valid).ask()
            if quality is None:
                return None
        if fmt in ("opus", "m4a") and quality not in ("auto", "disable") and \
                int(quality.rstrip("k")) > 192:
            Enhanced_Menu.print_status("YouTube Music's audio (spotDL's source) is usually 128-160 kbps, so a "
                                       "higher bitrate makes bigger files without sounding "
                                       "better. 'auto' keeps the source as it is.", "info")

    ok, message = set_audio_format(fmt, quality, downloader)
    Enhanced_Menu.print_status(message, "success" if ok else "error")
    if not ok:
        return None
    saved = config_manager.load()
    return saved["audio_format"], saved["audio_quality"]
