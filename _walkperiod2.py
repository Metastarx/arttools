import glob, os
from PIL import Image
d = r"E:\Unity\art\resource\video\20261001\20261001-124832_hero_video\03_frames\walk\doubao-seedance-2-5-260628\512"
files = sorted(glob.glob(os.path.join(d, "frame_*.png")))
px = [list(Image.open(f).convert("RGBA").resize((96, 96)).getdata()) for f in files]
def diff(a, b):
    s = 0
    for (r1,g1,b1,a1),(r2,g2,b2,a2) in zip(a,b):
        s += abs(r1*a1-r2*a2)+abs(g1*a1-g2*a2)+abs(b1*a1-b2*a2)
    return s/(len(a)*3*255)
N=len(px)
for L in range(2, 31):
    tot=0.0;n=0
    for k in range(0,N-L):
        tot+=diff(px[k],px[k+L]); n+=1
    print(L, round(tot/n,3))
