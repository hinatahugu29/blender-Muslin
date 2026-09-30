"""uid 運び(radius=uid, weight_softbody=検算値)が、補間で生まれた点を見分けられるかの確認。"""
import random
import numpy as np

f32 = np.float32


def _mix(u):
    """整数 → 0..1 の擬似乱数(非線形)。線形な検算値だと平均を取っても一致してしまう。"""
    x = (u * 0x9E3779B1) & 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & 0xFFFFFFFF
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & 0xFFFFFFFF
    x ^= x >> 16
    return x / 2**32


def chk(u):
    return float(f32(0.01 + 99.98 * _mix(u)))


def ok(r, w, tol=1e-3):
    if abs(r - round(r)) > 1e-4 or round(r) < 1:
        return False
    return abs(w - chk(int(round(r)))) < tol


assert all(ok(float(f32(u)), chk(u)) for u in range(1, 20000)), "正規の点が弾かれる"
random.seed(1)
for name, ts in (("中点", (0.5,)), ("1/3・2/3", (1 / 3, 2 / 3))):
    bad = n = 0
    for _ in range(200000):
        a, b = random.randint(1, 5000), random.randint(1, 5000)
        if a == b:
            continue
        for t in ts:
            r = a + (b - a) * t
            w = chk(a) + (chk(b) - chk(a)) * t
            n += 1
            bad += ok(float(f32(r)), float(f32(w)))
    print(f"{name}: 補間点を正規の点と誤認 {bad}/{n}")
