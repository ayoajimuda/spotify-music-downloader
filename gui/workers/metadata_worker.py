"""Read a link's details (title, cover, track list) without downloading anything.

Uses the downloader's own fetch_info() (spotdl save), so it follows the same
Settings as the terminal program: the spotDL location and your own Spotify
Client ID/Secret, if set.
"""

from PySide6.QtCore import QThread, Signal

from downloader.base_downloader import is_spotify_url, normalize_spotify_url, spotify_kind
from managers import log_manager


# ==================== Link helpers ====================
def url_kind(url: str) -> str:
    """'track', 'album', 'playlist' or 'artist', from the link alone (a search is a track)."""
    return spotify_kind(url) or "track"


def fmt_duration(seconds) -> str:
    try:
        minutes, secs = divmod(int(float(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    except (TypeError, ValueError):
        return "--:--"


def best_thumb(obj) -> str:
    if not obj:
        return ""
    thumbs = obj.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        return thumbs[-1].get("url", "") or ""
    return obj.get("thumbnail", "") or ""


def _track(entry: dict, album: str = "") -> dict:
    return dict(id=entry.get("id") or "", name=entry.get("title") or entry.get("id") or "",
                artists=", ".join(entry.get("artists") or []) or entry.get("artist") or "",
                album=entry.get("album") or album, duration=fmt_duration(entry.get("duration")),
                cover_url=best_thumb(entry), url=entry.get("url") or "", available=True)


# ==================== Worker ====================
class MetadataWorker(QThread):
    """
    Emits done(meta) or error(message). meta is:
        {kind, title, subtitle, cover_url, count, unavailable, source_url, raw,
         tracks: [{id, name, artists, album, duration, cover_url, url, available}]}
    The url can also be a search ("Artist - Title"): the best match is shown.
    """
    done = Signal(dict)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, downloader, url: str):
        super().__init__()
        self.downloader = downloader
        raw = url.strip().strip('"').strip("'")
        self.url = normalize_spotify_url(raw) if is_spotify_url(raw) else raw

    def run(self):
        kind = url_kind(self.url)
        self.log.emit(f"[fetch] {self.url} ({kind if is_spotify_url(self.url) else 'search'})")
        try:
            ok, message, info = self.downloader.fetch_info(self.url, kind)
        except RuntimeError as error:                 # spotDL isn't installed
            self.error.emit(str(error))
            return
        except Exception as error:
            self.error.emit(f"Couldn't read that link: {type(error).__name__}: {error}")
            return
        if not ok or not info:
            self.error.emit(message or "Couldn't read that link.")
            return

        try:
            log_manager.add_input(self.url, kind, info)
        except Exception:
            pass                                      # history is a convenience

        if info.get("_type") == "playlist":
            album = info.get("title", "") if kind == "album" else ""
            tracks = [_track(e, album) for e in info.get("entries") or [] if e and e.get("id")]
            if not tracks:
                self.error.emit("No songs found at that link.")
                return
            title = info.get("title") or kind.title()
            subtitle = info.get("artist") or ""
            cover = best_thumb(info) or tracks[0]["cover_url"]
            source = info.get("webpage_url") or self.url
        else:                                         # a single song (or a search's match)
            track = _track(info["entries"][0] if info.get("entries") else
                           {"id": info.get("id"), "title": info.get("title"),
                            "artist": info.get("artist"), "album": info.get("album"),
                            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
                            "url": info.get("webpage_url")})
            title = track["name"] or "Track"
            subtitle = track["artists"]
            cover = track["cover_url"]
            tracks = [track]
            source = track["url"] or self.url

        self.log.emit(f"[fetch] {title}: {len(tracks)} song(s)")
        self.done.emit(dict(kind=kind, title=title, subtitle=subtitle, cover_url=cover,
                            count=len(tracks), unavailable=0, tracks=tracks,
                            source_url=source, raw=info))
