"""Settings: the downloader's settings (shared with the terminal program) plus appearance.

Download settings are read from and saved to the project's config through
managers.config_manager, with the same checks the Config Menu uses. Appearance
(theme, accent, font) is GUI-only and goes to config/gui_settings.json.
"""

import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QLineEdit, QProgressBar, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

from gui.dialogs import DependencyDialog, messages
from gui.dialogs.cookie_dialog import (HOW_TO, active_cookie_file, choose_cookie_file,
                                       forget_cookie_file)
from gui.resources import make_icon
from gui.styles import ACCENTS, PALETTES
from gui.views.widgets import Card, button, label
from gui.workers import FFmpegInstallWorker, SpotdlUpdateWorker
from managers import config_manager

# (config key, label, tooltip) for the number settings shown on this page.
NUMBERS = (
    ("max_concurrent", "Downloads at once",
     "Songs downloaded at the same time for albums, playlists and artists. "
     "1 is gentlest on Spotify and YouTube Music."),
    ("max_retries", "Attempts per song", "How many times a failing song is tried."),
    ("retry_delay", "Seconds between attempts", "Wait before trying a failed song again."),
    ("yt_dlp_sleep_min", "Pause between requests (min s)",
     "spotDL (through yt-dlp) waits a random time between these two values before each "
     "YouTube Music request. Raise them if you're throttled."),
    ("yt_dlp_sleep_max", "Pause between requests (max s)", ""),
)
COMMON_FONTS = ["Segoe UI", "Arial", "Roboto", "Inter", "Google Sans"]


