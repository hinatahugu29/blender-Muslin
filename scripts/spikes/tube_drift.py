"""調査: 円柱の衝突マージンの中に置いた筒状の布が、重力 0 でも円柱のまわりを回り続けないか。

縫い目も曲げ制約も使わず、コアだけで調べる(円周方向につながった 1 枚の筒 + 円柱のコライダー)。
筒の静止形状の周を体の周より短くして(伸ばされた状態)、衝突の押し出しと伸びの引き戻しが
綱引きになる条件を作る。

使い方: python scripts/spikes/tube_drift.py
"""
import math
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon" / "muslin"))
import cloth_core  # noqa: E402


def cylinder_collider(radius, z0, z1, segments):
    pos, tris = [], []
    for k in range(segments):
        th = 2 * math.pi * k / segments
        pos += [radius * math.cos(th), radius * math.sin(th), z0, radius * math.cos(th), radius * math.sin(th), z1]
    for k in range(segments):
        k2 = (k + 1) % segments
        b0, t0, b1, t1 = 2 * k, 2 * k + 1, 2 * k2, 2 * k2 + 1
        tris += [(b0, b1, t1), (b0, t1, t0)]
    return pos, tris


def tube(radius, columns, rows, height):
    """円周方向にも続いた筒(columns 本の縦の列、rows 段)。(positions, edges, tris)"""
    pos = []
    for r in range(rows):
        for c in range(columns):
            th = 2 * math.pi * c / columns
            pos += [radius * math.cos(th), radius * math.sin(th), height * r / (rows - 1)]
    idx = lambda c, r: r * columns + (c % columns)
    edges, tris = [], []
    for r in range(rows):
        for c in range(columns):
            edges.append((idx(c, r), idx(c + 1, r)))
            if r + 1 < rows:
                edges.append((idx(c, r), idx(c, r + 1)))
                tris.append((idx(c, r), idx(c + 1, r), idx(c + 1, r + 1)))
                tris.append((idx(c, r), idx(c + 1, r + 1), idx(c, r + 1)))
    return pos, edges, tris


def run(label, cloth_rest_radius, cloth_pose_radius, thickness, friction, substeps, steps=300,
        iterations=10, cheb=0.98, post=3, collider_segments=64):
    pos, edges, tris = tube(cloth_rest_radius, 96, 12, 0.3)
    quads = cloth_core.bending_quads_from_triangles(tris)
    sim = cloth_core.ClothSim(pos, edges, quads, tris, [], 0.4, 0.0, 1e-3)
    scale = cloth_pose_radius / cloth_rest_radius
    p = np.array(sim.get_positions()).reshape(-1, 3)
    p[:, :2] *= scale
    sim.set_positions(p.ravel().tolist())
    cp, ct = cylinder_collider(0.1, -0.5, 0.8, collider_segments)
    sim.add_collider(cp, ct)
    out = []
    for s in range(steps):
        sim.step(1 / 24, 0.0, iterations, substeps, 0.08, (0.0, 0.0, 0.0), False, 0.0, 0.3,
                 True, thickness, friction, False, 0.0, post, True, cheb, False)
        if s in (49, steps - 1):
            before = np.array(sim.get_positions()).reshape(-1, 3)
            sim.step(1 / 24, 0.0, iterations, substeps, 0.08, (0.0, 0.0, 0.0), False, 0.0, 0.3,
                     True, thickness, friction, False, 0.0, post, True, cheb, False)
            now = np.array(sim.get_positions()).reshape(-1, 3)
            v = (now - before) * 24
            lz = float((now[:, 0] * v[:, 1] - now[:, 1] * v[:, 0]).mean())
            r = np.hypot(now[:, 0], now[:, 1])
            out.append(f"step {s + 1}: 最大速度 {np.linalg.norm(v, axis=1).max() * 100:6.2f} cm/s  "
                       f"Lz {lz:+.5f}  半径 {r.min():.4f}〜{r.max():.4f}")
    print(f"{label}\n   " + "\n   ".join(out), flush=True)


if __name__ == "__main__":
    t = 0.0076
    # 布の周が体(マージン込み)の周より短い = 伸ばされて、衝突の押し出しと引き戻しが綱引きになる
    run("A 伸ばされた筒(静止半径 0.095 → 0.105)、摩擦 0.8、High", 0.095, 0.105, t, 0.8, 16)
    run("B 伸びていない筒(静止半径 = 0.108)、摩擦 0.8、High", 0.108, 0.108, t, 0.8, 16)
    run("C 伸ばされた筒、摩擦 0、High", 0.095, 0.105, t, 0.0, 16)
    run("D 伸ばされた筒、摩擦 0.8、Normal", 0.095, 0.105, t, 0.8, 8)
    run("E 伸ばされた筒、摩擦 0.8、High、円柱 256 角形", 0.095, 0.105, t, 0.8, 16, collider_segments=256)
