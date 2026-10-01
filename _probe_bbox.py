import glob, os, statistics
from PIL import Image

def bbox(path):
    im = Image.open(path).convert("RGBA")
    return im.split()[3].getbbox()

root = r"E:\Unity\art\resource\video\20261001\20261001-141615_monster_paper_effigy\03_frames"
model = "doubao-seedance-2-5-260628"
for clip in ("idle", "walk", "attack", "hit", "dead"):
    src = sorted(glob.glob(os.path.join(root, clip, model, "source_frames", "*.png")))
    hs = []
    for f in src[3:119:12]:
        b = bbox(f)
        if b: hs.append(b[3] - b[1])
    print("%-7s n=%3d  height min %4d  median %4d  max %4d" % (clip, len(hs), min(hs), int(statistics.median(hs)), max(hs)))