class SettingsView(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.spotdl_worker = None
        self.ffmpeg_worker = None
        self.spotdl_status = None

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        scroll.setWidget(host)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(host)
        outer.setContentsMargins(40, 36, 40, 30)
        outer.setSpacing(18)

        head = QHBoxLayout()
        head.addWidget(label("Settings", "H1"))
        head.addStretch(1)
        self.open_cfg = button("  Open config folder", self.open_config_folder)
        self.reset_btn = button("  Reset to default", self.reset)
        self.save_btn = button("  Save changes", self.save, "accent")
        for widget in (self.open_cfg, self.reset_btn, self.save_btn):
            head.addWidget(widget)
        outer.addLayout(head)

        # ---------- Dependencies ----------
        deps, deps_layout = self._section("Dependencies",
                                          "The programs that do the downloading and converting.")
        self.deps_btn = button("  Full check", self.check_dependencies,
                               tooltip="Programs and Python packages, with how to fix anything missing")
        deps.header.addWidget(self.deps_btn)

        (self.spotdl_dot, self.spotdl_state, self.spotdl_action, self.spotdl_progress,
         self.spotdl_edit, self.spotdl_browse) = self._program_block(
            deps_layout, "spotDL", "Reads Spotify, finds each song on YouTube Music and downloads "
            "it. Update it (and yt-dlp with it) when downloads start failing.",
            "  Check for updates", self.spotdl_clicked)
        divider = QFrame()
        divider.setObjectName("Divider")
        deps_layout.addWidget(divider)
        (self.ffmpeg_dot, self.ffmpeg_state, self.ffmpeg_action, self.ffmpeg_progress,
         self.ffmpeg_edit, self.ffmpeg_browse) = self._program_block(
            deps_layout, "FFmpeg", "Converts the audio and embeds cover art.",
            "  Install FFmpeg", self.install_ffmpeg)
        outer.addWidget(deps)

        # ---------- Output + Cookies, side by side ----------
        row = QHBoxLayout()
        row.setSpacing(18)
        output, out_layout = self._section("Output", "Where downloads go and what they become.")
        self.path_edit = QLineEdit()
        out_layout.addWidget(label("Download folder", "H3"))
        out_layout.addWidget(self._with_browse(self.path_edit, self.pick_dir, "browse", "accent"))
        self.fmt = QComboBox()
        self.fmt.addItems(list(config_manager.VALID_FORMATS))
        self.fmt.setToolTip("mp3 plays everywhere; opus/m4a keep YouTube Music's audio with the "
                            "smallest files; flac/wav are lossless containers.")
        self.bitrate = QComboBox()
        self.bitrate.addItems(list(config_manager.VALID_QUALITIES))
        self.bitrate.setToolTip("auto keeps the source quality. Ignored for flac and wav.")
        pair = QHBoxLayout()
        pair.addLayout(self._field("Audio format", self.fmt))
        pair.addLayout(self._field("Bitrate", self.bitrate))
        out_layout.addLayout(pair)
        out_layout.addStretch(1)
        row.addWidget(output, 1)

        cookies, ck_layout = self._section(
            "Cookies", "YouTube Music cookies (where spotDL gets the audio): help with "
            "age-restricted songs, Premium quality and 'confirm you're not a bot'.")
        self.use_cookies = QComboBox()
        self.use_cookies.addItems(["No", "Yes"])
        ck_layout.addLayout(self._field("Use cookies", self.use_cookies))
        ck_layout.addWidget(label("Cookie file (cookies.txt)", "H3"))
        cookie_row = QHBoxLayout()
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setReadOnly(True)
        self.cookie_edit.setPlaceholderText("no cookie file chosen")
        self.cookie_edit.setToolTip(HOW_TO)
        self.cookie_browse = button("  Choose…", self.pick_cookie)
        self.cookie_remove = button("Remove", self.remove_cookie)
        cookie_row.addWidget(self.cookie_edit, 1)
        cookie_row.addWidget(self.cookie_browse)
        cookie_row.addWidget(self.cookie_remove)
        ck_layout.addLayout(cookie_row)
        ck_layout.addStretch(1)
        row.addWidget(cookies, 1)
        outer.addLayout(row)

        # ---------- Spotify login (optional) ----------
        spotify, sp_layout = self._section(
            "Spotify API (optional)",
            "spotDL reads Spotify with a shared built-in login, which is sometimes rate "
            "limited. Your own free app's Client ID and Secret avoid that: create one at "
            "developer.spotify.com/dashboard. Leave both blank to use the built-in login.")
        self.client_id = QLineEdit()
        self.client_id.setPlaceholderText("blank = spotDL's built-in login")
        self.client_secret = QLineEdit()
        self.client_secret.setEchoMode(QLineEdit.Password)
        self.client_secret.setPlaceholderText("blank = spotDL's built-in login")
        self.show_secret = button("Show", self.toggle_secret)
        self.dashboard_btn = button("  Open Spotify dashboard", lambda: QDesktopServices.openUrl(
            QUrl("https://developer.spotify.com/dashboard")))
        spotify.header.addWidget(self.dashboard_btn)
        pair = QHBoxLayout()
        pair.addLayout(self._field("Client ID", self.client_id), 1)
        secret_holder = QWidget()
        secret_row = QHBoxLayout(secret_holder)
        secret_row.setContentsMargins(0, 0, 0, 0)
        secret_row.addWidget(self.client_secret, 1)
        secret_row.addWidget(self.show_secret)
        pair.addLayout(self._field("Client Secret", secret_holder), 1)
        sp_layout.addLayout(pair)
        outer.addWidget(spotify)

        # ---------- Downloading + Appearance, side by side ----------
        row = QHBoxLayout()
        row.setSpacing(18)
        downloading, dl_layout = self._section(
            "Downloading", "Pacing and retries. Gentler settings mean less throttling.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        self.numbers = {}
        bounds = config_manager.INT_BOUNDS
        for index, (key, title, hint) in enumerate(NUMBERS):
            spin = QSpinBox()
            low, high = bounds.get(key, (0, 3600))
            spin.setRange(low, high)
            spin.setToolTip(hint)
            self.numbers[key] = spin
            r, c = divmod(index, 2)
            grid.addWidget(label(title, "H3"), r * 2, c)
            grid.addWidget(spin, r * 2 + 1, c)
        dl_layout.addLayout(grid)
        dl_layout.addStretch(1)
        row.addWidget(downloading, 1)

        appearance, ap_layout = self._section("Appearance", "Previews straight away; Save keeps it.")
        self.mode = QComboBox()
        self.mode.addItems(list(PALETTES))
        self.mode.currentTextChanged.connect(self.preview)
        self.accent = QComboBox()
        self.accent.addItems(list(ACCENTS))
        self.accent.currentTextChanged.connect(self.preview)
        pair = QHBoxLayout()
        pair.addLayout(self._field("Theme", self.mode))
        pair.addLayout(self._field("Accent", self.accent))
        ap_layout.addLayout(pair)
        self.font = QComboBox()
        families = sorted(set(QFontDatabase.families()))
        ordered = [f for f in COMMON_FONTS if f in families] + \
                  [f for f in families if f not in COMMON_FONTS]
        self.font.addItems(ordered or ["Segoe UI"])
        self.font.currentTextChanged.connect(self.preview)
        ap_layout.addLayout(self._field("Font", self.font))
        ap_layout.addStretch(1)
        row.addWidget(appearance, 1)
        outer.addLayout(row)

        self.note = label("Download settings are shared with the terminal program "
                          f"({config_manager.config_path()}), so a change here applies there "
                          "too.", "Faint", wrap=True)
        outer.addWidget(self.note)
        outer.addStretch(1)

        self.load()
        self.refresh_programs()

    # ==================== Layout helpers ====================
    def _section(self, title: str, subtitle: str):
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(10)
        card.header = QHBoxLayout()
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label(title, "SectionTitle"))
        texts.addWidget(label(subtitle, "Faint", wrap=True))
        card.header.addLayout(texts, 1)
        layout.addLayout(card.header)
        return card, layout

    @staticmethod
    def _field(title: str, widget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(6)
        box.addWidget(label(title, "H3"))
        box.addWidget(widget)
        return box

    @staticmethod
    def _with_browse(edit: QLineEdit, on_browse, name: str, variant: str = "") -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        btn = button("  Browse", on_browse, variant)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        holder.browse_button = btn
        return holder

    def _program_block(self, layout, name, description, action_text, on_action):
        top = QHBoxLayout()
        dot = label("●", "Dot")
        dot.setFixedWidth(18)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(label(name, "H3"))
        state = label("Checking…", "Sub", wrap=True)
        texts.addWidget(state)
        top.addWidget(dot)
        top.addLayout(texts, 1)
        action = button(action_text, on_action)
        top.addWidget(action)
        layout.addLayout(top)
        layout.addWidget(label(description, "Faint"))
        progress = QProgressBar()
        progress.setVisible(False)
        layout.addWidget(progress)
        edit = QLineEdit()
        edit.setPlaceholderText("Location: blank = find it automatically")
        holder = self._with_browse(edit, lambda: self.pick_program(edit), "browse")
        layout.addWidget(holder)
        return dot, state, action, progress, edit, holder.browse_button

    # ==================== Theme ====================
    def refresh_icons(self):
        theme = self.win.theme
        dim = theme.c["text_dim"]
        self.deps_btn.setIcon(make_icon("check", dim, 16))
        self.open_cfg.setIcon(make_icon("folder", dim, 16))
        self.reset_btn.setIcon(make_icon("reset", dim, 16))
        self.save_btn.setIcon(make_icon("save", theme.on_accent, 16))
        self.cookie_browse.setIcon(make_icon("cookie", dim, 16))
        self.dashboard_btn.setIcon(make_icon("link", dim, 16))
        self.spotdl_action.setIcon(make_icon("retry", dim, 16))
        self.ffmpeg_action.setIcon(make_icon("package", dim, 16))
        for btn in (self.spotdl_browse, self.ffmpeg_browse):
            btn.setIcon(make_icon("folder", dim, 16))
        self.path_edit.parentWidget().browse_button.setIcon(make_icon("folder", theme.on_accent, 16))

    # ==================== Dependencies ====================
    def _set_dot(self, dot, state: str):
        colour = {"ok": "#22c55e", "warn": "#f59e0b", "bad": "#ef4444"}.get(state, "#a1a1aa")
        dot.setStyleSheet(f"color: {colour};")

    def refresh_programs(self):
        """Quick local check (no internet): is each program there, and which version?"""
        from tools.dependency_check import program_version
        version, detail = program_version("spotdl")
        if version:
            self._set_dot(self.spotdl_dot, "ok")
            self.spotdl_state.setText(f"Version {version}  ·  {detail}")
        else:
            self._set_dot(self.spotdl_dot, "bad")
            self.spotdl_state.setText(f"Not found ({detail}). Install it with: pip install -U spotdl")
        self.spotdl_action.setText("  Check for updates")
        version, detail = program_version("ffmpeg", "-version")
        if version:
            self._set_dot(self.ffmpeg_dot, "ok")
            self.ffmpeg_state.setText(f"Version {version}  ·  {detail}")
            self.ffmpeg_action.setText("  Reinstall FFmpeg")
        else:
            self._set_dot(self.ffmpeg_dot, "bad")
            self.ffmpeg_state.setText(f"Not found ({detail}).")
            self.ffmpeg_action.setText("  Install FFmpeg")
        self.ffmpeg_action.setVisible(sys.platform == "win32" or not version)
        self.win.update_status_bar(recheck_spotdl=True)

    def spotdl_clicked(self):
        """First click checks online; if there's an update, the button becomes 'Update now'."""
        if self.spotdl_worker and self.spotdl_worker.isRunning():
            return
        status = self.spotdl_status
        if status is not None and status.outdated and status.download_url:
            if messages.confirm(self, "spotDL", "This spotDL is a standalone program and can't "
                                "update itself.\n\nOpen the download page for the newest one?"):
                QDesktopServices.openUrl(QUrl(status.download_url))
            return
        if status is not None and (status.outdated or not status.installed) and status.command:
            what = "Install" if not status.installed else "Update"
            if not messages.confirm(self, "spotDL", f"{what} spotDL now?\n\nThis runs:\n"
                                    + " ".join(status.command)):
                return
            self._busy(self.spotdl_action, self.spotdl_progress, f"  {what.rstrip('e')}ing…")
            self.spotdl_worker = SpotdlUpdateWorker("update", status)
            self.spotdl_worker.finished_update.connect(self._spotdl_updated)
        else:
            self._busy(self.spotdl_action, self.spotdl_progress, "  Checking…")
            self.spotdl_worker = SpotdlUpdateWorker("check")
            self.spotdl_worker.checked.connect(self._spotdl_checked)
        self.spotdl_worker.start()

    def _busy(self, btn, progress, text):
        btn.setEnabled(False)
        btn.setText(text)
        progress.setRange(0, 0)                 # moving bar: we can't measure this one
        progress.setVisible(True)

    def _idle(self, btn, progress):
        btn.setEnabled(True)
        progress.setVisible(False)
        progress.setRange(0, 100)

    def _spotdl_checked(self, status):
        self._idle(self.spotdl_action, self.spotdl_progress)
        self.spotdl_status = status
        if not status.installed:
            self._set_dot(self.spotdl_dot, "bad")
            self.spotdl_state.setText(status.error or "spotDL isn't installed.")
            self.spotdl_action.setText("  Install spotDL" if status.command else "  Check again")
        elif status.outdated:
            self._set_dot(self.spotdl_dot, "warn")
            self.spotdl_state.setText(f"Update available: {status.installed} → {status.latest}"
                                     f"  ·  installed with {status.method}")
            self.spotdl_action.setText("  Update now" if status.command else
                                       "  Download page" if status.download_url else "  Check again")
            if not status.command and not status.download_url:
                self.spotdl_state.setText(self.spotdl_state.text() + "\nUpdate it the way you "
                                         "installed it (the app can't tell how).")
        elif status.latest:
            self._set_dot(self.spotdl_dot, "ok")
            self.spotdl_state.setText(f"Up to date ({status.installed})  ·  {status.location}")
            self.spotdl_action.setText("  Check for updates")
        else:
            self._set_dot(self.spotdl_dot, "warn")
            self.spotdl_state.setText(f"Version {status.installed}. {status.error}")
            self.spotdl_action.setText("  Check again")
        self.win.log(f"[spotDL] {self.spotdl_state.text()}")

    def _spotdl_updated(self, ok, message):
        self._idle(self.spotdl_action, self.spotdl_progress)
        self.spotdl_status = None
        self.win.log(f"[spotDL] {message}")
        (messages.info if ok else messages.error)(self, "spotDL", message)
        self.refresh_programs()

    def install_ffmpeg(self):
        if self.ffmpeg_worker and self.ffmpeg_worker.isRunning():
            return
        if sys.platform == "win32" and not messages.confirm(
                self, "FFmpeg", "Download FFmpeg (about 100 MB) into the program's bin folder?"):
            return
        self.ffmpeg_action.setEnabled(False)
        self.ffmpeg_progress.setRange(0, 100)
        self.ffmpeg_progress.setValue(0)
        self.ffmpeg_progress.setVisible(True)
        self.ffmpeg_worker = FFmpegInstallWorker()
        self.ffmpeg_worker.progress.connect(self._ffmpeg_progress)
        self.ffmpeg_worker.finished_install.connect(self._ffmpeg_done)
        self.ffmpeg_worker.start()

    def _ffmpeg_progress(self, percent, text):
        self.ffmpeg_progress.setValue(percent)
        self.ffmpeg_state.setText(text)

    def _ffmpeg_done(self, ok, message):
        self._idle(self.ffmpeg_action, self.ffmpeg_progress)
        self.win.log(f"[ffmpeg] {message}")
        (messages.info if ok else messages.warning)(self, "FFmpeg", message)
        if ok:
            self.ffmpeg_edit.setText(config_manager.load().get("ffmpeg_path") or "")
            self.win.downloader.load_settings()
        self.refresh_programs()

    def shutdown(self):
        for worker in (self.spotdl_worker, self.ffmpeg_worker):
            if worker and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(15000)

    # ==================== Load ====================
    def load(self):
        """Fill the fields from the saved settings."""
        cfg = config_manager.load()
        self.path_edit.setText(str(config_manager.resolve_path(cfg["output_directory"])))
        self.fmt.setCurrentText(cfg["audio_format"])
        self.bitrate.setCurrentText(cfg["audio_quality"])
        self.spotdl_edit.setText(cfg.get("spotdl_path") or "")
        self.client_id.setText(cfg.get("spotify_client_id") or "")
        self.client_secret.setText(cfg.get("spotify_client_secret") or "")
        self.ffmpeg_edit.setText(cfg.get("ffmpeg_path") or "")
        self.use_cookies.setCurrentText("Yes" if cfg.get("use_cookies") else "No")
        for key, spin in self.numbers.items():
            spin.setValue(int(cfg.get(key, spin.minimum())))
        self._show_cookie()
        gui = self.win.gui_settings
        for combo, key in ((self.mode, "theme_mode"), (self.accent, "accent"), (self.font, "font")):
            combo.blockSignals(True)
            if combo.findText(gui[key]) < 0 and combo is self.font:
                combo.insertItem(0, gui[key])        # not installed here: keep it, don't swap it
            if combo.findText(gui[key]) >= 0:
                combo.setCurrentText(gui[key])
            combo.blockSignals(False)

    def _show_cookie(self):
        active = active_cookie_file()
        self.cookie_edit.setText(str(active) if active else "")
        self.cookie_remove.setEnabled(bool(active))

    def toggle_secret(self):
        hidden = self.client_secret.echoMode() == QLineEdit.Password
        self.client_secret.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)
        self.show_secret.setText("Hide" if hidden else "Show")

    # ==================== Pickers ====================
    def pick_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose the download folder",
                                                  self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)

    def pick_program(self, edit: QLineEdit):
        name, _ = QFileDialog.getOpenFileName(self, "Choose the program", edit.text())
        if name:
            edit.setText(name)

    def pick_cookie(self):
        path = choose_cookie_file(self)
        if path:
            self._show_cookie()
            self.win.log(f"[cookies] using {path}")

    def remove_cookie(self):
        if messages.confirm(self, "Cookies", "Stop using this cookie file?\n"
                            "(The file itself isn't deleted.)", default_yes=False):
            forget_cookie_file()
            self._show_cookie()
            self.win.log("[cookies] cookie file removed")

    def open_config_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(config_manager.config_path()).parent)))

    def check_dependencies(self):
        DependencyDialog(self, self.win.theme).exec()

    # ==================== Appearance ====================
    def preview(self):
        theme = self.win.theme
        theme.mode = self.mode.currentText()
        theme.accent_name = self.accent.currentText()
        theme.font = self.font.currentText()
        self.win.apply_theme()

    # ==================== Save / reset ====================
    def _collected(self) -> dict:
        values = {
            "output_directory": self.path_edit.text().strip(),
            "audio_format": self.fmt.currentText(),
            "audio_quality": self.bitrate.currentText(),
            "spotdl_path": self.spotdl_edit.text().strip(),
            "spotify_client_id": self.client_id.text().strip(),
            "spotify_client_secret": self.client_secret.text().strip(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip(),
            "use_cookies": self.use_cookies.currentText() == "Yes",
        }
        values.update({key: spin.value() for key, spin in self.numbers.items()})
        return values

    def save(self):
        values = self._collected()
        problems, clean = [], {}
        for key, value in values.items():
            checked, problem = config_manager.check_value(key, value)
            if problem:
                problems.append(f"{config_manager.CONFIG_SCHEMA[key].get('label', key)}: {problem}")
            else:
                clean[key] = checked
        for key in ("spotdl_path", "ffmpeg_path"):
            if clean.get(key) and not config_manager.resolve_path(clean[key]).exists():
                problems.append(f"{config_manager.CONFIG_SCHEMA[key]['label']}: "
                                f"{clean[key]} doesn't exist (leave it blank to find it automatically)")
        if bool(clean.get("spotify_client_id")) != bool(clean.get("spotify_client_secret")):
            problems.append("Spotify API: fill in both the Client ID and the Client Secret, "
                            "or leave both blank.")
        if clean.get("yt_dlp_sleep_min", 0) > clean.get("yt_dlp_sleep_max", 0):
            problems.append("The minimum pause can't be longer than the maximum.")
        if problems:
            messages.error(self, "Settings not saved", "\n".join(problems))
            return

        folder = config_manager.resolve_path(clean["output_directory"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messages.error(self, "Settings not saved", f"Can't create {folder}:\n{error}")
            return

        # Cookies on but no file yet: ask for one now, like the terminal program does.
        if clean["use_cookies"] and not active_cookie_file():
            if messages.confirm(self, "Cookies", "Cookies are on, but no cookie file is chosen.\n\n"
                                + HOW_TO + "\n\nChoose your cookies.txt now?"):
                self.pick_cookie()

        config_manager.update(**clean)
        self.win.downloader.load_settings()
        self.refresh_programs()                  # a new spotDL/ffmpeg location may change them

        gui = self.win.gui_settings
        gui["theme_mode"] = self.mode.currentText()
        gui["accent"] = self.accent.currentText()
        gui["font"] = self.font.currentText()
        gui.save()

        self.win.log("[settings] saved")
        self.win.settings_changed()
        self.save_btn.setText("  Saved!")
        QTimer.singleShot(1400, lambda: self.save_btn.setText("  Save changes"))

    def reset(self):
        if not messages.confirm(self, "Reset settings",
                                "Put every download setting and the appearance back to the "
                                "defaults?\n(Your cookie file is kept.)", default_yes=False):
            return
        config_manager.reset(keep_extras=True)
        self.win.downloader.load_settings()
        self.win.gui_settings.reset()
        self.win.gui_settings.save()
        self.load()
        self.preview()
        self.win.settings_changed()
        self.win.log("[settings] reset to defaults")
