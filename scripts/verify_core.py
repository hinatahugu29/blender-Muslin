"""Blender を起動せずに cloth_core.pyd を検証するハーネス。

使い方:
    python scripts/verify_core.py            # 検証のみ
    python scripts/verify_core.py --bench    # 性能計測も実行

addon/muslin/cloth_core.pyd を直接 import するので、
Blender に入れる前にコアが正しいかをここで確認できる。
アドオンの Python モジュールについては構文チェック(compile)のみ行う
(bpy が無い環境では import できないため)。
"""

from __future__ import annotations

import argparse
import math
import os
import py_compile
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "addon" / "muslin"

sys.path.insert(0, str(ADDON_DIR))

try:
    import cloth_core  # type: ignore
except ImportError as exc:  # pragma: no cover
    print(f"[FAIL] cloth_core を import できません: {exc}")
    print("      先に scripts/build.ps1 を実行してください。")
    sys.exit(1)


results: list[tuple[str, bool, str]] = []
skipped: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  —  {detail}" if detail else ""))


def skip(name: str, reason: str) -> None:
    """検証できなかった項目を記録する。

    黙って飛ばすと「通った」と区別がつかないので、最後の集計にも出す。
    """
    skipped.append((name, reason))
    print(f"[SKIP] {name}  —  {reason}")


def has_numpy() -> bool:
    """numpy が使えるか。

    Blender には同梱されているが、このハーネスは素の Python で動くので
    入っているとは限らない。numpy を使う項目だけを切り分けるために見る。
    """
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


# ----------------------------------------------------------------- ヘルパー

def build_grid(nx: int, ny: int, spacing: float = 0.1):
    """平面グリッドの (positions, edges, bending_quads, triangles, top_row) を返す。

    曲げ制約の4頂点は三角形から導く(コア側の関数を使う)。
    """
    def vid(x, y):
        return y * nx + x

    positions = []
    for y in range(ny):
        for x in range(nx):
            positions.extend((x * spacing, y * spacing, 0.0))

    edges, tris = [], []
    for y in range(ny):
        for x in range(nx):
            if x + 1 < nx:
                edges.append((vid(x, y), vid(x + 1, y)))
            if y + 1 < ny:
                edges.append((vid(x, y), vid(x, y + 1)))
            if x + 1 < nx and y + 1 < ny:
                tris.append((vid(x, y), vid(x + 1, y), vid(x + 1, y + 1)))
                tris.append((vid(x, y), vid(x + 1, y + 1), vid(x, y + 1)))

    top_row = [vid(x, ny - 1) for x in range(nx)]
    bending = cloth_core.bending_quads_from_triangles(tris)
    return positions, edges, bending, tris, top_row


def distance(pos, i, j):
    return math.dist(pos[i * 3:i * 3 + 3], pos[j * 3:j * 3 + 3])


# --------------------------------------------------------------- 検証項目

def test_module_surface():
    ver = cloth_core.core_version()
    check("core_version が取得できる", isinstance(ver, str) and ver != "", f"v{ver}")
    check("hello()", cloth_core.hello().startswith("Hello"))
    check("add(1,2)", cloth_core.add(1.0, 2.0) == 3.0)


def test_free_fall():
    sim = cloth_core.ClothSim([0.0, 0.0, 0.0], [], [], [], [], 0.0)
    for _ in range(240):
        sim.step(1.0 / 240.0, -9.81, 10, 1, 0.0)
    z = sim.get_positions()[2]
    expected = -0.5 * 9.81
    check("自由落下が解析解に一致", abs(z - expected) < 0.05,
          f"z={z:.4f} / 理論値 {expected:.4f}")


