import glob, os, sys
from PIL import Image
d = r"E:\Unity\art\resource\video\20261001\20261001-124832_hero_video\03_frames\walk\doubao-seedance-2-5-260628"
files = sorted(glob.glob(os.path.join(d, "frame_*.png")))
print("frames", len(files))
for f in files:
    im = Image.open(f).convert("RGBA")
    w, h = im.size
    px = im.load()
    # content bounds
    minx, maxx, miny, maxy = w, -1, h, -1
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if px[x, y][3] > 32:
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    ch = maxy - miny + 1
    cw = maxx - minx + 1
    y0 = miny + int(ch * 0.52); y1 = miny + int(ch * 0.68)
    red = 0; tot = 0
    for y in range(y0, y1):
        for x in range(minx, maxx + 1):
            r, g, b, a = px[x, y]
            if a < 200: continue
            tot += 1
            if r > 110 and r > g + 45 and r > b + 45: red += 1
    print(os.path.basename(f), "red_frac=%.3f" % (red / max(tot, 1)), "tot=%d" % tot)
