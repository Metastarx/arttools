import os
from PIL import Image

root = r"E:\Unity\art\resource\video\20261001\20261001-141615_monster_paper_effigy\03_frames\DoubaoPLACEHOLDER"
model = "doubao-seedance-2-5-260628"
base = r"E:\Unity\art\resource\video\20261001\20261001-141615_monster_paper_effigy\03_frames"
clips = ["idle", "walk", "attack", "hit", "dead"]
picks = [3, 43, 3, 3, 3]
canvas_h = 620
canvas_w = 0
tiles = []
for clip, idx in zip(clips, picks):
    d = os.path.join(base, clip, model, "512")
    f = os.path.join(d, "frame_%03d.png" % idx)
    im = Image.open(f).convert("RGBA")
    tiles.append((clip, im))
    canvas_w += im.width + 20
out = Image.new("RGBA", (canvas_w, canvas_h), (40, 40, 48, 255))
x = 10
for clip, im in tiles:
    out.alpha_composite(im, (x, canvas_h - im.height))
    x += im.width + 20
out.save(r"E:\Unity\art\_compare_clips.png")
print("saved", out.size, [ (c, im.size) for c, im in tiles ])