def test_pin_and_stretch():
    positions, edges, bending, tris, top = build_grid(11, 11)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
    rest_top = [positions[i * 3:i * 3 + 3] for i in top]

    for _ in range(180):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01)

    pos = sim.get_positions()
    moved = max(math.dist(pos[i * 3:i * 3 + 3], r) for i, r in zip(top, rest_top))
    check("ピン留め頂点が動かない", moved < 1e-9, f"最大変位 {moved:.3e}")
    check("発散していない", sim.is_finite())

    err = sim.average_stretch_error()
    check("伸び誤差が小さい (< 5%)", err < 0.05, f"平均 {err * 100:.2f}%")

    lowest = min(pos[i * 3 + 2] for i in range(len(pos) // 3))
    check("布が重力で垂れ下がっている", lowest < -0.1, f"最下点 z={lowest:.3f}")


def test_substep_consistency():
    """減衰後の静止ドレープ形状が substeps に依存しない
    (= compliance が反復設定ではなく生地の物性として効いている)。
    揺れている途中の形はカオス的に分岐するので、静止形状で比較する。
    """
    def run(substeps):
        positions, edges, bending, tris, top = build_grid(9, 9)
        sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 1e-6, 1e-4)
        for _ in range(400):
            sim.step(1.0 / 60.0, -9.81, 10, substeps, 0.9)
        return sim.get_positions()

    a = run(2)
    b = run(8)
    diff = max(math.dist(a[i:i + 3], b[i:i + 3]) for i in range(0, len(a), 3))
    check("静止ドレープ形状が substeps に依存しない (< 1cm)", diff < 0.01, f"最大差 {diff:.5f} m")


def test_density_affects_wind_response():
    """同じ風でも密度の高い生地ほどはためきにくい。

    重力だけで吊るした布の垂れ方は物理的に質量に依存しない(自由落下と同じ)。
    質量が意味を持つのは「面積に働く力」= 風圧のとき。
    """
    def blown(density):
        positions, edges, bending, tris, top = build_grid(9, 9)
        sim = cloth_core.ClothSim(positions, edges, bending, tris, top, density, 0.0, 1e-4)
        for _ in range(120):
            sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (2.0, 0.0, 0.0))
        pos = sim.get_positions()
        return max(pos[i * 3] for i in range(len(pos) // 3))

    light = blown(0.05)
    heavy = blown(1.0)
    check("軽い生地ほど風で流される", light > heavy + 1e-3,
          f"軽 x={light:.4f} m vs 重 x={heavy:.4f} m")


def test_floor_collision():
    positions, edges, bending, tris, _ = build_grid(9, 9)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, [], 0.2, 0.0, 1e-4)
    for _ in range(240):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0), True, -1.0, 0.3)
    pos = sim.get_positions()
    lowest = min(pos[i * 3 + 2] for i in range(len(pos) // 3))
    check("床を突き抜けない", lowest >= -1.0 - 1e-6, f"最下点 z={lowest:.4f} / 床 -1.0")


def test_seam_closure():
    """離れた2枚の布片が縫い合わされて1つになる(M3 の核心)"""
    # 左右に離した 2 枚の短冊
    positions = []
    edges = []
    for piece, x0 in enumerate((-0.6, 0.3)):
        base = piece * 4
        for k in range(4):
            positions.extend((x0 + k * 0.1, 0.0, 0.0))
        for k in range(3):
            edges.append((base + k, base + k + 1))

    sim = cloth_core.ClothSim(positions, edges, [], [], [0], 0.0, 0.0, 0.0)
    sim.set_seams([(3, 4)], 0.0)  # 右端(3) と 左端(4) を縫う

    before = distance(sim.get_positions(), 3, 4)
    for i in range(120):
        sim.set_seam_closure(i / 119.0)
        sim.step(1.0 / 60.0, 0.0, 10, 4, 0.1)
    after = distance(sim.get_positions(), 3, 4)

    check("縫い目が閉じる", after < before * 0.1, f"隙間 {before:.3f} m -> {after:.4f} m")
    check("縫製後も発散しない", sim.is_finite())


def build_sphere(center, radius, segments=16, rings=12):
    """UV球の (positions, triangles) を返す。法線は外向き。"""
    cx, cy, cz = center
    positions = []
    for r in range(rings + 1):
        phi = math.pi * r / rings
        for s in range(segments):
            theta = 2.0 * math.pi * s / segments
            positions.extend((
                cx + radius * math.sin(phi) * math.cos(theta),
                cy + radius * math.sin(phi) * math.sin(theta),
                cz + radius * math.cos(phi),
            ))

    triangles = []
    for r in range(rings):
        for s in range(segments):
            s2 = (s + 1) % segments
            a, b = r * segments + s, r * segments + s2
            c, d = (r + 1) * segments + s2, (r + 1) * segments + s
            triangles.append((a, d, c))
            triangles.append((a, c, b))
    return positions, triangles


def test_object_collision():
    """球の上に落とした布が、どの瞬間も球を貫通しない(M2 Exit条件の中核)"""
    center = (0.4, 0.4, -0.3)
    radius = 0.25
    sphere_pos, sphere_tris = build_sphere(center, radius)

    positions, edges, bending, tris, _ = build_grid(15, 15, 0.06)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, [], 0.2, 0.0, 1e-4)
    idx = sim.add_collider(sphere_pos, sphere_tris)
    check("コライダーを登録できる", idx == 0 and sim.collider_count == 1,
          f"{sim.collider_triangle_count} 三角形")

    thickness = 0.008
    touched = False
    deepest = float("inf")
    for _ in range(180):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                 False, 0.0, 0.3, True, thickness, 0.3)
        touched = touched or sim.last_collision_count > 0
        pos = sim.get_positions()
        for k in range(len(pos) // 3):
            deepest = min(deepest, math.dist(pos[k * 3:k * 3 + 3], center))

    check("布が球に接触する", touched)
    check("布が球を貫通しない", deepest > radius - 0.01,
          f"中心からの最小距離 {deepest:.4f} / 半径 {radius}")
    check("コリジョン後も発散しない", sim.is_finite())


def test_animated_collider():
    """動くコライダーが布を押し上げる(BVH refit が効いている)"""
    sphere_pos, sphere_tris = build_sphere((0.5, 0.5, -1.0), 0.3, 12, 8)
    positions, edges, bending, tris, top = build_grid(11, 11)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
    sim.add_collider(sphere_pos, sphere_tris)

    center_vertex = 5 * 11 + 5
    before = sim.get_positions()[center_vertex * 3 + 2]

    for k in range(1, 61):
        dz = k * 0.015
        moved = list(sphere_pos)
        for i in range(2, len(moved), 3):
            moved[i] = sphere_pos[i] + dz
        sim.update_collider(0, moved)
        sim.step(1.0 / 60.0, 0.0, 10, 4, 0.01, (0.0, 0.0, 0.0),
                 False, 0.0, 0.3, True, 0.01, 0.3)

    after = sim.get_positions()[center_vertex * 3 + 2]
    check("動くコライダーが布を押し上げる", after > before + 0.05,
          f"z {before:.4f} -> {after:.4f}")


def test_self_collision():
    positions, edges, bending, tris, top = build_grid(13, 13, 0.05)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
    for _ in range(180):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                 False, 0.0, 0.3, True, 0.005, 0.3, True, 0.02)
    err = sim.average_stretch_error()
    check("自己衝突を有効にしても布が破裂しない", sim.is_finite() and err < 0.05,
          f"伸び誤差 {err * 100:.2f}%")


