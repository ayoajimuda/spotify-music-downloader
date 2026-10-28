"""Base downloader for the Spotify music downloader. Contains only the download functions.

spotDL does the work: it reads the song list from Spotify, finds each song on
YouTube Music, downloads the audio and tags it with Spotify's metadata.

  fetch_info()    `spotdl save` - a link's songs and details, nothing downloaded
  run_download()  `spotdl download` - one song (or one whole link), with a stall
                  watchdog, throttle detection and per-song results
  albums, playlists and artists are downloaded song by song (a few at a time),
  so every song gets its own retries, retry-queue entry and archive line.
"""

import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from colorama import init, Fore, Style

from downloader import batch_downloader, rate_limiter
from downloader.assist_methods import cleanup_directory
from managers import config_manager, log_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program, spotdl_command

init(autoreset=True)

SPOTIFY_KINDS = ("track", "album", "playlist", "artist")
TRACK_URL = "https://open.spotify.com/track/{}"
_SPOTIFY_LINK = re.compile(
    r"^(?:https?://)?(?:open|play)\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?"
    r"(track|album|playlist|artist)/([A-Za-z0-9]{22})(?:[/?#].*)?$", re.IGNORECASE)
_SPOTIFY_URI = re.compile(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})$",
                          re.IGNORECASE)
_TRACK_ID = re.compile(r"open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]{22})")

UNAVAILABLE_TITLES: Set[str] = set()   # kept for older callers; Spotify lists don't have these
INFO_TIMEOUT = 600        # seconds to read a link's songs (big playlists take a while)
STOP_AFTER_THROTTLED = 3  # throttled songs in a row before a batch stops
SONG_FILE = "{artists} - {title}.{output-ext}"   # spotDL's own output-template variables

# spotDL's own messages (spotdl/download/downloader.py and progress_handler.py)
_DOWNLOADED = re.compile(r'Downloaded "(?P<name>.+?)": (?P<url>\S+)')
_SKIPPED = re.compile(r"Skipping (?P<name>.+?) \((?:file already exists|skip file found)\)")
_SONG_ERROR = re.compile(r"(?P<url>https://open\.spotify\.com/track/[A-Za-z0-9]{22}) - "
                         r"(?P<kind>[A-Za-z_]\w*): (?P<message>.*)")
_ERROR_LINE = re.compile(r"^(?:ERROR:?\s+)?(?P<kind>[A-Z]\w*(?:Error|Exception)): (?P<message>.+)")
_ARCHIVE_SAVED = re.compile(r"Saved archive with \d+ urls")   # songs in the archive aren't logged

# Spotify's (and spotipy's) wording for "slow down", next to YouTube's in rate_limiter.
_SPOTIFY_THROTTLE = ("rate/request limit", "http status: 429", "status code 429", "error 429",
                     "429 client error", "too many requests", "retry will occur after",
                     "rate limited", "rate-limited")


class _NullBar:
    """No-op stand-in for tqdm so worker threads don't render nested bars."""
    total = None
    n = 0

    def set_description(self, *args, **kwargs):
        pass

    def set_postfix_str(self, *args, **kwargs):
        pass

    def update(self, *args, **kwargs):
        pass

    def refresh(self):
        pass

    def close(self):
        pass


# ==================== Link helpers ====================
def parse_spotify_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """('track'|'album'|'playlist'|'artist', id) for a Spotify link or URI, else (None, None)."""
    text = (url or "").strip().strip('"').strip("'")
    match = _SPOTIFY_LINK.match(text) or _SPOTIFY_URI.match(text)
    return (match.group(1).lower(), match.group(2)) if match else (None, None)


def is_spotify_url(url: str) -> bool:
    """True for open.spotify.com track/album/playlist/artist links (and spotify: URIs)."""
    return parse_spotify_url(url)[0] is not None


def spotify_kind(url: str) -> Optional[str]:
    return parse_spotify_url(url)[0]


def normalize_spotify_url(url: str) -> str:
    """The plain https://open.spotify.com/<kind>/<id> form (drops ?si=, intl-xx/, URIs)."""
    kind, spotify_id = parse_spotify_url(url)
    return f"https://open.spotify.com/{kind}/{spotify_id}" if kind else (url or "").strip()


def track_id(url: str) -> Optional[str]:
    """The Spotify ID of a track link, else None."""
    kind, spotify_id = parse_spotify_url(url)
    return spotify_id if kind == "track" else None


def extract_playlist_id(url: str) -> Optional[str]:
    """The ID from an album/playlist/artist (or track) link, used to name its archive."""
    return parse_spotify_url(url)[1]


