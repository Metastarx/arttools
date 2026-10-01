import os
from PIL import Image

base = r"E:\Unity\art\resource\video\20261001\20261001-141615_monster_paper_effigy\03_frames"
model = "doubao-seedance-2-5-260628"
for clip in ("hit", "dead"):
    d = os.path.join(base, clip, model, "512")
    idxs = [3, 13, 23, 33, 43, 53, 63, 73, 83, 93, 103, 113, 118]
    tiles = [Image.open(os.path.join(d, "frame_%03d.png" % i)).convert("RGBA") for i in idxs]
    w = sum(t.width + 10 for t in tiles) + 10
    h = max(t.height for t in tiles) + 20
    out = Image.new("RGBA", (w, h), (40, 40, 48, 255))
    x = 10
    for t in tiles:
        out.alpha_composite(t, (x, h - t.height - 10))
        x += t.width + 10
    out.save(r"E:\Unity\art\_strip_%s.png" % clip)
    print(clip, out.size, [(os.path.basename(f), t.size) for f, t in zip(idxs, tiles)])