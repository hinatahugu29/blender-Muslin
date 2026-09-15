"""Quality プリセットの段を実測で決めるためのベンチ。

品質を 1 つのつまみにまとめるにあたり、各段の (iterations, substeps,
chebyshev_radius, post_collision_iterations) を当てずっぽうで決めたくない。
同じ場面を各設定で回し、

    - 1 フレームあたりの実時間
    - 平均伸び誤差
    - 垂れ比 (どれだけ垂れたか。硬い生地が硬く見えるか)

の 3 つを並べて、段の間で意味のある差が出る組み合わせを選ぶ。

使い方:
    blender --background --factory-startup --python scripts/bench_quality.py
    blender --background --factory-startup --python scripts/bench_quality.py -- --repeats 5

Blender 経由で走らせるのは、曲げ制約のトポロジをアドオン本体
(`mesh_io._collect_topology`) にそのまま作らせるため。ここを自前で
組み直すと、本番と違う本数の制約を測ってしまう。

計測の作法はこれまでと同じで、min-of-N を採る。平均は他プロセスの影響を
そのまま拾うので使わない。
"""

import argparse
import sys
import time
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "addon"))

from muslin import cloth_core, mesh_io  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 場面づくり

def make_cloth(side, size=1.0, z=1.0):
    """side x side のグリッドを Blender のメッシュとして作り、コアに渡す形にする。

    エッジ・曲げ・三角形の取り出しはアドオン本体の `_collect_topology` に
    任せる。ベンチ側で組み直すと本番と違うものを測ることになる。
    """
    step = size / (side - 1)
    verts = [(x * step, y * step, z) for y in range(side) for x in range(side)]
    faces = []
    for y in range(side - 1):
        for x in range(side - 1):
            i = y * side + x
            faces.append((i, i + 1, i + side + 1, i + side))

    name = f"bench_{side}"
    if name in bpy.data.meshes:
        bpy.data.meshes.remove(bpy.data.meshes[name])
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    edges, quads, triangles = mesh_io._collect_topology(mesh)
    positions = [c for v in verts for c in v]
    return positions, edges, quads, triangles


def pinned_corners(side):
    return [0, side - 1]


# ---------------------------------------------------------------- 計測

