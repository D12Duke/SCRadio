"""
SC Lock Radio - single-file desktop app.
ASCII only. No Unicode anywhere.
"""

import atexit
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
LOCK_R_MAX = 230
LOCK_G_MAX = 65
LOCK_B_MAX = 65
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
        self.music_folder = ""
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
                self.music_folder = s.get("music_folder", self.music_folder)
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
            "music_folder": self.music_folder,
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


class MusicPlayer:
    """Wraps pygame.mixer. Owns the shuffled playlist and current-track state."""

    def __init__(self):
        pygame.mixer.init()
        self.tracks = []
        self.index = 0
        self.current_name = ""
        self._lock = threading.Lock()

    def load_folder(self, folder):
        with self._lock:
            self.tracks = []
            self.index = 0
            self.current_name = ""
            if not folder or not os.path.isdir(folder):
                return 0
            files = []
            for entry in os.listdir(folder):
                low = entry.lower()
                if low.endswith(".mp3"):
                    files.append(os.path.join(folder, entry))
            random.shuffle(files)
            self.tracks = files
            return len(self.tracks)

    def track_count(self):
        with self._lock:
            return len(self.tracks)

    def _start_index_locked(self):
        if not self.tracks:
            self.current_name = ""
            return False
        path = self.tracks[self.index]
        self.current_name = os.path.splitext(os.path.basename(path))[0]
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            return True
        except Exception:
            self.current_name = ""
            return False

    def start(self):
        with self._lock:
            if not self.tracks:
                return False
            return self._start_index_locked()

    def advance(self):
        with self._lock:
            if not self.tracks:
                return False
            self.index += 1
            if self.index >= len(self.tracks):
                # reshuffle and restart playlist
                random.shuffle(self.tracks)
                self.index = 0
            return self._start_index_locked()

    def stop(self):
        with self._lock:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            self.current_name = ""

    def is_playing(self):
        try:
            return bool(pygame.mixer.music.get_busy())
        except Exception:
            return False

    def now_playing(self):
        with self._lock:
            return self.current_name


