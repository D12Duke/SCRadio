"""
SC Lock Radio - single-file desktop app.
ASCII only. No Unicode anywhere.
"""

import atexit
import collections
import configparser
import ctypes
import logging
import os
import queue
import random
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, ttk

import mss
import numpy as np
import pygame
import keyboard
from PIL import Image
from scipy.ndimage import label as _cc_label
import cv2


_user32 = ctypes.windll.user32


def foreground_window_title():
    """Return the title of the currently foreground window, or '' on error."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


class _FsyncFileHandler(logging.FileHandler):
    """FileHandler that fsyncs after every emit so log survives process kill."""
    def emit(self, record):
        super().emit(record)
        try:
            if self.stream is not None:
                self.stream.flush()
                os.fsync(self.stream.fileno())
        except Exception:
            pass


def _setup_logger():
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sclr.log")
    lg = logging.getLogger("sclr")
    lg.setLevel(logging.DEBUG)
    for h in list(lg.handlers):
        lg.removeHandler(h)
    # mode='a' preserves history across launches; we add a separator line
    fh = _FsyncFileHandler(log_path, mode="a", encoding="ascii")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%H:%M:%S"))
    lg.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    lg.addHandler(sh)
    return lg


LOG = _setup_logger()
# Log at module-import time so we capture earliest launch event,
# even if main() never reaches its first line.
LOG.info("================================================================")
LOG.info("SC Lock Radio module imported. pid=%d python=%s", os.getpid(), sys.version.split()[0])
LOG.info("Script=%s", os.path.abspath(__file__))


@atexit.register
def _log_exit():
    try:
        LOG.info("--- process exiting cleanly via atexit ---")
    except Exception:
        pass


# Center region size as fraction of primary monitor.
# 10% wide-ish region: large enough to contain the target-lock bracket when
# it drifts off dead-center, small enough that the upper-left ALERT warnings
# and side cockpit elements stay outside.
CENTER_REGION_FRAC_W = 0.10
CENTER_REGION_FRAC_H = 0.10

# Red-dominance + connected-component detection.
# Pixel-level: a pixel is "red" if R >= RED_R_MIN and dominates G/B by RED_DELTA.
# Cluster-level: lock indicator is a single connected red blob, roughly square,
# size in LOCK_MIN..LOCK_MAX pixels. Background HUD noise is many small blobs
# or one long thin line; both fail the shape gate.
# SC's lock-indicator red (used for both the L-brackets AND the target name
# text below the bracket) is a SPECIFIC saturated dark red. Sampled from user
# snapshots showing locked targets: roughly RGB(200, 35, 35).
# - Cockpit warning lights tend to be brighter pure red (R >= 240) -> reject
#   by upper-bounding R.
# - Orange-red HUD elements (decoy bars, threat lights) have G >= 70 -> reject
#   by tight G/B cap.
# - Dark cockpit panel glow is dimmer (R < 175) -> reject by lower-bound R.
LOCK_R_MIN = 175
LOCK_R_MAX = 215   # was 230; tightened to reject brighter cockpit highlights
LOCK_G_MAX = 50    # was 65; tightened to reject amber cockpit beam transitions
LOCK_B_MAX = 50    # was 65; matches the new G cap for a tighter red box
# legacy fallback fields still referenced by snapshot/debug logging
RED_R_MIN = LOCK_R_MIN
RED_DELTA = 60
LOCK_BLOB_MIN = 30
LOCK_BLOB_MAX = 6000
LOCK_ASPECT_MAX = 3.5

# Foreground-window gate: condition B is forced to false unless the foreground
# window title contains this substring. Eliminates false positives from red
# text in VS Code, browsers, Discord, IDE errors, etc.
SC_WINDOW_SUBSTR = "Star Citizen"

# Condition A is true if T was pressed within the last T_PRESS_WINDOW_SECONDS.
# Decoupled from scan_interval (which controls capture frequency) so the user
# can keep scans fast while having a long T-validity window.
T_PRESS_WINDOW_SECONDS = 20

# Template-matching: if template.png exists in the script directory, detection
# uses cv2.matchTemplate (normalized cross-correlation). Score = correlation
# * TEMPLATE_SCORE_SCALE so a slider value of N maps to "require correlation
# >= N/SCALE" -- with SCALE=200 the existing 10-200 slider expresses 5%-100%.
TEMPLATE_FILENAME = "template.png"
TEMPLATE_SCORE_SCALE = 200


def template_path():
    return os.path.join(script_dir(), TEMPLATE_FILENAME)

CONFIG_FILENAME = "config.ini"

# UI colors (Section 4)
BG = "#1a1a1a"
PANEL = "#2a2a2a"
TEXT = "#ffffff"
ACCENT = "#cc0000"
GREY = "#888888"
GREEN = "#33dd55"
YELLOW = "#dddd33"

# Status strings
ST_IDLE = "IDLE"
ST_SCAN = "SCANNING"
ST_LOCKED = "LOCKED"
ST_TIMEOUT = "TIMEOUT COUNTDOWN"


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(script_dir(), CONFIG_FILENAME)


class Config:
    def __init__(self):
        self.combat_folder = ""        # plays during lock engagement
        self.non_combat_folder = ""    # plays as ambient default
        self.scan_interval = 2
        self.lock_timeout = 5
        # Default threshold ~= one bracket leg (~15 red pixels). User snapshots
        # show four L-corners totaling 60-100 pixels, so even with one corner
        # partially off-screen we get a clean trigger.
        self.brightness_threshold = 15

    def load(self):
        cp = configparser.ConfigParser()
        path = config_path()
        if not os.path.exists(path):
            return
        try:
            cp.read(path)
            if cp.has_section("settings"):
                s = cp["settings"]
                # New fields:
                self.combat_folder = s.get("combat_folder", self.combat_folder)
                self.non_combat_folder = s.get("non_combat_folder",
                                               self.non_combat_folder)
                # Back-compat migration: old single 'music_folder' becomes
                # combat_folder if combat_folder wasn't explicitly set.
                if not self.combat_folder:
                    self.combat_folder = s.get("music_folder", "")
                self.scan_interval = int(s.get("scan_interval", str(self.scan_interval)))
                self.lock_timeout = int(s.get("lock_timeout", str(self.lock_timeout)))
                self.brightness_threshold = int(
                    s.get("brightness_threshold", str(self.brightness_threshold))
                )
        except Exception:
            pass
        self.clamp()

    def save(self):
        self.clamp()
        cp = configparser.ConfigParser()
        cp["settings"] = {
            "combat_folder": self.combat_folder,
            "non_combat_folder": self.non_combat_folder,
            "scan_interval": str(self.scan_interval),
            "lock_timeout": str(self.lock_timeout),
            "brightness_threshold": str(self.brightness_threshold),
        }
        try:
            with open(config_path(), "w", encoding="ascii") as f:
                cp.write(f)
        except Exception:
            pass

    def clamp(self):
        if self.scan_interval < 1:
            self.scan_interval = 1
        if self.scan_interval > 5:
            self.scan_interval = 5
        if self.lock_timeout < 5:
            self.lock_timeout = 5
        if self.lock_timeout > 60:
            self.lock_timeout = 60
        if self.brightness_threshold < 1:
            self.brightness_threshold = 1
        if self.brightness_threshold > 200:
            self.brightness_threshold = 200


class CrossfadePlayer:
    """Two-stream music player.

    - Two independent shuffled playlists: non-combat (ambient default) and
      combat (engaged-target soundtrack).
    - When START is pressed, non-combat begins playing on channel 0.
    - On engage_combat(): non-combat fades out over FADE_MS, combat fades in
      on channel 1 over FADE_MS (true crossfade, ~2s overlap).
    - On disengage_combat(): combat fades out, non-combat fades in (a fresh
      non-combat song -- we advance the queue rather than resume).
    - tick() auto-advances each queue when its current song finishes.
    - Each playlist reshuffles every time it's exhausted.
    """

    FADE_MS = 7000

    def __init__(self):
        # 16386 samples at 48kHz = ~341ms headroom. User reported 16k felt
        # best; bigger (32k) didn't help. Likely cause of remaining hiccups
        # is sample-rate mismatch -- Windows default is usually 48000 Hz, so
        # we match it here to eliminate the on-the-fly resampling step.
        try:
            pygame.mixer.pre_init(frequency=48000, size=-16, channels=2,
                                  buffer=16386)
        except Exception:
            pass
        pygame.mixer.init()
        if pygame.mixer.get_num_channels() < 4:
            pygame.mixer.set_num_channels(8)
        self.ch_nc = pygame.mixer.Channel(0)  # non-combat
        self.ch_co = pygame.mixer.Channel(1)  # combat
        # Re-entrant lock: _play_*_next runs with _lock held (caller=start/
        # engage/disengage/tick) and itself calls _top_up_preload_* which
        # also wants _lock. Without RLock this self-deadlocks the player.
        self._lock = threading.RLock()
        # Playlists + indices
        self.nc_tracks = []
        self.co_tracks = []
        self.nc_index = 0
        self.co_index = 0
        # Currently playing Sound objects (kept referenced so they aren't GC'd)
        self.nc_sound = None
        self.co_sound = None
        self.nc_name = ""
        self.co_name = ""
        # Manual-fade state. We do crossfades by ramping channel.set_volume()
        # 60 times across FADE_MS instead of using pygame's fade_ms argument,
        # because pygame's built-in fade can produce micro-glitches on some
        # audio drivers. ramp_id is incremented every time a new ramp starts
        # on a channel; in-flight ramps check their id before each step and
        # bail if superseded, so back-to-back engage/disengage doesn't fight.
        self._ramp_lock = threading.Lock()
        self._ramp_id_nc = 0
        self._ramp_id_co = 0

        # Preload pipeline: each queue has a dedicated worker thread that
        # processes a FIFO job queue of file paths, decodes them, and appends
        # the resulting Sound to a ready-deque. _play_*_next pops from the
        # ready-deque so transitions never block on decode.
        # PRELOAD_DEPTH=2 means we always try to keep the next 2 tracks
        # decoded and waiting, so a rapid C->N->C cycle still hits warm
        # preloads on both sides.
        self.PRELOAD_DEPTH = 2
        self._preload_lock = threading.Lock()
        self.preload_nc = collections.deque()  # (path, Sound)
        self.preload_co = collections.deque()
        self._jobs_nc = queue.Queue()
        self._jobs_co = queue.Queue()
        self._worker_stop = threading.Event()
        self._worker_nc = threading.Thread(target=self._preload_worker,
                                           args=(self._jobs_nc, self.preload_nc, "nc"),
                                           daemon=True, name="preload-nc-worker")
        self._worker_co = threading.Thread(target=self._preload_worker,
                                           args=(self._jobs_co, self.preload_co, "co"),
                                           daemon=True, name="preload-co-worker")
        self._worker_nc.start()
        self._worker_co.start()
        # State
        self.started = False
        self.in_combat = False

    # ---- folder loading ----
    def _load_folder(self, folder):
        if not folder or not os.path.isdir(folder):
            return []
        files = []
        for entry in os.listdir(folder):
            if entry.lower().endswith(".mp3"):
                files.append(os.path.join(folder, entry))
        random.shuffle(files)
        return files

    def load_non_combat_folder(self, folder):
        with self._lock:
            self.nc_tracks = self._load_folder(folder)
            self.nc_index = 0
        # Reset preload state for this category
        self._reset_preload_nc()
        n = self.non_combat_count()
        if n > 0:
            self._top_up_preload_nc()
        return n

    def load_combat_folder(self, folder):
        with self._lock:
            self.co_tracks = self._load_folder(folder)
            self.co_index = 0
        self._reset_preload_co()
        n = self.combat_count()
        if n > 0:
            self._top_up_preload_co()
        return n

    # ---- preload pipeline ----
    def _preload_worker(self, jobs, out_deque, tag):
        """Background thread. Pulls file paths from `jobs`, decodes, appends
        to `out_deque`. Maintains FIFO order, so the ready deque's left side
        is always the NEXT track to play."""
        while not self._worker_stop.is_set():
            try:
                path = jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if path is None:
                return
            try:
                sound = pygame.mixer.Sound(path)
            except Exception as ex:
                LOG.warning("%s preload failed %s: %s", tag,
                            os.path.basename(path), ex)
                continue
            with self._preload_lock:
                out_deque.append((path, sound))
            LOG.debug("%s preloaded: %s (ready depth=%d)",
                      tag, os.path.basename(path), len(out_deque))

    def _reset_preload_nc(self):
        with self._preload_lock:
            self.preload_nc.clear()
        # Drain pending jobs
        while True:
            try:
                self._jobs_nc.get_nowait()
            except queue.Empty:
                break

    def _reset_preload_co(self):
        with self._preload_lock:
            self.preload_co.clear()
        while True:
            try:
                self._jobs_co.get_nowait()
            except queue.Empty:
                break

    def _top_up_preload_nc(self):
        """Enqueue jobs so preload_nc reaches PRELOAD_DEPTH ready tracks.
        Counts what's ready + inflight (job queue size) so we don't double-fire."""
        with self._lock:
            tracks = list(self.nc_tracks)
            cur_idx = self.nc_index
        if not tracks:
            return
        with self._preload_lock:
            ready = len(self.preload_nc)
        inflight = self._jobs_nc.qsize()
        total = ready + inflight
        need = self.PRELOAD_DEPTH - total
        for i in range(need):
            offset = total + i
            target_idx = (cur_idx + offset) % len(tracks)
            self._jobs_nc.put(tracks[target_idx])

    def _top_up_preload_co(self):
        with self._lock:
            tracks = list(self.co_tracks)
            cur_idx = self.co_index
        if not tracks:
            return
        with self._preload_lock:
            ready = len(self.preload_co)
        inflight = self._jobs_co.qsize()
        total = ready + inflight
        need = self.PRELOAD_DEPTH - total
        for i in range(need):
            offset = total + i
            target_idx = (cur_idx + offset) % len(tracks)
            self._jobs_co.put(tracks[target_idx])

    def non_combat_count(self):
        with self._lock:
            return len(self.nc_tracks)

    def combat_count(self):
        with self._lock:
            return len(self.co_tracks)

    # ---- playback helpers (internal, assume self._lock held) ----
    def _play_nc_next(self, fade_in):
        """Start the next non-combat track. Pops from the ready deque (warm
        preload). Falls back to synchronous decode ONLY if both ready deque
        AND inflight job queue are empty -- rare, e.g. very first play."""
        if not self.nc_tracks:
            self.nc_sound = None
            self.nc_name = ""
            return
        with self._preload_lock:
            if self.preload_nc:
                path, sound = self.preload_nc.popleft()
            else:
                path = None
                sound = None
        if sound is None:
            path = self.nc_tracks[self.nc_index]
            try:
                sound = pygame.mixer.Sound(path)
                LOG.warning("nc sync-decoded (preload was empty): %s",
                            os.path.basename(path))
            except Exception as ex:
                LOG.warning("nc decode (sync) failed %s: %s", path, ex)
                self.nc_sound = None
                self.nc_name = ""
                return
        self.nc_sound = sound
        self.nc_name = os.path.splitext(os.path.basename(path))[0]
        fade_ms = self.FADE_MS if fade_in else 0
        try:
            self.ch_nc.play(self.nc_sound, fade_ms=fade_ms)
        except Exception as ex:
            LOG.warning("nc play failed: %s", ex)
            return
        LOG.info("Non-combat playing: %s (fade_in=%s)", self.nc_name, fade_in)
        self.nc_index += 1
        if self.nc_index >= len(self.nc_tracks):
            random.shuffle(self.nc_tracks)
            self.nc_index = 0
        # Top up preload buffer back to PRELOAD_DEPTH
        self._top_up_preload_nc()

    def _play_co_next(self, fade_in):
        if not self.co_tracks:
            self.co_sound = None
            self.co_name = ""
            return
        with self._preload_lock:
            if self.preload_co:
                path, sound = self.preload_co.popleft()
            else:
                path = None
                sound = None
        if sound is None:
            path = self.co_tracks[self.co_index]
            try:
                sound = pygame.mixer.Sound(path)
                LOG.warning("co sync-decoded (preload was empty): %s",
                            os.path.basename(path))
            except Exception as ex:
                LOG.warning("co decode (sync) failed %s: %s", path, ex)
                self.co_sound = None
                self.co_name = ""
                return
        self.co_sound = sound
        self.co_name = os.path.splitext(os.path.basename(path))[0]
        fade_ms = self.FADE_MS if fade_in else 0
        try:
            self.ch_co.play(self.co_sound, fade_ms=fade_ms)
        except Exception as ex:
            LOG.warning("co play failed: %s", ex)
            return
        LOG.info("Combat playing: %s (fade_in=%s)", self.co_name, fade_in)
        self.co_index += 1
        if self.co_index >= len(self.co_tracks):
            random.shuffle(self.co_tracks)
            self.co_index = 0
        self._top_up_preload_co()

    # ---- public API ----
    def start(self):
        """Begin ambient playback (non-combat)."""
        with self._lock:
            if self.started:
                return
            self.started = True
            self.in_combat = False
            if self.nc_tracks:
                self._play_nc_next(fade_in=True)

    def stop(self):
        """Stop everything immediately. Used on STOP button / app close."""
        with self._lock:
            self.started = False
            self.in_combat = False
            try:
                self.ch_nc.stop()
                self.ch_co.stop()
            except Exception:
                pass
            self.nc_sound = None
            self.co_sound = None
            self.nc_name = ""
            self.co_name = ""

    def engage_combat(self):
        """Crossfade non-combat -> combat. Picks a fresh combat song."""
        with self._lock:
            if not self.started or self.in_combat:
                return
            if not self.co_tracks:
                LOG.info("engage_combat ignored: no combat tracks loaded")
                return
            # Fade out non-combat
            try:
                if self.ch_nc.get_busy():
                    self.ch_nc.fadeout(self.FADE_MS)
            except Exception:
                pass
            # Fade in combat (new song)
            self._play_co_next(fade_in=True)
            self.in_combat = True

    def disengage_combat(self):
        """Crossfade combat -> non-combat. Picks next non-combat song."""
        with self._lock:
            if not self.started or not self.in_combat:
                return
            try:
                if self.ch_co.get_busy():
                    self.ch_co.fadeout(self.FADE_MS)
            except Exception:
                pass
            self._play_nc_next(fade_in=True)
            self.in_combat = False

    def tick(self):
        """Advance the active queue if its current song has ended.
        Called periodically by the detector thread."""
        with self._lock:
            if not self.started:
                return
            try:
                if self.in_combat:
                    if self.co_sound and not self.ch_co.get_busy():
                        self._play_co_next(fade_in=False)
                else:
                    if self.nc_sound and not self.ch_nc.get_busy():
                        self._play_nc_next(fade_in=False)
            except Exception:
                pass

    def is_playing(self):
        try:
            return bool(self.ch_nc.get_busy() or self.ch_co.get_busy())
        except Exception:
            return False

    def now_playing(self):
        with self._lock:
            return self.co_name if self.in_combat else self.nc_name


class Detector(threading.Thread):
    """Runs detection loop and sends status events to the UI queue."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.q = app.event_q
        self._stop_event = threading.Event()
        self.last_t_press_ts = 0.0
        # Tracks previous-scan cond_b so we can detect a True->False edge
        # while combat is active (i.e. moment of "lock lost") and snapshot
        # the screen for diagnostics.
        self._prev_cond_b = False
        self._losslock_snap_count = 0
        self._kb_hook = None
        # measure primary monitor
        with mss.mss() as sct:
            mon = sct.monitors[1]  # primary
            self.mon_left = mon["left"]
            self.mon_top = mon["top"]
            self.mon_w = mon["width"]
            self.mon_h = mon["height"]
        rw = int(self.mon_w * CENTER_REGION_FRAC_W)
        rh = int(self.mon_h * CENTER_REGION_FRAC_H)
        cx = self.mon_left + self.mon_w // 2
        cy = self.mon_top + self.mon_h // 2
        self.region = {
            "left": cx - rw // 2,
            "top": cy - rh // 2,
            "width": rw,
            "height": rh,
        }
        # Wider context region (3x each side of the analysis region) so we can
        # see WHERE the lock indicator is relative to dead-center.
        cw = rw * 3
        ch = rh * 3
        self.context_region = {
            "left": cx - cw // 2,
            "top": cy - ch // 2,
            "width": cw,
            "height": ch,
        }
        # Search region: fixed 300x300 dead-center. Per-user calibration --
        # tight enough that cockpit DECOY/NOISE strips, decoy bars, side
        # panels, and asteroid lighting fall outside the box. Locks are only
        # detected when the bracket sits in this central combat area.
        SEARCH_W, SEARCH_H = 200, 200
        self.search_region = {
            "left": cx - SEARCH_W // 2,
            "top": cy - SEARCH_H // 2,
            "width": SEARCH_W,
            "height": SEARCH_H,
        }
        # Per-T-press capture dir
        self.shots_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tshots")
        try:
            os.makedirs(self.shots_dir, exist_ok=True)
        except Exception:
            pass
        self._dump_next = False
        self._t_press_count = 0

    def _on_t(self, e):
        # keyboard.on_press_key fires for press only
        self.last_t_press_ts = time.time()
        self._dump_next = True
        try:
            LOG.info("T pressed (event scan_code=%s name=%s)",
                     getattr(e, "scan_code", "?"), getattr(e, "name", "?"))
        except Exception:
            pass

    def stop(self):
        self._stop_event.set()

    def _find_bracket_blob(self, arr_bgra):
        """Locate the dominant bracket-shape red blob in a BGRA image.
        Returns (size, slice_y, slice_x, aspect) or (0, None, None, 0) if none.
        """
        r = arr_bgra[:, :, 2].astype(np.int16)
        g = arr_bgra[:, :, 1].astype(np.int16)
        b = arr_bgra[:, :, 0].astype(np.int16)
        mask = (r >= RED_R_MIN) & ((r - g) >= RED_DELTA) & ((r - b) >= RED_DELTA)
        if not mask.any():
            return 0, None, None, 0.0
        labeled, n = _cc_label(mask, structure=np.ones((3, 3), dtype=np.int8))
        if n == 0:
            return 0, None, None, 0.0
        sizes = np.bincount(labeled.ravel())
        if sizes.size <= 1:
            return 0, None, None, 0.0
        biggest_label = int(np.argmax(sizes[1:]) + 1)
        biggest_size = int(sizes[biggest_label])
        ys, xs = np.where(labeled == biggest_label)
        h = int(ys.max() - ys.min() + 1)
        w = int(xs.max() - xs.min() + 1)
        aspect = max(w, h) / max(min(w, h), 1)
        if not (LOCK_BLOB_MIN <= biggest_size <= LOCK_BLOB_MAX
                and aspect <= LOCK_ASPECT_MAX):
            return 0, None, None, aspect
        # bounding-box slice with a small padding so the template includes the
        # outline of the bracket, not just the colored pixels
        pad = 4
        y0 = max(0, int(ys.min()) - pad)
        y1 = min(arr_bgra.shape[0], int(ys.max()) + 1 + pad)
        x0 = max(0, int(xs.min()) - pad)
        x1 = min(arr_bgra.shape[1], int(xs.max()) + 1 + pad)
        return biggest_size, slice(y0, y1), slice(x0, x1), aspect

    def _capture_lock_score(self, sct):
        """Count lock-red pixels that belong to bracket/text-shaped blobs.

        Two-stage filter:
          1. Color: SC lock-red (R 175-230, G<=65, B<=65). Excludes brighter
             cockpit warning red and oranger decoy elements.
          2. Shape: per-blob, keep only blobs with aspect <= ASPECT_MAX_KEEP
             (rejects long thin cockpit strips like DECOY/NOISE bars which
             have aspect 20-100+) and size <= SIZE_MAX_KEEP (rejects huge
             panel glows). Bracket-line segments and name-text characters
             are short and roughly square so they survive.

        Returns (score, total_lock_red_pre_shape, biggest_blob, biggest_aspect).
        score = sum of pixels in surviving (bracket/text-like) blobs only.
        """
        search_shot = sct.grab(self.search_region)
        search_arr = np.asarray(search_shot)  # BGRA
        b = search_arr[:, :, 0]
        g = search_arr[:, :, 1]
        r = search_arr[:, :, 2]
        mask = ((r >= LOCK_R_MIN) & (r <= LOCK_R_MAX)
                & (g <= LOCK_G_MAX) & (b <= LOCK_B_MAX))
        # Reticle + cockpit-beam exclusion: SC's reticle is dead-center, AND
        # the horizontal cockpit beam runs through center on most ships. An
        # 80x80 hole covers both. The lock bracket sits AROUND the target,
        # which is usually visible in the area outside the hole.
        h, w = mask.shape
        cy, cx = h // 2, w // 2
        hole = 40  # half-width of exclusion (80x80 total)
        mask[max(0, cy - hole):cy + hole, max(0, cx - hole):cx + hole] = False
        total_lock_red = int(mask.sum())
        if total_lock_red == 0:
            return 0, 0, 0, 0.0
        labeled, n = _cc_label(mask, structure=np.ones((3, 3), dtype=np.int8))
        if n == 0:
            return 0, total_lock_red, 0, 0.0
        sizes = np.bincount(labeled.ravel())
        sizes[0] = 0

        # per-blob bounding box via np.where is O(N) per call but for our
        # small mask (~1500x900) and typically tens of blobs it's fast enough
        # cockpit strips are 20-100+ aspect, distinct from bracket lines.
        # At long range bracket lines can be ~14 pixels long, 1 px wide -> aspect 14.
        # 18 leaves room for that while still rejecting cockpit panel strips.
        ASPECT_MAX_KEEP = 18.0
        SIZE_MAX_KEEP = 400      # name text + a bracket cluster is ~150-300

        score = 0
        biggest = 0
        biggest_aspect = 0.0
        for lbl in range(1, n + 1):
            sz = int(sizes[lbl])
            if sz <= 0:
                continue
            if sz > biggest:
                biggest = sz
            if sz > SIZE_MAX_KEEP:
                continue
            ys, xs = np.where(labeled == lbl)
            h = int(ys.max() - ys.min() + 1)
            w = int(xs.max() - xs.min() + 1)
            asp = max(w, h) / max(min(w, h), 1)
            if asp > biggest_aspect:
                biggest_aspect = asp
            if asp > ASPECT_MAX_KEEP:
                continue
            # bracket / text character / short bracket-line shape -> keep
            score += sz
        return score, total_lock_red, biggest, biggest_aspect

    def _save_lostlock_snapshot(self, sct, bright, total_red, biggest_blob, aspect):
        """Save a snapshot at the moment cond_b drops during combat.
        Saves both the analysis search region AND a wider context view so we
        can see what red element JUST vanished (or whether something else
        persisted)."""
        self._losslock_snap_count += 1
        n = self._losslock_snap_count
        ts = time.strftime("%H%M%S")
        roi = np.asarray(sct.grab(self.search_region))
        roi_rgb = roi[:, :, [2, 1, 0]].astype(np.uint8)
        roi_name = ("lostlock%03d_%s_score=%d_red=%d_blob=%d_asp=%.1f_search.png"
                    % (n, ts, bright, total_red, biggest_blob, aspect))
        Image.fromarray(roi_rgb, "RGB").save(
            os.path.join(self.shots_dir, roi_name))
        # Wider context: 2x the search region
        cw = self.search_region["width"] * 2
        ch = self.search_region["height"] * 2
        cx = self.search_region["left"] + self.search_region["width"] // 2
        cy = self.search_region["top"] + self.search_region["height"] // 2
        ctx_region = {"left": cx - cw // 2, "top": cy - ch // 2,
                      "width": cw, "height": ch}
        ctx = np.asarray(sct.grab(ctx_region))
        ctx_rgb = ctx[:, :, [2, 1, 0]].astype(np.uint8).copy()
        # outline the search box in green
        ox = self.search_region["left"] - ctx_region["left"]
        oy = self.search_region["top"] - ctx_region["top"]
        rx2 = ox + self.search_region["width"]
        ry2 = oy + self.search_region["height"]
        for thick in range(2):
            ctx_rgb[oy + thick, ox:rx2, :] = (0, 255, 0)
            ctx_rgb[ry2 - 1 - thick, ox:rx2, :] = (0, 255, 0)
            ctx_rgb[oy:ry2, ox + thick, :] = (0, 255, 0)
            ctx_rgb[oy:ry2, rx2 - 1 - thick, :] = (0, 255, 0)
        Image.fromarray(ctx_rgb, "RGB").save(
            os.path.join(self.shots_dir,
                         "lostlock%03d_%s_context.png" % (n, ts)))
        LOG.info("LOST-LOCK snapshot #%d: bright=%d red=%d blob=%d asp=%.2f -> %s",
                 n, bright, total_red, biggest_blob, aspect, roi_name)

    def _save_debug_snapshots(self, sct, bright, total_red=0, biggest_blob=0, aspect=0.0):
        ts = time.strftime("%H%M%S")
        n = self._t_press_count
        # 1) the actual analysis region
        roi = np.asarray(sct.grab(self.region))
        rgb = roi[:, :, [2, 1, 0]]
        roi_path = os.path.join(
            self.shots_dir,
            "t%03d_%s_score=%d_blob=%d_red=%d_asp=%.1f.png"
            % (n, ts, bright, biggest_blob, total_red, aspect))
        Image.fromarray(rgb.astype(np.uint8), "RGB").save(roi_path)
        # 2) wider context with the ROI marked in green
        ctx = np.asarray(sct.grab(self.context_region))
        ctx_rgb = ctx[:, :, [2, 1, 0]].astype(np.uint8).copy()
        # compute where the ROI lives inside the context
        ox = self.region["left"] - self.context_region["left"]
        oy = self.region["top"] - self.context_region["top"]
        rx2 = ox + self.region["width"]
        ry2 = oy + self.region["height"]
        # draw a 2-pixel-thick green rectangle
        for thick in range(2):
            ctx_rgb[oy + thick, ox:rx2, :] = (0, 255, 0)
            ctx_rgb[ry2 - 1 - thick, ox:rx2, :] = (0, 255, 0)
            ctx_rgb[oy:ry2, ox + thick, :] = (0, 255, 0)
            ctx_rgb[oy:ry2, rx2 - 1 - thick, :] = (0, 255, 0)
        ctx_path = os.path.join(
            self.shots_dir,
            "t%03d_%s_context.png" % (n, ts))
        Image.fromarray(ctx_rgb, "RGB").save(ctx_path)
        LOG.info("T-snapshot #%d saved: red=%d roi=%s ctx=%s",
                 n, bright, os.path.basename(roi_path),
                 os.path.basename(ctx_path))

    def run(self):
        LOG.info("Detector thread starting. Region=%s mon=(%dx%d)",
                 self.region, self.mon_w, self.mon_h)
        try:
            self._kb_hook = keyboard.on_press_key("t", self._on_t, suppress=False)
            LOG.info("Keyboard hook installed for T")
        except Exception as ex:
            LOG.exception("Keyboard hook install failed: %s", ex)
            self.q.put(("error", "Keyboard hook failed: " + str(ex)))
            return

        try:
            with mss.mss() as sct:
                player = self.app.player
                timeout_started_at = None  # epoch ts when condB last failed
                while not self._stop_event.is_set():
                    scan_interval = self.app.cfg.scan_interval
                    lock_timeout = self.app.cfg.lock_timeout
                    bright_thresh = self.app.cfg.brightness_threshold

                    if self.last_t_press_ts <= 0.0:
                        t_age = None
                    else:
                        t_age = time.time() - self.last_t_press_ts
                    cond_a = (t_age is not None) and (t_age <= float(T_PRESS_WINDOW_SECONDS))
                    fg_title = foreground_window_title()
                    fg_is_sc = (SC_WINDOW_SUBSTR.lower() in fg_title.lower())
                    try:
                        bright, total_red, biggest_blob, aspect = self._capture_lock_score(sct)
                    except Exception as ex:
                        self.q.put(("error", "Capture failed: " + str(ex)))
                        bright, total_red, biggest_blob, aspect = 0, 0, 0, 0.0
                    # Gate cond_b on SC being the foreground window. Background
                    # SC HUD with no lock still produces some red blobs; without
                    # this gate, red text in VS Code / browsers / Discord also
                    # falsely passes when SC is not even in focus.
                    cond_b = fg_is_sc and (bright >= bright_thresh)

                    # Save a debug PNG pair every time T was pressed since last
                    # scan, so we can see exactly what the analysis sampled.
                    if self._dump_next:
                        self._dump_next = False
                        self._t_press_count += 1
                        try:
                            self._save_debug_snapshots(sct, bright,
                                                       total_red=total_red,
                                                       biggest_blob=biggest_blob,
                                                       aspect=aspect)
                        except Exception as ex:
                            LOG.warning("debug snapshot failed: %s", ex)

                    # Lock-lost diagnostic: when combat is active and cond_b
                    # transitions True -> False, save a snapshot. This catches
                    # the exact frame where the detector thinks the bracket
                    # is gone -- helps diagnose situations where the player
                    # expected the lock to release but it didn't (e.g. kill
                    # notification text persisting in the search region).
                    if player.in_combat and self._prev_cond_b and not cond_b:
                        try:
                            self._save_lostlock_snapshot(sct, bright, total_red,
                                                          biggest_blob, aspect)
                        except Exception as ex:
                            LOG.warning("lostlock snapshot failed: %s", ex)
                    self._prev_cond_b = cond_b

                    # live diagnostics to UI
                    self.q.put(("debug", t_age, bright, bright_thresh, cond_a, cond_b))
                    LOG.debug("scan t_age=%s score=%d/%d red_total=%d blob=%d aspect=%.2f fg=%r sc=%s A=%s B=%s in_combat=%s",
                              ("%.2f" % t_age) if t_age is not None else "None",
                              bright, bright_thresh, total_red, biggest_blob, aspect,
                              fg_title[:40], fg_is_sc,
                              cond_a, cond_b, player.in_combat)

                    if not player.in_combat:
                        # Ambient (non-combat) state. Music is playing in the
                        # background. Watch for lock to fire combat music.
                        if cond_a and cond_b:
                            LOG.info("LOCK TRIGGER: engaging combat music")
                            player.engage_combat()
                            timeout_started_at = None
                            self.q.put(("status", ST_LOCKED, 0))
                            self.q.put(("now_playing", player.now_playing()))
                        else:
                            self.q.put(("status", ST_SCAN, 0))
                    else:
                        # Combat is active. Watch for lock loss; on countdown
                        # completion, fade back to non-combat.
                        if cond_b:
                            # lock still confirmed -> reset timer, keep combat
                            timeout_started_at = None
                            self.q.put(("status", ST_LOCKED, 0))
                            self.q.put(("now_playing", player.now_playing()))
                        else:
                            if timeout_started_at is None:
                                timeout_started_at = time.time()
                            elapsed = time.time() - timeout_started_at
                            remaining = int(max(0, lock_timeout - elapsed + 0.999))
                            if elapsed >= lock_timeout:
                                player.disengage_combat()
                                timeout_started_at = None
                                # Invalidate T-press window so a stale T from
                                # before doesn't re-fire combat the moment a
                                # bracket reappears.
                                self.last_t_press_ts = 0.0
                                LOG.info("Lock-lost timeout reached (%ds): combat -> non-combat, T invalidated", lock_timeout)
                                self.q.put(("status", ST_SCAN, 0))
                                self.q.put(("now_playing", player.now_playing()))
                            else:
                                self.q.put(("status", ST_TIMEOUT, remaining))

                    # Advance the active queue if its current song ended.
                    player.tick()

                    # Sleep in small chunks so stop is responsive AND so song
                    # transitions feel snappy (we tick mid-interval too).
                    end = time.time() + float(scan_interval)
                    while time.time() < end and not self._stop_event.is_set():
                        time.sleep(0.1)
                        player.tick()
        finally:
            try:
                if self._kb_hook is not None:
                    keyboard.unhook(self._kb_hook)
            except Exception:
                pass
            self.q.put(("stopped",))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = Config()
        self.cfg.load()

        self.player = CrossfadePlayer()
        if self.cfg.combat_folder:
            self.player.load_combat_folder(self.cfg.combat_folder)
        if self.cfg.non_combat_folder:
            self.player.load_non_combat_folder(self.cfg.non_combat_folder)

        self.event_q = queue.Queue()
        self.detector = None

        self._ticker_text = "---"
        self._ticker_offset = 0
        self._ticker_width_chars = 40

        self._build_ui()
        self._set_status(ST_IDLE, 0)
        self._update_track_count_label()
        self.root.after(100, self._poll_queue)
        self.root.after(150, self._tick_marquee)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- UI construction -----

    def _build_ui(self):
        r = self.root
        r.title("SC Lock Radio")
        r.geometry("500x620")
        r.configure(bg=BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, arrowcolor=TEXT)
        style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)

        # Title
        title = tk.Label(r, text="SC LOCK RADIO", bg=BG, fg=TEXT,
                         font=("Segoe UI", 20, "bold"))
        title.pack(pady=(14, 10))

        # Playlist section: two folder pickers
        pl = tk.Frame(r, bg=PANEL, padx=10, pady=10)
        pl.pack(fill="x", padx=12, pady=6)

        # Non-combat (ambient default) folder
        tk.Label(pl, text="Non-Combat Folder (ambient)", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        nc_row = tk.Frame(pl, bg=PANEL)
        nc_row.pack(fill="x", pady=(2, 2))
        self.nc_folder_var = tk.StringVar(value=self.cfg.non_combat_folder)
        self.nc_folder_entry = tk.Entry(nc_row, textvariable=self.nc_folder_var,
                                        state="readonly", readonlybackground=BG,
                                        fg=TEXT, disabledforeground=TEXT,
                                        relief="flat")
        self.nc_folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(nc_row, text="Browse", command=self._on_browse_non_combat,
                  bg=PANEL, fg=TEXT, activebackground="#444444",
                  activeforeground=TEXT, relief="flat", padx=10
                  ).pack(side="right")
        self.nc_count_var = tk.StringVar(value="Non-Combat Tracks: 0")
        tk.Label(pl, textvariable=self.nc_count_var, bg=PANEL, fg=GREY,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 6))

        # Combat (lock-engaged) folder
        tk.Label(pl, text="Combat Folder (lock engaged)", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        co_row = tk.Frame(pl, bg=PANEL)
        co_row.pack(fill="x", pady=(2, 2))
        self.co_folder_var = tk.StringVar(value=self.cfg.combat_folder)
        self.co_folder_entry = tk.Entry(co_row, textvariable=self.co_folder_var,
                                        state="readonly", readonlybackground=BG,
                                        fg=TEXT, disabledforeground=TEXT,
                                        relief="flat")
        self.co_folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(co_row, text="Browse", command=self._on_browse_combat,
                  bg=ACCENT, fg=TEXT, activebackground="#aa0000",
                  activeforeground=TEXT, relief="flat", padx=10
                  ).pack(side="right")
        self.co_count_var = tk.StringVar(value="Combat Tracks: 0")
        tk.Label(pl, textvariable=self.co_count_var, bg=PANEL, fg=GREY,
                 font=("Segoe UI", 9)).pack(anchor="w")

        # Detection settings
        ds = tk.Frame(r, bg=PANEL, padx=10, pady=10)
        ds.pack(fill="x", padx=12, pady=6)

        tk.Label(ds, text="Scan Interval", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.scan_var = tk.StringVar(value=str(self.cfg.scan_interval) + " second"
                                     + ("s" if self.cfg.scan_interval != 1 else ""))
        self.scan_combo = ttk.Combobox(
            ds, textvariable=self.scan_var, state="readonly",
            values=["1 second", "2 seconds", "3 seconds", "4 seconds", "5 seconds"])
        self.scan_combo.pack(fill="x", pady=(2, 0))
        self.scan_combo.bind("<<ComboboxSelected>>", self._on_scan_changed)
        tk.Label(ds, text="(Lower = more CPU usage)", bg=PANEL, fg=GREY,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 6))

        # lock timeout slider
        tk.Label(ds, text="Lock Timeout (seconds)", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        row2 = tk.Frame(ds, bg=PANEL)
        row2.pack(fill="x")
        self.timeout_value_var = tk.StringVar(value=str(self.cfg.lock_timeout))
        self.timeout_scale = tk.Scale(row2, from_=5, to=60, orient="horizontal",
                                      bg=PANEL, fg=TEXT, troughcolor=BG,
                                      highlightthickness=0, showvalue=False,
                                      command=self._on_timeout_changed)
        self.timeout_scale.set(self.cfg.lock_timeout)
        self.timeout_scale.pack(side="left", fill="x", expand=True)
        tk.Label(row2, textvariable=self.timeout_value_var, bg=PANEL, fg=TEXT,
                 width=4).pack(side="right")

        # brightness threshold slider
        tk.Label(ds, text="Brightness Threshold", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 0))
        row3 = tk.Frame(ds, bg=PANEL)
        row3.pack(fill="x")
        self.bright_value_var = tk.StringVar(value=str(self.cfg.brightness_threshold))
        self.bright_scale = tk.Scale(row3, from_=1, to=200, orient="horizontal",
                                     bg=PANEL, fg=TEXT, troughcolor=BG,
                                     highlightthickness=0, showvalue=False,
                                     command=self._on_bright_changed)
        self.bright_scale.set(self.cfg.brightness_threshold)
        self.bright_scale.pack(side="left", fill="x", expand=True)
        tk.Label(row3, textvariable=self.bright_value_var, bg=PANEL, fg=TEXT,
                 width=4).pack(side="right")
        tk.Label(ds, text="(Higher = less sensitive)", bg=PANEL, fg=GREY,
                 font=("Segoe UI", 8)).pack(anchor="w")

        # Controls
        ctrl = tk.Frame(r, bg=BG)
        ctrl.pack(fill="x", padx=12, pady=8)
        self.start_btn = tk.Button(ctrl, text="START", command=self._on_start,
                                   bg=ACCENT, fg=TEXT, activebackground="#aa0000",
                                   activeforeground=TEXT, relief="flat",
                                   font=("Segoe UI", 11, "bold"), width=12)
        self.stop_btn = tk.Button(ctrl, text="STOP", command=self._on_stop,
                                  bg=PANEL, fg=TEXT, activebackground="#444444",
                                  activeforeground=TEXT, relief="flat",
                                  font=("Segoe UI", 11, "bold"), width=12,
                                  state="disabled")
        self.start_btn.pack(side="left", expand=True, padx=10)
        self.stop_btn.pack(side="right", expand=True, padx=10)

        # Status panel
        sp = tk.Frame(r, bg=PANEL, padx=10, pady=10)
        sp.pack(fill="x", padx=12, pady=6)
        row4 = tk.Frame(sp, bg=PANEL)
        row4.pack(fill="x")
        tk.Label(row4, text="Status:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value=ST_IDLE)
        self.status_label = tk.Label(row4, textvariable=self.status_var,
                                     bg=PANEL, fg=GREY,
                                     font=("Segoe UI", 11, "bold"))
        self.status_label.pack(side="left", padx=(8, 0))

        # Live diagnostic strip: T-press age, bright pixel count, cond truth
        self.debug_var = tk.StringVar(value="T: never | Bright: -- | A: - B: -")
        self.debug_label = tk.Label(sp, textvariable=self.debug_var, bg=PANEL,
                                    fg=GREY, font=("Consolas", 9), anchor="w",
                                    justify="left")
        self.debug_label.pack(anchor="w", pady=(4, 0))

        # Now playing ticker
        np_frame = tk.Frame(r, bg=BG)
        np_frame.pack(fill="x", padx=12, pady=(10, 14), side="bottom")
        tk.Label(np_frame, text="NOW PLAYING", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.ticker_var = tk.StringVar(value="---")
        self.ticker_label = tk.Label(np_frame, textvariable=self.ticker_var,
                                     bg=BG, fg=ACCENT,
                                     font=("Consolas", 12), anchor="w")
        self.ticker_label.pack(fill="x")

    # ----- UI callbacks -----

    def _on_browse_combat(self):
        path = filedialog.askdirectory(title="Select Combat Music Folder")
        if not path:
            return
        self.cfg.combat_folder = path
        self.co_folder_var.set(path)
        n = self.player.load_combat_folder(path)
        self.cfg.save()
        self.co_count_var.set("Combat Tracks: " + str(n))

    def _on_browse_non_combat(self):
        path = filedialog.askdirectory(title="Select Non-Combat (Ambient) Music Folder")
        if not path:
            return
        self.cfg.non_combat_folder = path
        self.nc_folder_var.set(path)
        n = self.player.load_non_combat_folder(path)
        self.cfg.save()
        self.nc_count_var.set("Non-Combat Tracks: " + str(n))

    def _on_scan_changed(self, _evt=None):
        val = self.scan_var.get()
        try:
            n = int(val.split()[0])
        except Exception:
            n = 2
        self.cfg.scan_interval = n
        self.cfg.save()

    def _on_timeout_changed(self, val):
        try:
            n = int(float(val))
        except Exception:
            return
        self.cfg.lock_timeout = n
        self.timeout_value_var.set(str(n))
        self.cfg.save()

    def _on_bright_changed(self, val):
        try:
            n = int(float(val))
        except Exception:
            return
        self.cfg.brightness_threshold = n
        self.bright_value_var.set(str(n))
        self.cfg.save()

    def _on_start(self):
        if self.detector is not None and self.detector.is_alive():
            return
        nc = self.player.non_combat_count()
        co = self.player.combat_count()
        if nc == 0 and co == 0:
            self._set_status(ST_IDLE, 0)
            self.ticker_var.set("Pick at least one music folder (combat or non-combat)")
            return
        if nc == 0:
            self.ticker_var.set("WARNING: no non-combat folder; ambient will be silent")
        if co == 0:
            self.ticker_var.set("WARNING: no combat folder; lock won't trigger music")
        # Begin ambient playback; combat fires on lock via the detector.
        self.player.start()
        self.detector = Detector(self)
        self.detector.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status(ST_SCAN, 0)

    def _on_stop(self):
        if self.detector is not None:
            self.detector.stop()
        self.player.stop()
        self._set_status(ST_IDLE, 0)
        self._ticker_text = "---"
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def _on_close(self):
        try:
            if self.detector is not None:
                self.detector.stop()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        self.root.destroy()

    # ----- queue / status / ticker -----

    def _update_track_count_label(self, n=None):
        # Replaces old single-folder label with per-category counts.
        self.nc_count_var.set("Non-Combat Tracks: " + str(self.player.non_combat_count()))
        self.co_count_var.set("Combat Tracks: " + str(self.player.combat_count()))

    def _set_status(self, state, remaining):
        if state == ST_IDLE:
            self.status_var.set(ST_IDLE)
            self.status_label.configure(fg=GREY)
        elif state == ST_SCAN:
            self.status_var.set(ST_SCAN)
            self.status_label.configure(fg=TEXT)
        elif state == ST_LOCKED:
            self.status_var.set(ST_LOCKED)
            self.status_label.configure(fg=GREEN)
        elif state == ST_TIMEOUT:
            self.status_var.set(ST_TIMEOUT + " (" + str(remaining) + "s remaining)")
            self.status_label.configure(fg=YELLOW)

    def _poll_queue(self):
        try:
            while True:
                evt = self.event_q.get_nowait()
                tag = evt[0]
                if tag == "status":
                    self._set_status(evt[1], evt[2] if len(evt) > 2 else 0)
                elif tag == "now_playing":
                    name = evt[1]
                    if name:
                        self._ticker_text = name + "   "
                    else:
                        self._ticker_text = "---"
                    self._ticker_offset = 0
                elif tag == "stopped":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                elif tag == "debug":
                    _, t_age, bright, thresh, ca, cb = evt
                    if t_age is None:
                        t_str = "never"
                    else:
                        t_str = ("%.1fs ago" % t_age)
                    a_str = "Y" if ca else "N"
                    b_str = "Y" if cb else "N"
                    self.debug_var.set(
                        "T: " + t_str
                        + " | Bright: " + str(bright) + "/" + str(thresh)
                        + " | A:" + a_str + " B:" + b_str)
                elif tag == "error":
                    self.ticker_var.set("ERROR: " + str(evt[1]))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _tick_marquee(self):
        text = self._ticker_text if self._ticker_text else "---"
        if text == "---" or not text.strip():
            self.ticker_var.set("---")
        else:
            padded = text + " " * 8
            if len(padded) < self._ticker_width_chars:
                padded = (padded + " " * self._ticker_width_chars)
            self._ticker_offset = (self._ticker_offset + 1) % len(padded)
            window = (padded + padded)[self._ticker_offset:
                                       self._ticker_offset + self._ticker_width_chars]
            self.ticker_var.set(window)
        self.root.after(150, self._tick_marquee)


def main():
    # Pre-flight: keyboard hooks need admin on Windows for global capture in
    # some game contexts. We do not abort if it fails; we surface via UI.
    LOG.info("=== SC Lock Radio start. cwd=%s pid=%d ===", os.getcwd(), os.getpid())
    try:
        is_admin = False
        try:
            import ctypes
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass
        LOG.info("Running as admin: %s", is_admin)
        root = tk.Tk()
        app = App(root)
        root.mainloop()
        LOG.info("=== mainloop exited normally ===")
    except Exception:
        LOG.error("FATAL in main:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
