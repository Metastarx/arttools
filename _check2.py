from PIL import Image
import os, sys
base = sys.argv[1]
order = ["Idle", "Run", "Attack", "Hurt", "Dead"]
imgs = [Image.open(os.path.join(base, s + ".png")).convert("RGBA") for s in order]
w = sum(i.width for i in imgs) + 20 * (len(imgs) + 1)
h = max(i.height for i in imgs) + 20
out = Image.new("RGBA", (w, h), (40, 40, 48, 255))
x = 20
for im in imgs:
    out.alpha_composite(im, (x, h - im.height - 10))
    x += im.width + 20
out.thumbnail((2048, 2048), Image.LANCZOS)
out.save(sys.argv[2])
print(out.size)