def test_rewind_determinism():
    """set_positions で巻き戻し、同じフレーム数進めれば同じ結果になる"""
    positions, edges, bending, tris, top = build_grid(7, 7)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
    rest = sim.get_positions()

    def run60():
        for _ in range(60):
            sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01)
        return sim.get_positions()

    first = run60()
    sim.set_positions(rest)
    second = run60()
    diff = max(abs(a - b) for a, b in zip(first, second))
    check("巻き戻し後の再計算が完全に一致(決定的)", diff == 0.0, f"最大差 {diff:.3e}")


def test_error_handling():
    ok = False
    try:
        cloth_core.ClothSim([0.0, 1.0], [], [], [], [], 0.0)
    except ValueError:
        ok = True
    check("不正な positions 長で ValueError", ok)

    sim = cloth_core.ClothSim([0.0, 0.0, 0.0], [], [], [], [], 0.0)
    ok = False
    try:
        sim.set_positions([0.0, 0.0])
    except ValueError:
        ok = True
    check("長さ不一致の set_positions で ValueError", ok)

    # 範囲外インデックスは無視され、クラッシュしないこと
    sim = cloth_core.ClothSim([0.0, 0.0, 0.0], [(0, 99)], [], [], [], 0.0)
    sim.step(1.0 / 60.0, -9.81)
    check("範囲外インデックスでクラッシュしない", sim.is_finite())


