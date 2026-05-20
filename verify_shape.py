"""Score recent context PNGs with the NEW shape-filtered detection."""
import glob, os
import numpy as np
from PIL import Image
from scipy.ndimage import label as _cc_label

R_MIN, R_MAX, G_MAX, B_MAX = 175, 230, 65, 65
ASPECT_MAX = 10.0
SIZE_MAX = 400

def score(img):
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    mask = (r >= R_MIN) & (r <= R_MAX) & (g <= G_MAX) & (b <= B_MAX)
    raw = int(mask.sum())
    if raw == 0:
        return 0, 0, 0, 0.0
    labeled, n = _cc_label(mask, structure=np.ones((3, 3), dtype=np.int8))
    sizes = np.bincount(labeled.ravel())
    sizes[0] = 0
    kept = 0
    biggest = 0
    biggest_asp = 0.0
    for lbl in range(1, n + 1):
        sz = int(sizes[lbl])
        if sz <= 0: continue
        if sz > biggest: biggest = sz
        if sz > SIZE_MAX: continue
        ys, xs = np.where(labeled == lbl)
        h = int(ys.max() - ys.min() + 1)
        w = int(xs.max() - xs.min() + 1)
        asp = max(w, h) / max(min(w, h), 1)
        if asp > biggest_asp: biggest_asp = asp
        if asp > ASPECT_MAX: continue
        kept += sz
    return kept, raw, biggest, biggest_asp

files = sorted(glob.glob("tshots/*context*.png"))[-15:]
print("%-40s %-10s %-10s %-10s %-10s" % ("file", "kept", "raw", "biggest", "asp_max"))
for fn in files:
    img = np.array(Image.open(fn).convert("RGB"))
    k, raw, big, asp = score(img)
    print("%-40s %-10d %-10d %-10d %-10.1f" % (os.path.basename(fn), k, raw, big, asp))
