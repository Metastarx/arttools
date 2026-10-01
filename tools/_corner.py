import numpy as np
from PIL import Image
for p in [r"E:\Unity\art\resource\image\20261001\20261001-160109_monster_broken_incense\monster_broken_incense_01.png",
          r"E:\Unity\art\resource\image\20261001\20261001-160109_monster_broken_incense\512\monster_broken_incense_01.png"]:
    im = Image.open(p).convert("RGBA")
    a = np.array(im)
    print(p)
    print("  size", im.size, "corner", a[0,0], "has_alpha0", bool((a[:,:,3]==0).any()), "green_px", int(((a[:,:,1]>200)&(a[:,:,0]<80)&(a[:,:,2]<80)).sum()))