def benchmark():
    print("\n---- 性能計測 (1フレーム = iterations 10 / substeps 4) ----")
    print(f"{'頂点数':>8} {'ms/frame':>10} {'相当fps':>10}")
    for n in (21, 41, 81, 121):
        positions, edges, bending, tris, top = build_grid(n, n, 1.0 / n)
        sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01)  # ウォームアップ

        frames = 20
        t0 = time.perf_counter()
        for _ in range(frames):
            sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01)
        ms = (time.perf_counter() - t0) / frames * 1000.0
        print(f"{n * n:>8} {ms:>10.2f} {1000.0 / ms:>10.1f}")

    print("\n---- コリジョンのコスト (81x81 = 6561頂点) ----")
    sphere_pos, sphere_tris = build_sphere((0.5, 0.5, -0.4), 0.3, 32, 24)

    def timed(label, use_collider, self_collision):
        positions, edges, bending, tris, top = build_grid(81, 81, 1.0 / 81)
        sim = cloth_core.ClothSim(positions, edges, bending, tris, top, 0.2, 0.0, 1e-4)
        if use_collider:
            sim.add_collider(sphere_pos, sphere_tris)
        args = (1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                False, 0.0, 0.3, use_collider, 0.005, 0.3, self_collision, 0.008)
        sim.step(*args)
        t0 = time.perf_counter()
        for _ in range(10):
            sim.step(*args)
        ms = (time.perf_counter() - t0) / 10 * 1000.0
        print(f"{label:<28} {ms:>8.2f} ms/frame  ({1000.0 / ms:>6.1f} fps)")

    timed("コリジョンなし", False, False)
    timed(f"球コライダー({len(sphere_tris)}三角形)", True, False)
    timed("自己衝突のみ", False, True)
    timed("両方", True, True)


