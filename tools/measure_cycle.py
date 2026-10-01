"""measure_cycle.py - find the motion cycle length of a rendered clip.

The video pipeline renders about 119 frames of a looping motion, so one walk cycle
is roughly ten of them. Picking sprite frames needs that number: sample exactly one
cycle and the sheet loops cleanly; guess wrong and the character limps or pops.

The method is plain self-similarity. Every frame is downscaled to 96x96 and every
pair of frames L apart is compared as sum(abs(rgb_a * alpha_a - rgb_b * alpha_b)),
which ignores the transparent background and is stable against the slight camera
drift these clips have. Averaging over all pairs at a given L gives one score per
lag; low scores mean "these two frames show the same pose", so the lags with the
lowest scores are the cycle and its multiples.

Reported output: the eight lowest lags and a suggested cycle, which is the smallest
lag that is close to the best score (within --tolerance). A mirrored walk shows its
half-cycle too, and that is exactly the desired answer: a left-foot-forward drawing
and a right-foot-forward drawing are one half-cycle apart.

Usage:
    python tools/measure_cycle.py <frame_dir> [--top 8] [--max-lag 70] [--tolerance 1.12]

Exit code is 0 as long as the directory has frames; the numbers are the answer.
"""
import argparse
import glob
import os
import sys

from PIL import Image

SAMPLE = 96


def load_pixels(frame_dir):
    files = sorted(glob.glob(os.path.join(frame_dir, "frame_*.png")))
    if not files:
        raise SystemExit("no frame_*.png in " + frame_dir)
    pixels = []
    for path in files:
        img = Image.open(path).convert("RGBA").resize((SAMPLE, SAMPLE), Image.LANCZOS)
        pixels.append(img.getdata())
    return files, pixels


def frame_distance(a, b):
    total = 0
    for (r1, g1, b1, a1), (r2, g2, b2, a2) in zip(a, b):
        total += abs(r1 * a1 - r2 * a2) + abs(g1 * a1 - g2 * a2) + abs(b1 * a1 - b2 * a2)
    return total / (len(a) * 3 * 255.0)


def score_lag(pixels, lag):
    total = 0.0
    count = 0
    for i in range(0, len(pixels) - lag):
        total += frame_distance(pixels[i], pixels[i + lag])
        count += 1
    return total / count if count else 1.0


def main():
    parser = argparse.ArgumentParser(description="measure the motion cycle of a clip")
    parser.add_argument("frame_dir")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--max-lag", type=int, default=70)
    parser.add_argument("--tolerance", type=float, default=1.12,
                        help="a lag counts as the cycle if its score is within this "
                             "factor of the best score")
    args = parser.parse_args()

    files, pixels = load_pixels(args.frame_dir)
    n = len(pixels)
    limit = min(args.max_lag, n // 2)
    scores = [(lag, score_lag(pixels, lag)) for lag in range(2, limit + 1)]
    ranked = sorted(scores, key=lambda item: item[1])

    print("frames: %d  (compared at %dx%d, rgb weighted by alpha)" % (n, SAMPLE, SAMPLE))
    print("lowest average difference:")
    for lag, value in ranked[: args.top]:
        print("  lag %3d   %.4f" % (lag, value))

    best = ranked[0][1]
    suggestion = None
    for lag, value in ranked:
        if value <= best * args.tolerance:
            suggestion = lag
            break
    print("")
    print("suggested cycle: %s  (best score %.4f)" % (suggestion, best))
    print("pick-clip-frames -Period %s -Count <frames>" % suggestion)
    return 0


if __name__ == "__main__":
    sys.exit(main())