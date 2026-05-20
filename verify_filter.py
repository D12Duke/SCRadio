"""Verify the lock-red color filter against recent snapshots."""
import glob
import os
from PIL import Image
import numpy as np

R_MIN, R_MAX, G_MAX, B_MAX = 175, 230, 65, 65
print("Filter: R in [%d..%d], G<=%d, B<=%d" % (R_MIN, R_MAX, G_MAX, B_MAX))
print()
files = sorted(glob.glob("tshots/*context*.png"))[-12:]
for fn in files:
    img = np.array(Image.open(fn).convert("RGB"))
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    mask = (r >= R_MIN) & (r <= R_MAX) & (g <= G_MAX) & (b <= B_MAX)
    count = int(mask.sum())
    if count > 0:
        avg = (int(r[mask].mean()), int(g[mask].mean()), int(b[mask].mean()))
    else:
        avg = (0, 0, 0)
    print("%-40s lock_red=%-6d avg_rgb=%s" % (os.path.basename(fn), count, avg))