class Detector(threading.Thread):
    """Runs detection loop and sends status events to the UI queue."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.q = app.event_q
        self._stop_event = threading.Event()
        self.last_t_press_ts = 0.0
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
        # Template held in memory; auto-captured from current screen on T press.
        # No disk file required.
        self._template_gray = None
        self._auto_capture_pending = False  # set True on each T press

    def _on_t(self, e):
        # keyboard.on_press_key fires for press only
        self.last_t_press_ts = time.time()
        self._dump_next = True
        # T resets template tracking: the next scan grabs a fresh template
        # from the current screen so we lock onto THIS target's bracket.
        self._auto_capture_pending = True
        self._template_gray = None
        try:
            LOG.info("T pressed (event scan_code=%s name=%s) - template reset",
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

                    # live diagnostics to UI
                    self.q.put(("debug", t_age, bright, bright_thresh, cond_a, cond_b))
                    LOG.debug("scan t_age=%s score=%d/%d red_total=%d blob=%d aspect=%.2f fg=%r sc=%s A=%s B=%s playing=%s",
                              ("%.2f" % t_age) if t_age is not None else "None",
                              bright, bright_thresh, total_red, biggest_blob, aspect,
                              fg_title[:40], fg_is_sc,
                              cond_a, cond_b, player.is_playing())

                    playing = player.is_playing()

                    if not playing:
                        if cond_a and cond_b:
                            LOG.info("LOCK TRIGGER: starting music")
                            ok = player.start()
                            if ok:
                                LOG.info("Music started: %s", player.now_playing())
                                timeout_started_at = None
                                self.q.put(("status", ST_LOCKED, 0))
                                self.q.put(("now_playing", player.now_playing()))
                            else:
                                LOG.warning("player.start() returned False (no tracks?)")
                                self.q.put(("status", ST_SCAN, 0))
                        else:
                            self.q.put(("status", ST_SCAN, 0))
                    else:
                        # music is currently playing
                        if cond_b:
                            # lock still confirmed
                            timeout_started_at = None
                            if cond_a:
                                # T pressed again -> spec calls this target-switch:
                                # reset timer. Timer is already reset above.
                                pass
                            self.q.put(("status", ST_LOCKED, 0))
                        else:
                            # condB failed -> countdown
                            if timeout_started_at is None:
                                timeout_started_at = time.time()
                            elapsed = time.time() - timeout_started_at
                            remaining = int(max(0, lock_timeout - elapsed + 0.999))
                            if elapsed >= lock_timeout:
                                player.stop()
                                timeout_started_at = None
                                # Invalidate the T-press window: after the
                                # countdown completes (whatever the user has
                                # the Lock Timeout slider set to), the user
                                # must press T again to start a new lock.
                                # Otherwise a within-T_PRESS_WINDOW_SECONDS T
                                # from before would auto-restart music the
                                # moment a bracket reappears.
                                self.last_t_press_ts = 0.0
                                LOG.info("Lock-lost timeout reached (%ds): music stopped, T invalidated", lock_timeout)
                                self.q.put(("status", ST_SCAN, 0))
                                self.q.put(("now_playing", ""))
                            else:
                                self.q.put(("status", ST_TIMEOUT, remaining))

                    # advance to next song when current ends
                    if player.track_count() > 0 and not player.is_playing():
                        # Only auto-advance if we previously HAD a current track
                        # (i.e. we were playing). Otherwise this would auto-start
                        # without a lock. now_playing() returns "" after stop().
                        if player.now_playing():
                            player.advance()
                            self.q.put(("now_playing", player.now_playing()))

                    # sleep in small chunks so stop is responsive
                    end = time.time() + float(scan_interval)
                    while time.time() < end and not self._stop_event.is_set():
                        time.sleep(0.05)
                        # detect song-end mid-interval so transitions feel snappy
                        if player.now_playing() and not player.is_playing():
                            player.advance()
                            self.q.put(("now_playing", player.now_playing()))
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

        self.player = MusicPlayer()
        if self.cfg.music_folder:
            self.player.load_folder(self.cfg.music_folder)

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

        # Playlist section
        pl = tk.Frame(r, bg=PANEL, padx=10, pady=10)
        pl.pack(fill="x", padx=12, pady=6)
        tk.Label(pl, text="Music Folder", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")

        row = tk.Frame(pl, bg=PANEL)
        row.pack(fill="x", pady=(4, 4))
        self.folder_var = tk.StringVar(value=self.cfg.music_folder)
        self.folder_entry = tk.Entry(row, textvariable=self.folder_var,
                                     state="readonly", readonlybackground=BG,
                                     fg=TEXT, disabledforeground=TEXT,
                                     relief="flat")
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        browse = tk.Button(row, text="Browse", command=self._on_browse,
                           bg=ACCENT, fg=TEXT, activebackground="#aa0000",
                           activeforeground=TEXT, relief="flat", padx=10)
        browse.pack(side="right")

        self.track_count_var = tk.StringVar(value="Tracks Loaded: 0")
        tk.Label(pl, textvariable=self.track_count_var, bg=PANEL, fg=TEXT).pack(
            anchor="w")

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

    def _on_browse(self):
        path = filedialog.askdirectory(title="Select Music Folder")
        if not path:
            return
        self.cfg.music_folder = path
        self.folder_var.set(path)
        n = self.player.load_folder(path)
        self.cfg.save()
        self._update_track_count_label(n)

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
        if self.player.track_count() == 0:
            self._set_status(ST_IDLE, 0)
            self.ticker_var.set("No tracks loaded - pick a music folder")
            return
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
        if n is None:
            n = self.player.track_count()
        self.track_count_var.set("Tracks Loaded: " + str(n))

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
