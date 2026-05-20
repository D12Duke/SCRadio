"""Diagnose mss capture across all monitors.
Saves a PNG of each monitor's full frame and its center-30% region,
plus a bright-pixel count for the center region.
Run with: venv\\Scripts\\python.exe probe_monitors.py
"""
import os
import sys
import time
import mss
import numpy as np
from PIL import Image

OUT = os.path.dirname(os.path.abspath(__file__))


def bright_count(arr_bgra, cutoff=200):
    # Red-dominance: distinguishes target-lock indicators from generic HUD.
    r = arr_bgra[:, :, 2].astype("int16")
    g = arr_bgra[:, :, 1].astype("int16")
    b = arr_bgra[:, :, 0].astype("int16")
    mask = (r >= 180) & ((r - g) >= 60) & ((r - b) >= 60)
    return int(mask.sum())


def save_png(arr_bgra, path):
    rgb = arr_bgra[:, :, [2, 1, 0]]
    Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(path)


def main():
    print("Waiting 3 seconds so you can switch focus to Star Citizen...")
    time.sleep(3)
    with mss.mss() as sct:
        mons = sct.monitors
        print("mss reports %d monitor entries (index 0 = virtual all-screen):"
              % len(mons))
        for i, m in enumerate(mons):
            print("  [%d] left=%d top=%d width=%d height=%d"
                  % (i, m["left"], m["top"], m["width"], m["height"]))
        print()

        # iterate real monitors (skip index 0 which is the union rect)
        for i in range(1, len(mons)):
            m = mons[i]
            print("--- monitor %d (%dx%d) ---" % (i, m["width"], m["height"]))
            full = np.asarray(sct.grab(m))
            full_path = os.path.join(OUT, "probe_mon%d_full.png" % i)
            save_png(full, full_path)
            print("  full frame  bright>=200 count = %d   saved -> %s"
                  % (bright_count(full), os.path.basename(full_path)))

            rw = int(m["width"] * 0.05)
            rh = int(m["height"] * 0.05)
            cx = m["left"] + m["width"] // 2
            cy = m["top"] + m["height"] // 2
            region = {"left": cx - rw // 2, "top": cy - rh // 2,
                      "width": rw, "height": rh}
            cen = np.asarray(sct.grab(region))
            cen_path = os.path.join(OUT, "probe_mon%d_center.png" % i)
            save_png(cen, cen_path)
            print("  center 30%%  bright>=200 count = %d   saved -> %s"
                  % (bright_count(cen), os.path.basename(cen_path)))
    print()
    print("Open the PNG files in this folder to see exactly what mss saw.")
    print("If center is pitch black on the monitor where SC runs, that confirms")
    print("exclusive-fullscreen blocks capture. Fix: SC -> Settings -> Graphics")
    print("-> Display Mode -> set to BORDERLESS (or Windowed Fullscreen).")


if __name__ == "__main__":
    sys.exit(main())
