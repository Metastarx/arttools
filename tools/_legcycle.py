import glob, os, sys
import numpy as np
from PIL import Image

d = sys.argv[1]
files = sorted(glob.glob(os.path.join(d, "frame_*.png")))
S = 64
crop = float(sys.argv[2]) if len(sys.argv) > 2 else 0.35

arrs = []
for f in files:
    im = Image.open(f).convert("RGBA")
    a = np.array(im)
    al = a[:, :, 3] > 16
    ys, xs = np.where(al)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    h = y1 - y0 + 1
    # bottom slice of the content only (legs / feet)
    cy0 = y1 - int(h * crop)
    sub = a[cy0:y1+1, x0:x1+1].astype(np.float32)
    rgb = sub[:, :, :3]
    alpha = sub[:, :, 3:4] / 255.0
    img = rgb * alpha
    im2 = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    arrs.append(np.asarray(im2, dtype=np.float32).reshape(-1))

A = np.stack(arrs)
n = len(A)
res = []
for lag in range(4, 71):
    a = A[:n-lag]
    b = A[lag:]
    res.append((float(np.abs(a-b).mean()), lag))
res.sort()
print("legs-only autocorrelation (bottom %.0f%%), %d frames" % (crop*100, n))
for v, l in res[:10]:
    print("  lag %3d   %.3f" % (l, v))