def run_once(setting, side, frames, fabric):
    positions, edges, quads, triangles = make_cloth(side)
    sim = cloth_core.ClothSim(
        positions,
        edges,
        quads,
        triangles,
        pinned_corners(side),
        fabric["density"],
        fabric["stretch_compliance"],
        fabric["bending_compliance"],
    )
    dt = 1.0 / 24.0

    start = time.perf_counter()
    for _ in range(frames):
        sim.step(
            dt,
            gravity=-9.81,
            iterations=setting["iterations"],
            substeps=setting["substeps"],
            damping=0.01,
            collision_enabled=False,
            self_collision_enabled=False,
            post_collision_iterations=setting["post_collision_iterations"],
            chebyshev_radius=setting["chebyshev_radius"],
        )
    elapsed = time.perf_counter() - start

    out = sim.get_positions()
    lowest = min(out[i * 3 + 2] for i in range(len(out) // 3))
    return {
        "ms_per_frame": elapsed / frames * 1000.0,
        "stretch_error": sim.average_stretch_error(),
        "droop": 1.0 - lowest,      # 開始 z=1.0 からどれだけ下がったか
        "finite": sim.is_finite(),
    }


def bench(setting, side, frames, fabric, repeats):
    runs = [run_once(setting, side, frames, fabric) for _ in range(repeats)]
    best = min(runs, key=lambda r: r["ms_per_frame"])
    # 物理量は設定が同じなら毎回同じはず。念のため確かめる。
    spread = max(r["droop"] for r in runs) - min(r["droop"] for r in runs)
    assert spread < 1e-9, f"決定的でない: 垂れ幅が {spread:.2e} ばらついた"
    return best


# ---------------------------------------------------------------- 候補

# 段の候補。Substeps を優先して上げる(実測で Iterations より約 3 倍効く)。
# Chebyshev は Iterations が 5 以下だと効かないので、そこは 0 にしてある。
CANDIDATES = [
    ("Draft",  dict(iterations=5,  substeps=2,  chebyshev_radius=0.0,  post_collision_iterations=1)),
    ("Normal", dict(iterations=10, substeps=4,  chebyshev_radius=0.0,  post_collision_iterations=2)),
    ("High",   dict(iterations=10, substeps=8,  chebyshev_radius=0.98, post_collision_iterations=3)),
    ("Final",  dict(iterations=15, substeps=16, chebyshev_radius=0.98, post_collision_iterations=4)),
]

FABRICS = {
    # 柔らかい生地。段ごとの伸び誤差の差を見る
    "cotton": dict(density=0.15, stretch_compliance=0.0, bending_compliance=0.02),
    # 硬い生地。SUBSTEPS_FOR_STIFF_FABRIC=16 の根拠を段でも確かめる
    "denim":  dict(density=0.40, stretch_compliance=0.0, bending_compliance=0.001),
}


# 段を決めるために掃く候補。Substeps を主に振る(実測で Iterations より
# 約 3 倍効く)。Chebyshev は Iterations が 5 以下では効かないので 0 にする。
SWEEP = [
    (it, sub, 0.0 if it <= 5 else 0.98)
    for it in (5, 10, 15)
    for sub in (2, 4, 8, 16, 32)
]


def run_sweep(args, fabric_name="cotton"):
    fabric = FABRICS[fabric_name]
    print(f"---- 掃引 ({fabric_name}) ----")
    print(f"{'it':>4}{'sub':>5}{'cheb':>6}{'ms/frame':>11}"
          f"{'伸び誤差':>12}{'垂れ':>9}")
    for it, sub, cheb in SWEEP:
        setting = dict(iterations=it, substeps=sub, chebyshev_radius=cheb,
                       post_collision_iterations=2)
        r = bench(setting, args.side, args.frames, fabric, args.repeats)
        flag = "" if r["finite"] else "  <-- 発散"
        print(f"{it:>4}{sub:>5}{cheb:>6.2f}{r['ms_per_frame']:>11.2f}"
              f"{r['stretch_error']:>12.5f}{r['droop']:>9.4f}{flag}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", type=int, default=41, help="グリッドの一辺の頂点数")
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--sweep", action="store_true",
                    help="段を決めるための候補を広く掃く")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = ap.parse_args(argv)

    verts = args.side * args.side
    print(f"cloth_core {getattr(cloth_core, '__version__', '?')} / "
          f"{cloth_core.thread_count()} スレッド")
    print(f"{args.side}x{args.side} = {verts} 頂点 / {args.frames} フレーム "
          f"/ min-of-{args.repeats}\n")

    if args.sweep:
        run_sweep(args)
        return

    for fabric_name, fabric in FABRICS.items():
        print(f"---- {fabric_name} "
              f"(bending_compliance={fabric['bending_compliance']}) ----")
        print(f"{'段':<8}{'it':>4}{'sub':>5}{'cheb':>6}{'post':>5}"
              f"{'ms/frame':>11}{'伸び誤差':>12}{'垂れ':>9}")
        rows = []
        for name, setting in CANDIDATES:
            r = bench(setting, args.side, args.frames, fabric, args.repeats)
            assert r["finite"], f"{name} が発散した"
            rows.append((name, setting, r))
            print(f"{name:<8}{setting['iterations']:>4}{setting['substeps']:>5}"
                  f"{setting['chebyshev_radius']:>6.2f}"
                  f"{setting['post_collision_iterations']:>5}"
                  f"{r['ms_per_frame']:>11.2f}{r['stretch_error']:>12.5f}"
                  f"{r['droop']:>9.4f}")

        base = rows[1][2]        # Normal を基準に相対で見る
        print(f"\n{'':8}Normal 比:  " + "  ".join(
            f"{n}={r['ms_per_frame'] / base['ms_per_frame']:.2f}x"
            for n, _, r in rows
        ))
        print()


if __name__ == "__main__":
    main()