def safe_name(name: str, fallback: str = "_") -> str:
    """A string that's safe as a file or folder name on any OS."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(". ")
    return name[:150].rstrip(". ") or fallback


def template_safe(name: str, fallback: str = "_") -> str:
    """A folder name for a spotDL output template: braces would read as template variables."""
    return safe_name(name, fallback).replace("{", "(").replace("}", ")")


def looks_throttled(text: str) -> bool:
    """YouTube refusing traffic (rate_limiter's markers) or Spotify's API limits."""
    low = (text or "").lower()
    return rate_limiter.looks_throttled(text) or any(m in low for m in _SPOTIFY_THROTTLE)


def classify_failure(output: str, timed_out: bool = False, timeout: int = 0) -> str:
    """A short, readable reason for a failed spotDL run (most specific first)."""
    low = (output or "").lower()
    if timed_out:
        return f"Stalled: no output for {timeout}s, stopped"
    if "invalid_client" in low or "invalid client" in low:
        return "Spotify rejected the Client ID / Secret (check them in Settings, or clear them)"
    if any(m in low for m in _SPOTIFY_THROTTLE[:5]) or "retry will occur after" in low:
        return "Spotify rate limit (429): wait a while, or set your own Client ID/Secret in Settings"
    if re.search(r"http error 429", low):
        return "HTTP 429 from YouTube (rate limited, slow down or wait)"
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube requires authentication (sign in to confirm you're not a bot - check cookies)"
    if re.search(r"http error 403|\b403[: ]+forbidden", low):
        return "HTTP 403 (YouTube refused the audio: update spotDL / yt-dlp, then check cookies)"
    if "no results found" in low or "could not find a match" in low:
        return "No match found on YouTube Music for this song"
    if "javascript runtime" in low or "no supported js" in low:
        return "Missing JS runtime (run: spotdl --download-deno)"
    if ("ffmpeg" in low and ("not found" in low or "not installed" in low)) or "ffmpegerror" in low:
        return "ffmpeg not found (install ffmpeg, run spotdl --download-ffmpeg, or set it in Settings)"
    if "confirm your age" in low or ("age" in low and "restrict" in low):
        return "Age restricted on YouTube (needs cookies from an adult account)"
    if "non existing id" in low or "invalid id" in low or "404" in low or "not found" in low:
        return "Spotify couldn't find that link (deleted, private, or mistyped?)"
    if "metadataerror" in low:
        return "Downloaded, but the metadata couldn't be embedded"
    if "proxyerror" in low or "connectionerror" in low or "max retries exceeded" in low:
        return "Network error (couldn't reach Spotify or YouTube)"
    if not (output or "").strip():
        return "No output from spotDL"
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    errors = [l for l in lines if _ERROR_LINE.match(l) or l.upper().startswith("ERROR")]
    return (errors[-1] if errors else lines[-1])[:300]


def read_save_file(path: Path) -> List[dict]:
    """The songs in a .spotdl save file (a JSON list of song records)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def song_entry(song: dict) -> dict:
    """One spotDL song record, in the shape the menus and GUI use."""
    url = song.get("url") or (TRACK_URL.format(song["song_id"]) if song.get("song_id") else "")
    return {
        "id": song.get("song_id") or track_id(url),
        "title": song.get("name") or "",
        "artist": song.get("artist") or "",
        "artists": song.get("artists") or [],
        "album": song.get("album_name") or "",
        "album_artist": song.get("album_artist") or "",
        "duration": song.get("duration") or 0,
        "track_number": song.get("track_number"),
        "thumbnail": song.get("cover_url") or "",
        "url": url,
    }


class SpotifyDownloader:
    """
    Downloader class that contains all the download functions
    """

    def __init__(self):
        self.__output_directory = Path.home() / "Music" / "Collection" / "Spotify"
        self.__audio_quality = "320k"
        self.__audio_format = "mp3"
        self.use_cookies = False
        self.max_retries = 2          # attempts for a struggling song
        self.retry_delay = 20         # seconds between attempts
        self.download_timeout = 120   # seconds of silence before spotDL is stopped

        self.debug = False
        self.max_concurrent = 3       # songs at once (albums, playlists, artists)
        self.yt_dlp_sleep_min = 3     # min seconds yt-dlp (inside spotDL) waits between requests
        self.yt_dlp_sleep_max = 7     # max seconds (random delay in this range)

        self.rate_limit_backoff = 300
        self.rate_limit_max_wait = 1800
        self._last_run_throttled = False

        # The modules this class works with. main_menu can replace any of them.
        self.log_manager = log_manager
        self.history = log_manager            # add_input lives in the log manager
        self.batch_file = batch_downloader

        self.archives_dir = Path(log_manager.HISTORY_DIR) / "archives"
        self.archives_dir.mkdir(parents=True, exist_ok=True)

        self.load_settings()

    # ==================== Settings ====================
    def load_settings(self, settings: Optional[dict] = None) -> None:
        """Take the saved settings from the config manager (or the dict given)."""
        settings = settings if settings is not None else config_manager.load()
        self.audio_format = settings["audio_format"]
        self.audio_quality = settings["audio_quality"]
        self.output_directory = config_manager.resolve_path(settings["output_directory"])
        self.use_cookies = bool(settings.get("use_cookies"))
        for key in ("max_retries", "retry_delay", "download_timeout", "max_concurrent",
                    "yt_dlp_sleep_min", "yt_dlp_sleep_max"):
            if key in settings:
                setattr(self, key, settings[key])

    # The settings menus still set the old name; keep both in step.
    @property
    def max_concurrency(self) -> int:
        return self.max_concurrent

    @max_concurrency.setter
    def max_concurrency(self, value: int):
        self.max_concurrent = value

    def get_user_preferences(self):
        """Ask for the download settings, save them, and apply them here."""
        from tools import ask_user_preference as preferences
        if not preferences.is_configured():
            from managers import cookie_manager
            preferences.configure(config_manager, Enhanced_Menu, cookie_manager)
        return preferences.get_user_preferences(self)

    # ==================== Public properties ====================
    @property
    def audio_format(self) -> str:
        """Current audio format (mp3, flac, etc.)."""
        return self.__audio_format

    @audio_format.setter
    def audio_format(self, value: str):
        if value in config_manager.VALID_FORMATS:
            self.__audio_format = value
        else:
            raise ValueError(f"Unsupported audio format: {value}")

    @property
    def audio_quality(self) -> str:
        """Current audio bitrate (320k, 192k, auto, etc.)."""
        return self.__audio_quality

    @audio_quality.setter
    def audio_quality(self, value: str):
        if value in config_manager.VALID_QUALITIES:
            self.__audio_quality = value
        else:
            raise ValueError(f"Unsupported audio quality: {value}")

    @property
    def output_directory(self) -> Path:
        """Output directory path."""
        return self.__output_directory

    @output_directory.setter
    def output_directory(self, path):
        self.__output_directory = Path(path)
        self.__output_directory.mkdir(parents=True, exist_ok=True)

    # ==================== Output templates ====================
    def output_template(self, kind: str = "track", title: str = "") -> str:
        """
        Where spotDL saves the songs, as one of its output templates:
          track     <music>/Artist - Title.mp3
          album     <music>/Album Artist/Album/Artist - Title.mp3
          playlist  <music>/<playlist name>/Artist - Title.mp3
          artist    <music>/<artist name>/Album/Artist - Title.mp3
          batch     <music>/<title, if given>/Artist - Title.mp3
        """
        out = self.__output_directory
        if kind == "album":
            return str(out / "{album-artist}" / "{album}" / SONG_FILE)
        if kind == "playlist":
            return str(out / template_safe(title or "Playlist", "Playlist") / SONG_FILE)
        if kind == "artist":
            return str(out / template_safe(title or "Artist", "Artist") / "{album}" / SONG_FILE)
        if kind == "batch" and title:
            return str(out / template_safe(title, "Batch") / SONG_FILE)
        return str(out / SONG_FILE)

    # ==================== spotDL helpers ====================
    def _spotdl(self) -> List[str]:
        """The command that runs spotDL (location from Settings, bundled, PATH, or python -m)."""
        command, where = spotdl_command()
        if not command:
            message = f"spotDL not found ({where}). Install it with: pip install -U spotdl"
            self.log_manager.log_error(message)
            raise RuntimeError(message)
        return command

    def _spotify_args(self) -> List[str]:
        """Your own Spotify app's credentials, when both are set in Settings."""
        cfg = config_manager.load()
        client_id = (cfg.get("spotify_client_id") or "").strip()
        client_secret = (cfg.get("spotify_client_secret") or "").strip()
        if client_id and client_secret:
            return ["--use-official-api", "--client-id", client_id,
                    "--client-secret", client_secret]
        return []

    def _common_args(self) -> List[str]:
        """Arguments every spotDL call shares: Spotify login, ffmpeg, cookies, quiet output."""
        args = ["--simple-tui", "--log-level", "INFO"]
        args += self._spotify_args()
        # spotDL needs the ffmpeg program itself; Settings may name its folder instead.
        path, where = find_program("ffmpeg")
        if path and not where.startswith("PATH"):
            args += ["--ffmpeg", str(path)]
        cookie_file = self._get_cookie_file()
        if cookie_file:
            args += ["--cookie-file", cookie_file]
        return args

    def _get_cookie_file(self) -> Optional[str]:
        """
        A usable cookie-file path if cookies are enabled, else None. These are
        YouTube Music cookies: spotDL gets the audio from YouTube Music.
        """
        if not self.use_cookies:
            return None
        from managers import cookie_manager          # late: it creates cookies/ on import
        active = cookie_manager.get_active_cookie_file()
        if active:
            return str(Path(active).resolve())
        folder = Path(cookie_manager.COOKIE_DIRECTORY)
        if folder.is_dir():
            candidates = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime,
                                reverse=True)
            if candidates:
                return str(candidates[0].resolve())
        self.log_manager.log_warning("Cookies are enabled but no cookie file was found. "
                                     "Add one in the Cookies menu.")
        return None

    @staticmethod
    def _env() -> dict:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["COLUMNS"] = "10000"      # rich (spotDL's console) would wrap lines at 80
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        return env

    @staticmethod
    def _query(url_or_search: str) -> str:
        """A Spotify link in its plain form; anything else (a search) as typed."""
        text = (url_or_search or "").strip().strip('"').strip("'")
        return normalize_spotify_url(text) if is_spotify_url(text) else text

    def fetch_info(self, url: str, item_type: str = "track") -> Tuple[bool, str, Optional[dict]]:
        """
        Read a link's songs without downloading (spotdl save). Returns (ok, message, info).

        info for a track:       {"_type": "video", "title", "artist", "album", "id", ...}
        info for anything else: {"_type": "playlist", "title", "entries": [songs], ...}
        A track can also be a search ("Artist - Title"); spotDL picks the best match.
        """
        query = self._query(url)
        kind = spotify_kind(query)
        if kind is None and item_type != "track":
            return False, f"Paste a Spotify {item_type} link (open.spotify.com/{item_type}/...)", None
        if kind and item_type in SPOTIFY_KINDS and kind != item_type:
            article = "an" if item_type[0] in "aeiou" else "a"
            return False, f"That's a Spotify {kind} link, not {article} {item_type}", None

        with tempfile.TemporaryDirectory(prefix="spotdl-save-") as folder:
            save_file = Path(folder) / "songs.spotdl"
            command = [*self._spotdl(), "save", query, "--save-file", str(save_file),
                       "--lyrics", "--threads", "4", "--simple-tui", "--log-level", "INFO",
                       *self._spotify_args()]
            code, output = run_program(command, timeout=INFO_TIMEOUT)
            if code is None:
                return False, output, None
            songs = read_save_file(save_file)
        if code != 0 and not songs:
            return False, classify_failure(output), None
        if not songs:
            what = "search" if kind is None else "link"
            return False, f"Spotify returned no songs for that {what}", None

        entries = [song_entry(s) for s in songs]
        first = songs[0]
        if (kind or "track") == "track":
            entry = entries[0]
            info = {"_type": "video", "kind": "track", "id": entry["id"], "title": entry["title"],
                    "artist": entry["artist"], "uploader": entry["artist"],
                    "artists": entry["artists"], "album": entry["album"],
                    "duration": entry["duration"], "thumbnail": entry["thumbnail"],
                    "webpage_url": entry["url"], "entries": entries}
        else:
            title = (first.get("list_name") or
                     (first.get("album_name") if kind == "album" else None) or
                     (first.get("artist") if kind == "artist" else None) or kind.title())
            artist = (first.get("album_artist") if kind == "album" else
                      first.get("artist") if kind == "artist" else "")
            info = {"_type": "playlist", "kind": kind, "id": parse_spotify_url(query)[1],
                    "title": title, "artist": artist or "", "uploader": artist or "",
                    "playlist_count": len(entries), "thumbnail": first.get("cover_url") or "",
                    "webpage_url": query, "entries": entries}
        return True, "ok", info

    # ========================= Download Functions ================================
    def run_download(self, url: str, output_template: str, additional_args=None,
                     show_progress: bool = True, threads: int = 1):
        """
        Runs spotDL on one song, search or link, with a progress bar and a stall watchdog.

        Args:
            url (str): a Spotify link, or a search ("Artist - Title")
            output_template (str): spotDL output template (folder + file name pattern)
            additional_args (list, optional): extra spotDL arguments
            show_progress (bool, optional): draw a progress bar. Defaults to True.
            threads (int, optional): songs spotDL downloads at once (whole links)

        Returns a CompletedProcess on success (or when everything was already there),
        with .downloaded, .skipped and .throttled. Raises CalledProcessError with
        .reason, .throttled and .failed ({song url: reason}) when any song failed,
        and RuntimeError if spotDL isn't installed.
        """
        if not output_template:
            raise ValueError("run_download requires an output template")
        folder = output_template.split("{")[0]
        if os.path.dirname(folder):
            os.makedirs(os.path.dirname(folder) if not folder.endswith(os.sep) else folder,
                        exist_ok=True)

        query = self._query(url)
        command = [*self._spotdl(), "download", query,
                   "--output", output_template,
                   "--format", self.__audio_format,
                   "--bitrate", self.__audio_quality,
                   "--overwrite", "skip",
                   "--print-errors",
                   "--threads", str(max(1, int(threads)))]
        if self.yt_dlp_sleep_max:
            command += ["--yt-dlp-args", f"--sleep-interval {int(self.yt_dlp_sleep_min)} "
                                         f"--max-sleep-interval {int(self.yt_dlp_sleep_max)}"]
        command += self._common_args()
        if additional_args:
            command.extend(additional_args if isinstance(additional_args, list) else [additional_args])

        progress_bar = tqdm(desc="Downloading", unit="song", leave=False, dynamic_ncols=True,
                            bar_format="{desc} {bar}| {n_fmt} [{elapsed}{postfix}]") \
            if show_progress else _NullBar()

        process = None
        watchdog = None
        throttled = False
        timed_out = threading.Event()
        self._last_run_throttled = False
        downloaded: List[str] = []
        skipped: List[str] = []
        failed: Dict[str, str] = {}
        inline_errors: List[str] = []
        archive_saved = False

        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, bufsize=1, encoding="utf-8", errors="replace",
                                       env=self._env())

            # download_timeout is "no output for N seconds", so long but healthy
            # downloads aren't killed mid-transfer.
            def _kill(proc):
                timed_out.set()
                try:
                    proc.kill()
                except Exception:
                    pass

            def _arm():
                t = threading.Timer(self.download_timeout, _kill, args=(process,))
                t.daemon = True
                t.start()
                return t

            watchdog = _arm()
            output_lines: List[str] = []
            try:
                for line in iter(process.stdout.readline, ''):
                    watchdog.cancel()
                    watchdog = _arm()
                    line = line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if len(output_lines) > 2000:
                        output_lines = output_lines[-400:]

                    if not throttled and looks_throttled(line):
                        throttled = True
                        self.log_manager.log_warning("Throttling detected (Spotify or YouTube)",
                                                     url=query, console=False)

                    done = _DOWNLOADED.search(line)
                    if done:
                        downloaded.append(done.group("name"))
                        progress_bar.update(1)
                        progress_bar.set_description(f"{Fore.GREEN}Downloaded{Style.RESET_ALL}")
                        continue
                    skip = _SKIPPED.search(line)
                    if skip:
                        skipped.append(skip.group("name"))
                        progress_bar.update(1)
                        continue
                    error = _SONG_ERROR.search(line)
                    if error:
                        failed[error.group("url")] = f"{error.group('kind')}: {error.group('message')}"
                        continue
                    if _ARCHIVE_SAVED.search(line):
                        archive_saved = True
                        continue
                    if _ERROR_LINE.match(line):
                        inline_errors.append(line)
                        continue
                    # "<song>: Downloading" / "Converting" / "Embedding metadata" ...
                    status = line.rsplit(": ", 1)[-1] if ": " in line else ""
                    if status in ("Downloading", "Converting", "Embedding metadata", "Processing"):
                        progress_bar.set_description(f"{Fore.CYAN}{status}{Style.RESET_ALL}")
                        progress_bar.refresh()
            finally:
                if watchdog is not None:
                    watchdog.cancel()

            process.wait()
            progress_bar.close()
            full_output = "\n".join(output_lines)
            if not throttled and looks_throttled(full_output):
                throttled = True
            self._last_run_throttled = throttled

            # One song that failed without a --print-errors line still failed.
            if inline_errors and not failed and not downloaded and not skipped:
                failed[query] = inline_errors[-1]

            # With --archive, songs downloaded earlier are dropped silently; the
            # archive line at the end shows the run itself went fine.
            ok = (process.returncode == 0 and not timed_out.is_set() and not failed
                  and (downloaded or skipped or archive_saved))
            if ok:
                result = subprocess.CompletedProcess(args=command, returncode=0,
                                                     stdout=full_output, stderr="")
                result.throttled = throttled
                result.downloaded = downloaded
                result.skipped = skipped
                result.already_downloaded = not downloaded
                if result.already_downloaded:
                    self.log_manager.log_info("Already downloaded (skipped)", url=query,
                                              console=False)
                return result

            if failed and (downloaded or skipped):
                reason = (f"{len(failed)} of {len(failed) + len(downloaded) + len(skipped)} "
                          f"songs failed: {classify_failure(next(iter(failed.values())))}")
            elif failed:
                reason = classify_failure("\n".join(failed.values()) + "\n" + full_output,
                                          timed_out.is_set(), self.download_timeout)
            elif process.returncode == 0 and not timed_out.is_set():
                reason = "spotDL didn't download anything (no songs found for that link or search)"
            else:
                reason = classify_failure(full_output, timed_out.is_set(), self.download_timeout)
            self.log_manager.log_error(f"Download failed: {reason}", url=query, console=False)
            failure = subprocess.CalledProcessError(process.returncode or 1, command,
                                                    output=full_output, stderr="")
            failure.throttled = throttled
            failure.reason = reason
            failure.failed = failed
            failure.downloaded = downloaded
            failure.skipped = skipped
            raise failure

        except FileNotFoundError:
            progress_bar.close()
            error_msg = "spotDL not found. Please install it with: pip install -U spotdl"
            self.log_manager.log_error(error_msg)
            raise RuntimeError(error_msg)
        except subprocess.CalledProcessError:
            progress_bar.close()
            raise
        except BaseException as e:            # includes Ctrl-C: don't leave spotDL running
            progress_bar.close()
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            if not isinstance(e, KeyboardInterrupt):
                self.log_manager.log_error(f"Unexpected error in run_download: {e}", url=query)
            raise

    def _download_with_retry(self, url: str, output_template: str, additional_args: list = None,
                             item_type: str = "item", show_progress: bool = True,
                             metadata: Optional[dict] = None,
                             threads: int = 1) -> Tuple[bool, str, bool]:
        """
        Download with up to max_retries attempts. Returns (success, last_error, throttled)
        so callers that batch many links can report why each one failed, and can
        tell a bad song apart from a refusing host. A throttled attempt isn't
        retried straight away: that only deepens the rate limit.
        """
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            if attempt > 1:
                if show_progress:
                    Enhanced_Menu.print_status(
                        f"Attempt {attempt}/{self.max_retries} in {self.retry_delay}s...", "info")
                time.sleep(self.retry_delay)
            try:
                result = self.run_download(url, output_template, additional_args,
                                           show_progress=show_progress, threads=threads)
                if not getattr(result, "already_downloaded", False):
                    self.log_manager.log_success(f"Downloaded {item_type}", url=url,
                                                 metadata=metadata, item_type=item_type,
                                                 console=False)
                return True, "", False
            except subprocess.CalledProcessError as e:
                last_error = getattr(e, "reason", "") or str(e)[:300]
                if show_progress:
                    Enhanced_Menu.print_status(f"Attempt {attempt} failed: {last_error}", "error")
                if getattr(e, "throttled", False):
                    self.log_manager.log_failure(f"Throttled while downloading {item_type}",
                                                 url=url, metadata=metadata, item_type=item_type,
                                                 error=last_error, console=False)
                    return False, last_error, True
                if last_error.startswith(("No match found", "Spotify couldn't find")):
                    break                     # the same search will fail the same way
            except RuntimeError:
                raise                         # spotDL is missing - retrying won't help
            except KeyboardInterrupt:
                raise
            except Exception as e:
                last_error = str(e)[:300]
                self.log_manager.log_error(f"Unexpected error in attempt {attempt}: {e}", url=url)
        self.log_manager.log_failure(f"Failed after {self.max_retries} attempts", url=url,
                                     metadata=metadata, item_type=item_type, error=last_error,
                                     console=False)
        return False, last_error, False

    # Older name for the same thing.
    _retry_logic = _download_with_retry

    def _backoff_seconds(self, streak: int) -> float:
        """How long to wait after `streak` throttled songs in a row (doubling, capped)."""
        return min(self.rate_limit_max_wait, self.rate_limit_backoff * 2 ** max(0, streak - 1))

    @staticmethod
    def _penalize(seconds: float) -> None:
        """Tell the rate limiter to pause everyone. Never fails the download over it."""
        try:
            rate_limiter.penalize(seconds)
        except Exception:
            pass

    # ==================== Archives (already-downloaded songs) ====================
    # Same format as spotDL's own --archive: one track link per line.
    def _archive_path(self, url: str) -> Path:
        key = extract_playlist_id(url) or safe_name(url)[:40]
        return self.archives_dir / f"spotify_{safe_name(key)}.txt"

    @staticmethod
    def _read_archive(path: Optional[Path]) -> Set[str]:
        """Spotify track IDs in an archive (older "youtube <id>" lines are read too)."""
        if not path or not path.is_file():
            return set()
        ids = set()
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                match = _TRACK_ID.search(line)
                if match:
                    ids.add(match.group(1))
                elif line.startswith("youtube "):
                    ids.add(line.split(" ", 1)[1].strip())
        except OSError:
            pass
        return ids

    @staticmethod
    def _add_to_archive(path: Optional[Path], song_id: str) -> None:
        if not path or not song_id:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(TRACK_URL.format(song_id) + "\n")
        except OSError:
            pass

    # ==================== Albums, playlists, artists (several songs at once) ====================
    def parallel_run_download(self, tasks, archive_path: Optional[Path], max_workers: int = 3,
                              desc: str = "Downloading", source: str = "") -> Dict[str, bool]:
        """
        Download several songs at once.

        Args:
            tasks: [{"url", "id", "title", "template"}, ...]
            archive_path: songs whose ID is in this file are skipped; finished
                          songs are added to it, so a re-run carries on
            max_workers: downloads at the same time
            desc: label for the progress bar
            source: where the links came from (stored with failures)

        Returns:
            {url: True/False} for every task that was attempted.
        """
        done_ids = self._read_archive(archive_path)
        todo = [t for t in tasks if t.get("id") not in done_ids]
        results: Dict[str, bool] = {}
        if not todo:
            return results

        stop = threading.Event()
        streak = 0

        def work(task):
            if stop.is_set():
                return task, None, "", False
            rate_limiter.acquire()            # shared pacing across all workers
            if stop.is_set():
                return task, None, "", False
            ok, error, throttled = self._download_with_retry(
                task["url"], task["template"], [], item_type="track", show_progress=False,
                metadata={"title": task.get("title"), "artist": task.get("artist")})
            return task, ok, error, throttled

        bar = tqdm(total=len(todo), desc=desc, unit="song", dynamic_ncols=True)
        pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
        futures = [pool.submit(work, task) for task in todo]
        try:
            for future in as_completed(futures):
                task, ok, error, throttled = future.result()
                if ok is None:
                    continue                  # skipped after a stop
                results[task["url"]] = ok
                bar.update(1)
                name = (task.get("label") or task.get("title") or task["url"])[:60]
                if ok:
                    streak = 0
                    self._add_to_archive(archive_path, task.get("id"))
                    tqdm.write(f"  {Fore.GREEN}done{Style.RESET_ALL}  {name}")
                    continue
                self.log_manager.record_failure(task["url"], task.get("label") or task.get("title", ""),
                                                error, source, throttled=throttled,
                                                item_type="track")
                tqdm.write(f"  {Fore.RED}failed{Style.RESET_ALL} {name}  ({error[:80]})")
                if throttled:
                    streak += 1
                    self._penalize(self._backoff_seconds(streak))
                    if streak >= STOP_AFTER_THROTTLED and not stop.is_set():
                        stop.set()
                        tqdm.write(f"  {Fore.YELLOW}Throttled {streak} times in a row - "
                                   f"stopping. The rest can be downloaded later.{Style.RESET_ALL}")
                else:
                    streak = 0
        except KeyboardInterrupt:
            stop.set()
            for future in futures:
                future.cancel()
            tqdm.write(f"  {Fore.YELLOW}Stopping after the downloads in progress...{Style.RESET_ALL}")
            raise
        finally:
            pool.shutdown(wait=True)
            bar.close()
        return results

    def _run_collection(self, url: str, metadata: dict, kind: str, max_workers: int = 3) -> bool:
        """Download an album/playlist/artist's songs in parallel (see output_template)."""
        name = metadata.get("title") or kind.title()
        template = self.output_template(kind, name)
        tasks = []
        for entry in metadata.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            label = f"{entry.get('artist')} - {entry.get('title')}" if entry.get("artist") \
                else entry.get("title") or entry["id"]
            tasks.append({"id": entry["id"], "url": entry.get("url") or TRACK_URL.format(entry["id"]),
                          "title": entry.get("title") or entry["id"], "artist": entry.get("artist"),
                          "label": label, "template": template})
        archive = self._archive_path(url)
        already = len(self._read_archive(archive) & {t["id"] for t in tasks})
        if already:
            Enhanced_Menu.print_status(f"{already} song(s) already downloaded earlier.", "info")
        if len(tasks) == already:
            Enhanced_Menu.print_status(f"Nothing new to download in this {kind}.", "success")
            return True

        folder = template.split("{")[0].rstrip("/\\") or str(self.__output_directory)
        Enhanced_Menu.print_status(f"Downloading {len(tasks) - already} song(s) into {folder}, "
                                   f"{max_workers} at a time...", "info")
        results: Dict[str, bool] = {}
        try:
            results = self.parallel_run_download(tasks, archive, max_workers,
                                                 desc=safe_name(name)[:30], source="")
        except KeyboardInterrupt:
            print()
            Enhanced_Menu.print_status("Interrupted. Run it again later and finished songs "
                                       "will be skipped.", "warning")
        succeeded = sum(results.values())
        failed = len(results) - succeeded
        print()
        Enhanced_Menu.print_key_value("Downloaded", succeeded, width=12)
        if failed:
            Enhanced_Menu.print_key_value("Failed", failed, width=12,
                                          note="added to the retry queue")
        not_tried = len(tasks) - already - len(results)
        if not_tried:
            Enhanced_Menu.print_key_value("Not tried", not_tried, width=12,
                                          note=f"run the {kind} again later")
        if succeeded:
            cleanup_directory(self.__output_directory, self.log_manager)
        return failed == 0 and not not_tried

    def _run_playlist(self, url: str, metadata: dict, max_workers: int = 3) -> bool:
        """Older name: a playlist through _run_collection."""
        return self._run_collection(url, metadata, "playlist", max_workers)

    # ==================== Tracks, albums, playlists, artists ====================
    def download_item(self, item_type: str, url_prompt: str, output_template: str = None,
                      additional_args: list = None, confirm_large: bool = False,
                      use_archive: bool = False, concurrent: bool = False,
                      max_workers: int = 3) -> bool:
        """
        Unified download for tracks, albums, playlists and artists.

        Args:
            item_type (str): "track", "album", "playlist" or "artist"
            url_prompt (str): what to ask for ("track link", ...)
            output_template (str, optional): spotDL output template (tracks)
            additional_args (list, optional): extra spotDL arguments
            confirm_large (bool, optional): ask before collections of more than 50 songs
            use_archive (bool, optional): unused (collections always keep an archive)
            concurrent (bool, optional): unused (collections are always song by song)
            max_workers (int, optional): songs at once for collections

        Returns:
            bool: True if the last download succeeded
        """
        searchable = item_type == "track"
        while True:
            Enhanced_Menu.clear_screen()
            Enhanced_Menu.print_header(f"Download {item_type.title()}")

            hint = " or 'Artist - Title' to search" if searchable else ""
            url = Enhanced_Menu.get_input(
                f"Enter a Spotify {url_prompt}{hint} (or 'back' to return)", "str")
            url = (url or "").strip().strip('"').strip("'")
            if not url:
                Enhanced_Menu.print_status("Nothing entered", "error")
                Enhanced_Menu.pause()
                continue
            if url.lower() == 'back':
                return False

            if not is_spotify_url(url) and (not searchable or re.match(r"https?://", url)):
                Enhanced_Menu.print_status(
                    f"Not a Spotify {item_type} link. Copy it from Spotify: Share -> "
                    f"Copy link to {'song' if item_type == 'track' else item_type}", "error")
                Enhanced_Menu.pause()
                continue

            Enhanced_Menu.print_status("Reading it from Spotify...", "info")
            is_valid, message, metadata = self.fetch_info(url, item_type)
            if not is_valid or not metadata:
                Enhanced_Menu.print_status(f"Couldn't read it: {message}", "error")
                if not Enhanced_Menu.confirm("Try another one?", default=True):
                    return False
                continue

            query = self._query(url)
            self.history.add_input(query, item_type, metadata)

            # Display resource information
            Enhanced_Menu.print_status("Found:", "success")
            count = metadata.get("playlist_count") or len(metadata.get("entries") or [])
            if item_type == "track":
                print(f"  {Fore.CYAN}Track:{Style.RESET_ALL} {metadata.get('title', 'Unknown')}")
                print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {metadata.get('artist') or 'Unknown Artist'}")
                if metadata.get("album"):
                    print(f"  {Fore.CYAN}Album:{Style.RESET_ALL} {metadata['album']}")
                if not is_spotify_url(url):
                    print(f"  {Style.DIM}(best match for your search){Style.RESET_ALL}")
            else:
                label = {"album": "Album", "playlist": "Playlist", "artist": "Artist"}[item_type]
                print(f"  {Fore.CYAN}{label}:{Style.RESET_ALL} {metadata.get('title', 'Unknown')}")
                if item_type == "album" and metadata.get("artist"):
                    print(f"  {Fore.CYAN}Artist:{Style.RESET_ALL} {metadata['artist']}")
                print(f"  {Fore.CYAN}Songs:{Style.RESET_ALL} {count or '?'}")
            print()

            # Confirm large collections
            if confirm_large and (count or 0) > 50:
                Enhanced_Menu.print_status(
                    f"This {item_type} has {count} songs. This may take a while.", "warning")
                if not Enhanced_Menu.get_input("Continue with download? (y/n)", "yn", default=False):
                    Enhanced_Menu.print_status("Download cancelled", "info")
                    continue

            if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
                self.get_user_preferences()

            if item_type == "track":
                # Download the song that was shown, even if it came from a search.
                target = metadata.get("webpage_url") or query
                template = output_template or self.output_template("track")
                Enhanced_Menu.print_status("Starting download...", "info")
                success, error, throttled = self._download_with_retry(
                    target, template, list(additional_args or []), "track", metadata=metadata)
                if success:
                    Enhanced_Menu.print_status(f"Downloaded to {self.__output_directory}", "success")
                else:
                    self.log_manager.record_failure(target, metadata.get('title', ''), error, "",
                                                    throttled=throttled, item_type="track",
                                                    metadata=metadata)
                    Enhanced_Menu.print_status(
                        "Added to the retry queue: pick it up later from the Downloads menu"
                        + (" (throttled - wait a while first)." if throttled else "."),
                        "warning")
            else:
                success = self._run_collection(query, metadata, item_type,
                                               max_workers or self.max_concurrent or 2)

            # ---------------- Post-download prompt (shared) ----------------
            if success:
                if Enhanced_Menu.get_input(f"\nDownload another {item_type}? (y/n): ", "yn", default=True):
                    continue
                return True
            if Enhanced_Menu.get_input(f"\nDownload failed. Try another {item_type}? (y/n): ", "yn", default=True):
                continue
            return False

    # Older name for the same thing.
    _download_item = download_item

    def download_track(self):
        """Download a single song (a link or a search)."""
        return self.download_item(item_type="track", url_prompt="song link")

    def download_album(self):
        """Download an album, song by song."""
        return self.download_item(item_type="album", url_prompt="album link",
                                  confirm_large=True, max_workers=self.max_concurrent or 2)

    def download_playlist(self):
        """Download a playlist, several songs at once."""
        return self.download_item(item_type="playlist", url_prompt="playlist link",
                                  confirm_large=True, max_workers=self.max_concurrent or 2)

    def download_artist(self):
        """Download every song on an artist's albums and singles."""
        return self.download_item(item_type="artist", url_prompt="artist link",
                                  confirm_large=True, max_workers=self.max_concurrent or 2)

    # ==================== Batch files ====================
    def download_from_file(self, file_path: str = None) -> bool:
        """
        Download every Spotify link in a .txt, .csv or .json file, strictly one at a time.

        Sequential by design: a batch file is usually long, and firing several
        requests at once at the same hosts is what gets you throttled. Every
        success is written back into the source file, so a re-run picks up where
        the last one stopped; every failure lands in the retry queue with its
        error and attempt count. Album/playlist/artist links in the file are
        downloaded whole, with an archive so a re-run skips finished songs.
        """
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Batch Download", "Download every Spotify link in a .txt, .csv or .json file")

        if file_path is None:
            raw = Enhanced_Menu.get_input(
                "Path to the .txt, .csv or .json file (or 'back' to return)", "str")
            raw = (raw or "").strip().strip('"').strip("'")
            if not raw or raw.lower() == "back":
                return False
            file_path = raw

        path = Path(file_path).expanduser()
        if not path.is_file():
            Enhanced_Menu.print_status(f"No such file: {path}", "error")
            return False

        entries = self.batch_file.parse(path)
        if not entries:
            Enhanced_Menu.print_status(f"No links found in {path.name}", "error")
            return False

        # De-duplicate (keeping order), drop non-Spotify links, and skip
        # anything the file already records as downloaded.
        seen = set()
        valid: List[Dict[str, str]] = []
        invalid = already_done = 0
        for entry in entries:
            url = entry["url"]
            key = normalize_spotify_url(url)
            if key in seen:
                continue
            seen.add(key)
            if not is_spotify_url(url):
                invalid += 1
                continue
            if entry.get("status") == "success":
                already_done += 1
                continue
            valid.append(entry)
        duplicates = len(entries) - len(seen)

        Enhanced_Menu.print_status(f"Read {path.name}:", "success")
        print(f"  {Fore.CYAN}Links found:{Style.RESET_ALL} {len(entries)}")
        if duplicates:
            print(f"  {Fore.YELLOW}Duplicates skipped:{Style.RESET_ALL} {duplicates}")
        if invalid:
            print(f"  {Fore.YELLOW}Not Spotify links, skipped:{Style.RESET_ALL} {invalid}")
        if already_done:
            print(f"  {Fore.CYAN}Already marked success:{Style.RESET_ALL} {already_done}")
        print(f"  {Fore.GREEN}To download:{Style.RESET_ALL} {len(valid)}")
        print()

        if not valid:
            Enhanced_Menu.print_status("Nothing left to download.", "info")
            return True

        if not Enhanced_Menu.get_input(f"Download these {len(valid)} links? (y/n)",
                                       "yn", default=True):
            Enhanced_Menu.print_status("Cancelled", "info")
            return False

        self.history.add_input(str(path), "batch")

        # Output folder: named after the file by default, so a batch stays together.
        folder_name = safe_name(path.stem, "Batch")
        use_folder = Enhanced_Menu.get_input(f"Save into a subfolder named '{folder_name}'? (y/n)",
                                             "yn", default=True)
        output_template = self.output_template("batch", folder_name if use_folder else "")

        if Enhanced_Menu.get_input("Configure download settings? (y/n)", "yn", default=False):
            self.get_user_preferences()
            output_template = self.output_template("batch", folder_name if use_folder else "")

        total = len(valid)
        succeeded = failed = 0
        pending: Dict[str, str] = {}          # url -> status, not yet flushed to file
        cleared: List[str] = []               # urls to remove from the retry queue
        failures: List[Tuple[str, str, str]] = []
        interrupted = throttled_out = False
        rate_limit_streak = 0
        started = time.monotonic()

        Enhanced_Menu.print_status(f"Starting batch download of {total} links...", "info")
        print()

        try:
            for index, entry in enumerate(valid, 1):
                url, title = entry["url"], entry.get("title", "")
                kind = spotify_kind(url) or "track"
                label = title or url
                print(f"{Fore.CYAN}[{index}/{total}]{Style.RESET_ALL} {str(label)[:65]}"
                      + (f" {Style.DIM}({kind}){Style.RESET_ALL}" if kind != "track" else ""))

                extra = [] if kind == "track" else ["--archive", str(self._archive_path(url))]
                ok, error, throttled = self._download_with_retry(
                    url, output_template, extra, item_type=kind, show_progress=False,
                    metadata={"title": title} if title else None,
                    threads=1 if kind == "track" else max(1, self.max_concurrent or 1))

                if ok:
                    succeeded += 1
                    pending[url] = "success"
                    cleared.append(url)
                    rate_limit_streak = 0
                    print(f"      {Fore.GREEN}done{Style.RESET_ALL}")
                else:
                    failed += 1
                    pending[url] = "failed"
                    failures.append((url, title, error))
                    self.log_manager.record_failure(url, title, error, str(path),
                                                    throttled=throttled, item_type=kind)
                    if throttled:
                        rate_limit_streak += 1
                        print(f"      {Fore.RED}throttled -> retry queue{Style.RESET_ALL}")
                    else:
                        rate_limit_streak = 0
                        print(f"      {Fore.RED}failed: {error[:70]} -> retry queue{Style.RESET_ALL}")

                # Written per link rather than in batches: the file should say
                # what the screen just said, and an interrupted run shouldn't
                # lose the last few results.
                if self.batch_file.mark_statuses(path, pending):
                    pending = {}

                # Once Spotify or YouTube starts refusing, every remaining link
                # fails the same way. Stop instead: the markers already written
                # make the re-run pick up here.
                if rate_limit_streak >= STOP_AFTER_THROTTLED:
                    throttled_out = True
                    Enhanced_Menu.print_status(
                        "Three throttled links in a row - stopping here. Wait a while, "
                        "then re-run this file; finished links will be skipped.", "warning")
                    break

                if index < total:
                    if rate_limit_streak:
                        wait = self._backoff_seconds(rate_limit_streak)
                        Enhanced_Menu.print_status(
                            f"Throttled - waiting {wait:.0f}s before the next link", "warning")
                        time.sleep(wait)      # Ctrl-C is caught by the handler below
                    else:
                        # yt-dlp's own --sleep-interval only applies within a
                        # single spotDL run, not between them.
                        time.sleep(random.uniform(self.yt_dlp_sleep_min, self.yt_dlp_sleep_max))

        except KeyboardInterrupt:
            interrupted = True
            print()
            Enhanced_Menu.print_status(
                "Interrupted. Re-run this file later and finished links will be skipped.",
                "warning")

        # Final flush of statuses, and drop anything that succeeded from the queue.
        self.batch_file.mark_statuses(path, pending)
        self.log_manager.clear_failures(cleared)

        elapsed = time.monotonic() - started
        stopped = interrupted or throttled_out
        print()
        Enhanced_Menu.print_header("Batch Download Complete" if not stopped
                                   else "Batch Download Stopped")
        print(f"  {Fore.GREEN}Succeeded:{Style.RESET_ALL} {succeeded}")
        if failed:
            print(f"  {Fore.RED}Failed:{Style.RESET_ALL} {failed}")
            for url, title, _ in failures[:10]:
                print(f"      {Fore.RED}- {str(title or url)[:60]}{Style.RESET_ALL}")
            if len(failures) > 10:
                print(f"      {Fore.RED}...and {len(failures) - 10} more{Style.RESET_ALL}")
            print(f"  {Fore.YELLOW}Queued for retry in:{Style.RESET_ALL} {self.log_manager.FAILED_FILE}")
        if stopped:
            print(f"  {Fore.YELLOW}Not attempted:{Style.RESET_ALL} {total - succeeded - failed}")
        print(f"  {Fore.CYAN}Statuses written to:{Style.RESET_ALL} {path}")
        print(f"  {Fore.CYAN}Elapsed:{Style.RESET_ALL} {elapsed / 60:.1f} min")

        if succeeded:
            cleanup_directory(self.__output_directory, self.log_manager)

        return failed == 0 and not stopped

    # ==================== Retry queue ====================
    def download_from_retry_queue(self) -> bool:
        """Re-attempt every link in the retry queue (see managers.retry_manager)."""
        from managers import retry_manager
        if not retry_manager.is_configured():
            retry_manager.configure(self, self.log_manager, self.batch_file, rate_limiter)
        Enhanced_Menu.clear_screen()
        Enhanced_Menu.print_header("Retry Queue", "Re-attempt previously failed songs")
        summary = retry_manager.run(
            confirm=lambda question: Enhanced_Menu.get_input(question, "yn", default=True))
        if summary.recovered:
            cleanup_directory(self.__output_directory, self.log_manager)
        return summary.ok


# The YouTube version's class name, so code written for it keeps working.
YoutubeMusicDownloader = SpotifyDownloader
