"""Verify the new center-hole exclusion against recent lostlock snapshots.
Compares score WITH and WITHOUT the 40x40 center hole."""
import glob, os
import numpy as np
from PIL import Image
from scipy.ndimage import label as _cc_label

R_MIN, R_MAX, G_MAX, B_MAX = 175, 215, 50, 50
ASPECT_MAX = 18
SIZE_MAX = 400
HOLE = 40

def score(img, use_hole):
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    mask = (r >= R_MIN) & (r <= R_MAX) & (g <= G_MAX) & (b <= B_MAX)
    if use_hole:
        h, w = mask.shape
        cy, cx = h // 2, w // 2
        mask[max(0, cy - HOLE):cy + HOLE, max(0, cx - HOLE):cx + HOLE] = False
    raw = int(mask.sum())
    if raw == 0:
        return 0, raw
    labeled, n = _cc_label(mask, structure=np.ones((3, 3), dtype=np.int8))
    sizes = np.bincount(labeled.ravel())
    sizes[0] = 0
    kept = 0
    for lbl in range(1, n + 1):
        sz = int(sizes[lbl])
        if sz < 1 or sz > SIZE_MAX:
            continue
        ys, xs = np.where(labeled == lbl)
        bh = int(ys.max() - ys.min() + 1)
        bw = int(xs.max() - xs.min() + 1)
        asp = max(bw, bh) / max(min(bw, bh), 1)
        if asp > ASPECT_MAX:
            continue
        kept += sz
    return kept, raw

files = sorted(glob.glob("tshots/lostlock*search.png"))[-8:]
print("%-50s %-12s %-12s" % ("file", "no_hole", "with_hole"))
for fn in files:
    img = np.array(Image.open(fn).convert("RGB"))
    no_hole, _ = score(img, False)
    with_hole, _ = score(img, True)
    print("%-50s %-12d %-12d" % (os.path.basename(fn), no_hole, with_hole))
