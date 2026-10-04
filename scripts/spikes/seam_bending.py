"""調査: 縫い目をまたぐと曲げ抵抗が無いために、縫い目の位置で布が折れやすくならないか。

カンチレバー法(片端を固定して水平に突き出し、自重でどれだけ垂れるか)で、
- 1 枚の続いた短冊
- 同じ短冊を真ん中で 2 枚に分けて縫い合わせたもの(縫い目は距離の制約だけ)
を比べる。縫い目が曲げ抵抗を持たなければ、2 枚のほうが垂れる。

使い方: python scripts/spikes/seam_bending.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon" / "muslin"))
sys.path.insert(0, str(REPO / "scripts"))
import cloth_core  # noqa: E402
import seam_bending  # noqa: E402
from verify_core import build_grid  # noqa: E402

EDGE, NY, CLAMP = 0.005, 9, 7
SUBSTEPS = (8, 32, 64)


def strip(nx_total, split_at=None):
    """短冊。split_at を指定すると、その列で 2 枚に分けて縫う(縫い目の頂点は同じ位置に重ねる)。"""
    if split_at is None:
        pos, edges, _b, tris, _t = build_grid(nx_total, NY, EDGE)
        return pos, edges, tris, [], nx_total

    a_nx = split_at + 1
    b_nx = nx_total - split_at
    pa, ea, _b, ta, _ = build_grid(a_nx, NY, EDGE)
    pb, eb, _b, tb, _ = build_grid(b_nx, NY, EDGE)
    # 2 枚目を、1 枚目の右端の位置から始める(縫い目の頂点どうしは同じ位置)
    for k in range(0, len(pb), 3):
        pb[k] += split_at * EDGE
    n_a = len(pa) // 3
    pos = pa + pb
    edges = ea + [(i + n_a, j + n_a) for i, j in eb]
    tris = ta + [(i + n_a, j + n_a, k + n_a) for i, j, k in tb]
    seams = [(y * a_nx + (a_nx - 1), n_a + y * b_nx) for y in range(NY)]
    return pos, edges, tris, seams, nx_total


def droop(nx_total, split_at, compliance, substeps, seam_compliance=0.0, bend=False):
    pos, edges, tris, seams, _ = strip(nx_total, split_at)
    quads = cloth_core.bending_quads_from_triangles(tris)
    # 固定: 1 枚目の左の CLAMP 列
    a_nx = (split_at + 1) if split_at is not None else nx_total
    pinned = [y * a_nx + x for y in range(NY) for x in range(CLAMP)]
    sim = cloth_core.ClothSim(pos, edges, quads, tris, pinned, 0.15, 0.0, compliance)
    if seams:
        sim.set_seams(seams, seam_compliance)
        sim.set_seam_closure(0.0)
        if bend:
            # 縫い目をまたぐ曲げ制約(M10)。縫い目の辺は列 split_at
            a_nx = split_at + 1
            chain_a = [y * a_nx + split_at for y in range(NY)]
            chain_b = [s_[1] for s_ in seams]
            items, rest = seam_bending.rungs(seams, chain_a, chain_b, seam_bending.boundary_wings(tris), pos)
            sim.set_seam_bending(items, rest, compliance)
    for _ in range(300):
        sim.step(1.0 / 60.0, -9.81, 20, substeps, 0.6, (0.0, 0.0, 0.0),
                 False, 0.0, 0.3, False, 0.0, 0.3, False, 0.0, 2, False)
    p = sim.get_positions()
    if not sim.is_finite():
        return None, None
    # 先端(最後の頂点の列)の垂れと、縫い目付近の折れ(縫い目の両側 1 辺の角度)
    tip = [(len(p) // 3 - 1) - y * (nx_total - split_at if split_at is not None else nx_total) for y in range(NY)]
    tip_z = -sum(p[i * 3 + 2] for i in tip) / len(tip)
    return tip_z / ((nx_total - CLAMP) * EDGE), p


def main():
    nx = 41
    print(f"{'compliance':>10} {'substeps':>8} {'1枚':>8} {'縫い目のみ':>10} {'曲げ制約つき':>12} {'残る差':>8}", flush=True)
    for compliance in (0.0, 1e-3, 5e-3):
        for sub in SUBSTEPS:
            one, _ = droop(nx, None, compliance, sub)
            two, _ = droop(nx, 20, compliance, sub)
            fixed, _ = droop(nx, 20, compliance, sub, bend=True)
            fmt = lambda v: ('%.3f' % v) if v is not None else 'NaN'
            diff = None if one is None or fixed is None else fixed - one
            print(f"{compliance:>10} {sub:>8} {fmt(one):>8} {fmt(two):>10} {fmt(fixed):>12} "
                  f"{('%+.3f' % diff) if diff is not None else '':>8}", flush=True)


if __name__ == "__main__":
    main()
