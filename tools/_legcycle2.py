import glob, os, sys
import numpy as np
from PIL import Image
d = sys.argv[1]
files = sorted(glob.glob(os.path.join(d, "frame_*.png")))
S = 64
arrs = []
for f in files:
    im = Image.open(f).convert("RGBA")
    a = np.array(im)
    al = a[:, :, 3] > 16
    ys, xs = np.where(al)
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    h = y1 - y0 + 1
    cy0 = y1 - int(h * 0.4)
    sub = a[cy0:y1+1, x0:x1+1].astype(np.float32)
    img = sub[:, :, :3] * (sub[:, :, 3:4] / 255.0)
    im2 = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    arrs.append(np.asarray(im2, dtype=np.float32).reshape(-1))
A = np.stack(arrs); n = len(A)
best = []
for lag in range(4, 41):
    best.append((float(np.abs(A[:n-lag]-A[lag:]).mean()), lag))
best.sort()
print("legs-only, short lags:")
for v, l in best[:12]:
    print("  lag %3d   %.3f" % (l, v))