def test_post_collision_correction():
    """衝突後の伸び補正が、貫通を増やさずに伸び誤差を減らすことを確認する"""
    center, radius = 0.5, 0.3
    sphere_center = (center, center, -0.35)
    sphere_pos, sphere_tris = build_sphere(sphere_center, radius)

    n, spacing = 21, 0.05
    positions, edges, bending, tris, _top = build_grid(n, n, spacing)

    def drape(post):
        sim = cloth_core.ClothSim(list(positions), edges, bending, tris, [], 0.2, 0.0, 1e-4)
        sim.add_collider(sphere_pos, sphere_tris)
        for _ in range(150):
            sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                     False, 0.0, 0.3, True, 0.01, 0.3, True, 0.02, post)
        p = sim.get_positions()
        deepest = min(
            math.dist(p[k * 3:k * 3 + 3], sphere_center) for k in range(len(p) // 3)
        )
        return sim.average_stretch_error(), deepest, sim.is_finite()

    err_off, deepest_off, finite_off = drape(0)
    err_on, deepest_on, finite_on = drape(4)

    check("衝突後補正を渡せる(pyo3 の引数)", finite_off and finite_on)
    check("衝突後補正で伸び誤差が減る", err_on < err_off,
          f"{err_off:.5f} -> {err_on:.5f}")
    check("衝突後補正で球に潜り込まない", deepest_on > radius - 1e-3,
          f"最小距離 {deepest_on:.5f} (補正なし {deepest_off:.5f} / 半径 {radius})")


def test_broadphase_cache():
    """広域探索のキャッシュが、結果を変えずに使えることを確認する"""
    center = (0.5, 0.5, -0.35)
    radius = 0.3
    sphere_pos, sphere_tris = build_sphere(center, radius)
    n, spacing = 21, 0.05
    positions, edges, bending, tris, _top = build_grid(n, n, spacing)

    def drape(cache):
        sim = cloth_core.ClothSim(list(positions), edges, bending, tris, [], 0.2, 0.0, 1e-4)
        sim.add_collider(sphere_pos, sphere_tris)
        deepest = float("inf")
        for _ in range(120):
            sim.step(1.0 / 60.0, -9.81, 2, 8, 0.01, (0.0, 0.0, 0.0),
                     False, 0.0, 0.3, True, 0.01, 0.3, False, 0.0, 2, cache)
            p = sim.get_positions()
            deepest = min(
                deepest,
                min(math.dist(p[k * 3:k * 3 + 3], center) for k in range(len(p) // 3)),
            )
        return sim.get_positions(), sim.average_stretch_error(), deepest

    pos_a, err_a, deep_a = drape(False)
    pos_b, err_b, deep_b = drape(True)

    check("キャッシュ有無で伸び誤差が一致する", abs(err_a - err_b) < 1e-6,
          f"{err_a:.6f} / {err_b:.6f}")
    max_diff = max(abs(a - b) for a, b in zip(pos_a, pos_b))
    check("キャッシュ有無で頂点位置が一致する", max_diff < 1e-6, f"最大差 {max_diff:.2e}")
    check("キャッシュで球に食い込まない", deep_b > deep_a - 1e-3,
          f"直接 {deep_a:.5f} / キャッシュ {deep_b:.5f}")


def test_timings():
    """時間内訳の計測が取れることを確認する"""
    positions, edges, bending, tris, _top = build_grid(11, 11, 0.1)
    sim = cloth_core.ClothSim(positions, edges, bending, tris, [], 0.2, 0.0, 1e-4)
    for _ in range(3):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01)

    t = sim.timings()
    check("内訳を取得できる", isinstance(t, dict) and "total" in t,
          f"{len(t)} 項目")
    check("合計が正の値になる", t["total"] > 0.0, f"{t['total']:.4f} ms")

    parts = sum(t[k] for k in
                ("integrate", "stretch", "bending", "seam", "floor",
                 "object_collision", "self_collision", "post_collision", "velocity"))
    check("内訳の合計が全体を超えない", parts <= t["total"] * 1.05 + 1e-6,
          f"内訳 {parts:.4f} ms / 全体 {t['total']:.4f} ms")


def test_bending_stiffness():
    """曲げのコンプライアンスが剛性として効くことを、カンチレバー法で確認する

    短冊の片端を固定して水平に突き出し、自重でどれだけ垂れるかを測る。
    曲げ剛性は Substeps に強く依存するので、差が見える設定で試す。
    """
    nx, ny, edge, clamp = 41, 9, 0.005, 7
    positions, edges, _bend, tris, _top = build_grid(nx, ny, edge)
    quads = cloth_core.bending_quads_from_triangles(tris)
    check("曲げの4頂点を導ける", len(quads) > 0, f"{len(quads)} 組")

    def vid(x, y):
        return y * nx + x

    pinned = [vid(x, y) for y in range(ny) for x in range(clamp)]
    tip = [vid(nx - 1, y) for y in range(ny)]
    overhang = (nx - clamp) * edge

    def droop(compliance):
        sim = cloth_core.ClothSim(
            list(positions), edges, quads, tris, pinned, 0.15, 0.0, compliance
        )
        for _ in range(300):
            sim.step(1.0 / 60.0, -9.81, 20, 32, 0.6, (0.0, 0.0, 0.0),
                     False, 0.0, 0.3, False, 0.0, 0.3, False, 0.0, 2, False)
        if not sim.is_finite():
            return None
        p = sim.get_positions()
        return -sum(p[i * 3 + 2] for i in tip) / len(tip) / overhang

    stiff = droop(0.0)
    soft = droop(0.3)
    check("硬い設定で発散しない", stiff is not None)
    check("柔らかい設定で発散しない", soft is not None)
    if stiff is None or soft is None:
        return

    check("完全剛体なら垂れきらない", stiff < 0.85, f"突き出し長の {stiff * 100:.0f}%")
    check("compliance が剛性として効く", soft > stiff + 0.1,
          f"硬い {stiff:.3f} / 柔らかい {soft:.3f}")


def test_seam_logic():
    """シームのチェーン抽出とペアリング(bpy 非依存の純粋ロジック)を検証する"""
    import seams  # addon/muslin/seams.py

    # --- チェーン分割 ---
    # 2本の独立した直線チェーン: 0-1-2-3 と 10-11-12
    edges = [(0, 1), (1, 2), (2, 3), (10, 11), (11, 12)]
    chains = seams.split_edges_into_chains(edges)
    check("2本のエッジ列を分離できる", len(chains) == 2, f"{len(chains)} 本検出")
    check("チェーンが端から順序付けられる",
          chains[0] == [0, 1, 2, 3] and chains[1] == [10, 11, 12],
          f"{chains}")

    # 順序をシャッフルしても同じ結果になること
    shuffled = [(2, 3), (11, 12), (1, 2), (10, 11), (0, 1)]
    check("エッジの入力順に依存しない",
          seams.split_edges_into_chains(shuffled) == chains)

    # 分岐のあるエッジ列は除外される
    branched = [(0, 1), (1, 2), (1, 3)]
    check("分岐したエッジ列は採用しない", seams.split_edges_into_chains(branched) == [])

    # 閉ループも扱える
    loop = [(0, 1), (1, 2), (2, 3), (3, 0)]
    loop_chains = seams.split_edges_into_chains(loop)
    check("閉ループを1本のチェーンにできる",
          len(loop_chains) == 1 and len(loop_chains[0]) == 4, f"{loop_chains}")

    # --- ペアリング(頂点数が違うチェーン同士) ---
    # A: x=0 に4頂点、B: x=1 に3頂点。どちらも z 方向に 0〜0.3
    positions = [0.0] * (7 * 3)
    chain_a = [0, 1, 2, 3]
    chain_b = [4, 5, 6]
    for k, v in enumerate(chain_a):
        positions[v * 3] = 0.0
        positions[v * 3 + 2] = k * 0.1
    for k, v in enumerate(chain_b):
        positions[v * 3] = 1.0
        positions[v * 3 + 2] = k * 0.15

    pairs = seams.pair_chains(chain_a, chain_b, positions)
    check("頂点数が違うチェーンでも全頂点が縫われる",
          {p[0] for p in pairs} == set(chain_a) and {p[1] for p in pairs} == set(chain_b),
          f"{len(pairs)} ペア")
    check("端点同士が対応する",
          (chain_a[0], chain_b[0]) in pairs and (chain_a[-1], chain_b[-1]) in pairs,
          f"{pairs}")

    # --- 向きの自動判定 ---
    check("同じ向きなら反転しない",
          seams.should_flip(chain_a, chain_b, positions) is False)

    reversed_b = list(reversed(chain_b))
    check("逆向きのチェーンは反転が必要と判定される",
          seams.should_flip(chain_a, reversed_b, positions) is True)

    flipped_pairs = seams.pair_chains(chain_a, reversed_b, positions, flipped=True)
    check("反転すると正しく端点同士が対応する",
          (chain_a[0], chain_b[0]) in flipped_pairs
          and (chain_a[-1], chain_b[-1]) in flipped_pairs,
          f"{flipped_pairs}")

    # --- 弧長 ---
    length = seams.seam_length(chain_a, positions)
    check("縫い目の長さを計算できる", abs(length - 0.3) < 1e-9, f"{length:.4f} m")

    # 退化ケース(全頂点が同じ位置)でもクラッシュしない
    degenerate = [0.0] * 9
    check("退化したチェーンでもクラッシュしない",
          isinstance(seams.pair_chains([0, 1], [1, 2], degenerate), list))


def test_transform():
    """ローカル<->ワールドの座標変換(bpy 非依存)を検証する"""
    if not has_numpy():
        # transform.py は numpy 実装なので、無い環境では検証しようがない。
        # Blender 内では同梱の numpy が使われるため、この経路は
        # blender_selftest.py の「mesh_io の座標往復」でも押さえてある。
        skip(
            "座標変換 (transform.py)",
            "numpy が無い。python -m pip install numpy で入れてください",
        )
        return

    import transform  # addon/muslin/transform.py

    def reference(flat, m):
        """numpy 化する前の素の Python 実装(これと一致すれば等価)"""
        out = []
        for i in range(len(flat) // 3):
            x, y, z = flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2]
            for r in range(3):
                out.append(
                    m[r][0] * x + m[r][1] * y + m[r][2] * z + m[r][3]
                )
        return out

    identity = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    points = [1.0, 2.0, 3.0, -4.0, 0.5, 0.0]

    got = transform.transform_flat(points, identity)
    check("単位行列で座標が変わらない", got == points, f"{got}")

    # 平行移動 + 非一様スケール + せん断を含む一般の行列
    matrix = [
        [2.0, 0.1, 0.0, 5.0],
        [0.0, 3.0, -0.2, -1.0],
        [0.3, 0.0, 0.5, 2.5],
        [0.0, 0.0, 0.0, 1.0],
    ]
    got = transform.transform_flat(points, matrix)
    want = reference(points, matrix)
    max_diff = max(abs(a - b) for a, b in zip(got, want))
    check("素の Python 実装と一致する", max_diff < 1e-12, f"最大差 {max_diff:.2e}")

    check("頂点数が保たれる", len(got) == len(points), f"{len(got)} 要素")

    # ワールド往復: 逆行列を掛けたら元に戻る(write_positions_to_mesh の経路)
    import numpy as np

    inverse = np.linalg.inv(np.array(matrix, dtype=np.float64)).tolist()
    back = transform.transform_flat(got, inverse)
    max_diff = max(abs(a - b) for a, b in zip(back, points))
    check("逆行列で元の座標に戻る", max_diff < 1e-9, f"最大差 {max_diff:.2e}")

    # 空メッシュでも落ちない
    check("空の配列でもクラッシュしない", transform.transform_flat([], identity) == [])

    # 行列の回転部と平行移動部を正しく分離できる
    rotation, translation = transform.split_matrix(matrix)
    check("平行移動成分を取り出せる",
          list(translation) == [5.0, -1.0, 2.5], f"{list(translation)}")
    check("回転成分は 3x3 になる", rotation.shape == (3, 3), f"{rotation.shape}")


def test_cache_io():
    """ベイクキャッシュの読み書き(bpy 非依存)を検証する"""
    import shutil
    import tempfile

    import cache_io  # addon/muslin/cache_io.py

    directory = os.path.join(tempfile.mkdtemp(prefix="muslin_test_"), "cache")
    try:
        positions = [0.5, -1.25, 3.0, 10.0, 20.0, 30.0]

        cache_io.write_frame(directory, 7, positions)
        check("フレームを書き出せる", cache_io.has_frame(directory, 7))
        check("書き出していないフレームは無い", not cache_io.has_frame(directory, 8))

        loaded = cache_io.read_frame(directory, 7, expected_count=2)
        # float32 に落とすので厳密一致ではなく許容誤差で比較する
        close = all(abs(a - b) < 1e-6 for a, b in zip(positions, loaded))
        check("書き出した座標を読み戻せる", close and len(loaded) == 6, f"{loaded}")

        # 頂点数の食い違いを検出できるか(メッシュ編集後のベイク再利用を防ぐ)
        mismatch = False
        try:
            cache_io.read_frame(directory, 7, expected_count=3)
        except cache_io.CacheError:
            mismatch = True
        check("頂点数の不一致を検出する", mismatch)

        # 存在しないフレーム
        missing = False
        try:
            cache_io.read_frame(directory, 999)
        except cache_io.CacheError:
            missing = True
        check("欠けたフレームで CacheError", missing)

        # 壊れたファイル
        with open(cache_io.frame_path(directory, 9), "wb") as handle:
            handle.write(b"GARBAGE!")
        corrupt = False
        try:
            cache_io.read_frame(directory, 9)
        except cache_io.CacheError:
            corrupt = True
        check("壊れたファイルで CacheError", corrupt)

        # 途中で切れたファイル(ヘッダは正しいがデータ不足)
        import struct
        with open(cache_io.frame_path(directory, 10), "wb") as handle:
            handle.write(struct.pack(cache_io.HEADER_FORMAT, cache_io.MAGIC, cache_io.VERSION, 100))
            handle.write(b"\x00" * 12)
        truncated = False
        try:
            cache_io.read_frame(directory, 10)
        except cache_io.CacheError:
            truncated = True
        check("データ不足のファイルで CacheError", truncated)

        # メタ情報
        cache_io.write_info(directory, {"frame_start": 1, "frame_end": 7})
        info = cache_io.read_info(directory)
        check("メタ情報を保存・読み出しできる",
              info is not None and info["frame_end"] == 7, f"{info}")

        size = cache_io.cache_size_bytes(directory)
        check("キャッシュサイズを取得できる", size > 0, f"{size} bytes")

        # 自分が作ったファイルだけを消し、無関係なファイルは残すこと
        user_file = os.path.join(directory, "user_notes.txt")
        with open(user_file, "w", encoding="utf-8") as handle:
            handle.write("do not delete")
        cache_io.clear_cache(directory)
        check("キャッシュを削除できる", not cache_io.has_frame(directory, 7))
        check("無関係なファイルは削除しない", os.path.isfile(user_file))

        # 存在しないディレクトリでもクラッシュしない
        check("存在しないディレクトリの削除でもクラッシュしない",
              cache_io.clear_cache(os.path.join(directory, "nope")) == 0)
    finally:
        shutil.rmtree(os.path.dirname(directory), ignore_errors=True)


def compile_addon_modules():
    """bpy 依存モジュールの構文チェック(import はできないので compile のみ)"""
    ok = True
    for path in sorted(ADDON_DIR.glob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True, cfile=str(path.with_suffix(".pyc.tmp")))
            Path(str(path.with_suffix(".pyc.tmp"))).unlink(missing_ok=True)
        except py_compile.PyCompileError as exc:
            ok = False
            print(f"       {path.name}: {exc}")
    check("アドオン Python モジュールの構文チェック", ok)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", action="store_true", help="性能計測も実行する")
    args = parser.parse_args()

    print(f"cloth_core v{cloth_core.core_version()}  (Python {sys.version.split()[0]})\n")

    test_module_surface()
    test_free_fall()
    test_pin_and_stretch()
    test_substep_consistency()
    test_density_affects_wind_response()
    test_floor_collision()
    test_object_collision()
    test_animated_collider()
    test_self_collision()
    test_seam_closure()
    test_rewind_determinism()
    test_error_handling()
    test_post_collision_correction()
    test_broadphase_cache()
    test_bending_stiffness()
    test_timings()
    test_seam_logic()
    test_cache_io()
    test_transform()
    compile_addon_modules()

    if args.bench:
        benchmark()

    failed = [name for name, ok, _ in results if not ok]
    summary = f"\n==== {len(results) - len(failed)}/{len(results)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    print(summary + " ====")
    for name, reason in skipped:
        print(f"SKIPPED: {name} ({reason})")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
