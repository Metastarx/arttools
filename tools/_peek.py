import glob, os
import numpy as np
from PIL import Image

def bbox(path):
    a = np.array(Image.open(path).convert("RGBA"))
    al = a[:, :, 3] > 16
    ys, xs = np.where(al)
    return (xs.max()-xs.min()+1, ys.max()-ys.min()+1)

src = r"E:\Unity\Roguelike\ResourceSource\Normalized\Chars\Player\Default\Body\_src\walk"
for f in sorted(glob.glob(os.path.join(src, "*.png"))):
    print(os.path.basename(f), bbox(f))
print("--- source candidates")
sd = r"E:\Unity\art\resource\video\20261001\20261001-124832_hero_video\03_frames\walk"
for sub in ["doubao-seedance-2-5-260628", r"doubao-seedance-2-5-260628\512", r"doubao-seedance-2-5-260628\source_frames"]:
    g = sorted(glob.glob(os.path.join(sd, sub, "frame_*.png")))
    print(sub, len(g), [ (os.path.basename(x), bbox(x)) for x in g[:3] ])