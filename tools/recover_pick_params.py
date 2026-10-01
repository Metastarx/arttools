import sys, os, glob, json
from PIL import Image
import numpy as np

def bbox_and_bytes(path):
    im = Image.open(path).convert("RGBA")
    a = np.array(im)
    al = a[:, :, 3] > 16
    ys, xs = np.where(al)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    return (x1 - x0 + 1, y1 - y0 + 1), a[y0:y1+1, x0:x1+1]

def main(src_dir, src_root, out_json):
    result = {}
    for clip in sorted(os.listdir(src_dir)):
        cdir = os.path.join(src_dir, clip)
        if not os.path.isdir(cdir):
            continue
        srcs = sorted(glob.glob(os.path.join(src_root, clip, "*", "frame_*.png")))
        if not srcs:
            continue
        cache = []
        for s in srcs:
            r = bbox_and_bytes(s)
            cache.append((os.path.basename(s), r))
        picks = []
        for f in sorted(glob.glob(os.path.join(cdir, "*.png"))):
            r = bbox_and_bytes(f)
            if r is None:
                picks.append(None); continue
            (w, h), arr = r
            found = None
            for name, cr in cache:
                if cr is None:
                    continue
                (cw, ch), carr = cr
                if cw != w or ch != h:
                    continue
                if carr.shape == arr.shape and np.array_equal(carr, arr):
                    found = name; break
            picks.append(found)
        result[clip] = picks
        print(clip, "->", picks)
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])