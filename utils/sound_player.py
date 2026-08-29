from __future__ import annotations

import time
from pathlib import Path

from aqt.qt import QUrl

from .logger import log

try:
    from PyQt6.QtMultimedia import QSoundEffect
except Exception:
    QSoundEffect = None  # type: ignore[assignment,misc]


class SoundPlayer:
    def __init__(self, default_sound_path: Path, config: dict) -> None:
        self._default_path = default_sound_path
        self._config = config
        self._last_played_at = 0.0
        self._current_path: Path | None = None
        self._effect = QSoundEffect() if QSoundEffect is not None else None
        self.update_config(config)

    def _effective_path(self) -> Path:
        """Prefer the user's own custom .wav (Settings -> Toolbar & Sound
        -> "Custom sound") over the bundled default — but only if it's
        actually there. A path that's been moved, deleted, or was typed in
        wrong falls back to the bundled ding rather than silently going
        mute, since a missing "brief chime at the end of a session" is
        easy to not notice until it matters."""
        custom = str(self._config.get("sound", {}).get("custom_path", "") or "").strip()
        if custom:
            p = Path(custom)
            if p.is_file():
                return p
            log.warning("SoundPlayer: custom_path %r not found, using default", custom)
        return self._default_path

    def update_config(self, config: dict) -> None:
        self._config = config
        if self._effect is None:
            return
        self._effect.setVolume(float(config.get("sound", {}).get("volume", 0.55)))
        path = self._effective_path()
        # Only touch setSource when the resolved file actually changed —
        # QSoundEffect re-decodes on every setSource call, and update_config
        # runs on every settings save/profile switch, not just ones that
        # touch the sound section.
        if path != self._current_path:
            self._effect.setSource(QUrl.fromLocalFile(str(path)))
            self._effect.setLoopCount(1)
            self._current_path = path

    def play(self) -> None:
        if not self._config.get("sound", {}).get("enabled", True):
            return
        if self._effect is None:
            return
        now = time.monotonic()
        if now - self._last_played_at < 0.35:
            return
        self._last_played_at = now
        self._effect.stop()
        self._effect.play()
