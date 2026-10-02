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

# Windows のコンソールは既定が cp932 / cp1252 なので、日本語を print した
# 時点で UnicodeEncodeError で落ちる。呼び出し側が PYTHONIOENCODING を
# 立てているかに依存しないよう、ここで UTF-8 に寄せる。
# (verify_core.ps1 は立てているが、CI は python を直接叩くので落ちた)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

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


def test_set_reference():
    """型紙の差し替え(M8): 走らせたまま寸法が変わる"""
    positions, edges, bending, tris, top = build_grid(9, 9, 0.05)
    center = top[len(top) // 2]
    sim = cloth_core.ClothSim(positions, edges, bending, tris, [center], 0.2, 0.0, 1e-4)
    for _ in range(40):
        sim.step(1.0 / 24.0, -9.81, 10, 8, 0.5)
    wide = list(positions)
    for i in range(0, len(wide), 3):
        wide[i] *= 1.3
    sim.set_reference(wide)
    for _ in range(120):
        sim.step(1.0 / 24.0, -9.81, 10, 8, 0.5)
    pos = sim.get_positions()
    horizontal = [math.dist(pos[a * 3:a * 3 + 3], pos[b * 3:b * 3 + 3]) / 0.05
                  for a, b in edges if b == a + 1]
    ratio = sum(horizontal) / len(horizontal)
    check("型紙を横に 1.3 倍にすると走っている布の横の辺が伸びる",
          abs(ratio - 1.3) < 0.03 and sim.is_finite(), f"{ratio:.3f} 倍")
    try:
        sim.set_reference(wide[3:])
        check("頂点数が違う型紙は拒否する", False)
    except ValueError:
        check("頂点数が違う型紙は拒否する", True)


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

    # 上の吊り下げグリッドは自己接触を一度も起こさない(接触数 0 を実測)。
    # つまり「破裂しない」だけでは、自己衝突が何もしていなくても通る。
    # 重なった2枚を用意して、実際に押し離すことを直接確かめる。
    # 食い込みは浅くする。深いと押し出しがそのまま速度になり(分離速度 =
    # 食い込み / サブステップの dt)布が吹き飛ぶので、押し離せるかが見えない。
    nx = ny = 5
    spacing, thickness, gap = 0.05, 0.02, 0.018
    stacked, stacked_edges = [], []
    for layer in range(2):
        base = layer * nx * ny
        for y in range(ny):
            for x in range(nx):
                stacked.extend((x * spacing, y * spacing, layer * gap))
        for y in range(ny):
            for x in range(nx):
                i = base + y * nx + x
                if x + 1 < nx:
                    stacked_edges.append((i, i + 1))
                if y + 1 < ny:
                    stacked_edges.append((i, i + nx))

    def closest_between_layers(flat):
        half = nx * ny
        best = float("inf")
        for a in range(half):
            for b in range(half, 2 * half):
                best = min(best, math.dist(flat[a * 3:a * 3 + 3],
                                           flat[b * 3:b * 3 + 3]))
        return best

    def run_stacked(enabled):
        s = cloth_core.ClothSim(list(stacked), stacked_edges, [], [], [],
                                0.0, 0.0, 0.0)
        # 1ステップだけ見る。長く回すと押し出しで得た速度のぶん離れ続ける
        s.step(1.0 / 60.0, 0.0, 10, 4, 0.0, (0.0, 0.0, 0.0),
               False, 0.0, 0.3, False, 0.0, 0.3, enabled, thickness, 2, False)
        return closest_between_layers(s.get_positions())

    check("自己衝突なしなら重なったままである",
          abs(run_stacked(False) - gap) < 1e-6, f"{run_stacked(False):.5f} m")
    separated = run_stacked(True)
    check("重なった2枚を厚みまで押し離す",
          thickness <= separated < thickness * 2.0,
          f"{separated:.5f} m (厚み {thickness})")

    # 開始時点で深く食い込んでいると、押し出した距離がそのまま速度になって
    # 布が吹き飛ぶ。untangle は速度を発生させずに位置だけを直す。
    deep_gap = 0.004
    deep = []
    for layer in range(2):
        for y in range(ny):
            for x in range(nx):
                deep.extend((x * spacing, y * spacing, layer * deep_gap))

    def run_deep(use_untangle):
        s = cloth_core.ClothSim(list(deep), stacked_edges, [], [], [],
                                0.0, 0.0, 0.0)
        left = None
        if use_untangle:
            left = s.untangle(8, True, thickness, False, 0.0, False, 0.0)
        for _ in range(30):
            s.step(1.0 / 60.0, 0.0, 10, 4, 0.0, (0.0, 0.0, 0.0),
                   False, 0.0, 0.3, False, 0.0, 0.3, True, thickness, 2, False)
        return closest_between_layers(s.get_positions()), left

    flung, _ = run_deep(False)
    check("untangle 無しでは深い食い込みで吹き飛ぶ(前提の確認)",
          flung > thickness * 10, f"{flung:.4f} m まで離れた")

    settled, left = run_deep(True)
    check("untangle が食い込みを解消しきる", left == 0, f"残り {left} 箇所")
    check("untangle 後は吹き飛ばない", settled < thickness * 1.5,
          f"{settled:.5f} m (厚み {thickness})")


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
        for _ in range(220):
            # 床が無いと布が球から滑り落ちて自由落下し、「遠いから貫通して
            # いない」という無意味な判定になる。
            # 厚みは 0.05。伸び補正は厚みを大きく取ったときのための機能で、
            # 推奨の 0.4 x エッジ長(0.02)では押し出しが小さく差が出ない。
            sim.step(1.0 / 60.0, -9.81, 10, 4, 0.01, (0.0, 0.0, 0.0),
                     True, -0.80, 0.3, True, 0.01, 0.3, True, 0.05, post)
        p = sim.get_positions()
        deepest = min(
            math.dist(p[k * 3:k * 3 + 3], sphere_center) for k in range(len(p) // 3)
        )
        return sim.average_stretch_error(), deepest, sim.is_finite()

    err_off, deepest_off, finite_off = drape(0)
    err_on, deepest_on, finite_on = drape(4)

    check("衝突後補正を渡せる(pyo3 の引数)", finite_off and finite_on)
    check("衝突後補正で伸び誤差が減る", err_on < err_off * 0.8,
          f"{err_off:.5f} -> {err_on:.5f}")
    check("衝突後補正で球に潜り込まない", deepest_on > radius - 1e-3,
          f"最小距離 {deepest_on:.5f} (補正なし {deepest_off:.5f} / 半径 {radius})")
    # 飛んで行っても「遠いから貫通なし」になるので、載っていることも見る
    check("布が球の上に載っている(試験の成立条件)", deepest_on < radius * 1.5,
          f"最小距離 {deepest_on:.5f} / 半径 {radius}")


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


def test_grab():
    """布をつまむ: 幾何計算(bpy 非依存)とコアの set_grab"""
    import grab

    hit = grab.ray_plane((0, -5, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0))
    check("レイと平面の交点", hit is not None and math.dist(hit, (0, 0, 0)) < 1e-12, str(hit))
    check("平面と平行なレイは交わらない",
          grab.ray_plane((0, -5, 0), (1, 0, 0), (0, 0, 0), (0, 1, 0)) is None)
    check("後ろにある平面とは交わらない",
          grab.ray_plane((0, 5, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0)) is None)
    positions = [0, 0, 0, 1, 0, 0, 0, 1, 0]
    check("面の中で一番近い頂点を選ぶ", grab.nearest_vertex((0.9, 0.1, 0), [0, 1, 2], positions) == 1)
    target = grab.drag_target((1, 1, 1), (1.2, 1, 1), (1.7, 1, 1.5))
    check("つまんだ瞬間のずれを保って動かす",
          math.dist(target, (1.5, 1, 1.5)) < 1e-12, str(target))

    sim = cloth_core.ClothSim([0.0, 0.0, 0.0], [], [], [], [], 0.0)
    sim.set_grab(0, (0.0, 0.0, 0.5))
    check("つまんだ頂点を返す", sim.grabbed_vertex == 0)
    for _ in range(30):
        sim.step(1.0 / 60.0, -9.81, 10, 4, 0.5)
    z = sim.get_positions()[2]
    check("硬くつまむと目標に留まる(重力に負けない)", abs(z - 0.5) < 1e-6, f"z={z:.6f}")
    sim.clear_grab()
    check("離すとつまんでいない", sim.grabbed_vertex is None)
    try:
        sim.set_grab(5, (0.0, 0.0, 0.0))
        check("範囲外の頂点は拒否する", False)
    except ValueError:
        check("範囲外の頂点は拒否する", True)


def test_seam_codes():
    """辺の属性値から縫い目の頂点列を作り直す(bpy 非依存)"""
    import seams

    check("属性値は uid と側で決まり 0 と重ならない",
          seams.seam_code(1, 0) == 3 and seams.seam_code(1, 1) == 4
          and seams.seam_code(0, 0) != 0)
    # 辺の並びも向きもばらばら(統合・細分化の後はそうなる)
    edges = [(5, 4), (0, 1), (6, 5), (2, 1), (8, 9)]
    codes = [seams.seam_code(7, 1), seams.seam_code(7, 0), seams.seam_code(7, 1),
             seams.seam_code(7, 0), 0]
    side_a = seams.chains_from_codes(codes, edges, 7, 0)
    side_b = seams.chains_from_codes(codes, edges, 7, 1)
    check("A 側を1本の列にできる", side_a == [[0, 1, 2]], str(side_a))
    check("B 側を1本の列にできる", side_b == [[4, 5, 6]], str(side_b))
    check("ほかの縫い目の辺は混ざらない",
          seams.chains_from_codes(codes, edges, 8, 0) == [])
    # 4頂点の列の真ん中の辺が消えると2本に分かれる → 呼び出し側は壊れたとみなす
    b = seams.seam_code(7, 1)
    cut = seams.chains_from_codes([b, b], [(10, 11), (12, 13)], 7, 1)
    check("途切れた側は2本になる", len(cut) == 2, str(cut))


def test_pattern_uv():
    """型紙から UV を作る(bpy 非依存)"""
    if not has_numpy():
        skip("型紙から UV", "numpy が無い")
        return
    import numpy as np
    import pattern_uv

    # 立てて作った型紙 2 枚(XZ 平面。前身頃 0.5x0.7 と 小さな 0.3x0.2)
    def rect(w, h, y, nx=6, nz=8, x0=0.0):
        pts, edges = [], []
        for k in range(nz):
            for i in range(nx):
                pts.append((x0 + w * i / (nx - 1), y, h * k / (nz - 1)))
        for k in range(nz):
            for i in range(nx):
                a = k * nx + i
                if i + 1 < nx:
                    edges.append((a, a + 1))
                if k + 1 < nz:
                    edges.append((a, a + nx))
        return pts, edges

    p1, e1 = rect(0.5, 0.7, 0.0)
    p2, e2 = rect(0.3, 0.2, 0.3, x0=2.0)
    n1 = len(p1)
    pts = np.array(p1 + p2)
    edges = e1 + [(a + n1, b + n1) for a, b in e2]
    uvs, mpu = pattern_uv.layout(pts.ravel(), edges)

    check("UV は 0〜1 に収まる", uvs.min() >= -1e-9 and uvs.max() <= 1 + 1e-9,
          f"{uvs.min():.3f}〜{uvs.max():.3f}")
    e = np.array(edges)
    real = np.linalg.norm(pts[e[:, 0]] - pts[e[:, 1]], axis=1)
    on_uv = np.linalg.norm(uvs[e[:, 0]] - uvs[e[:, 1]], axis=1) * mpu
    check("UV 上の長さ × 縮尺 = 型紙の実寸(全ピース共通の縮尺)",
          np.allclose(real, on_uv, atol=1e-9), f"最大差 {np.abs(real - on_uv).max():.2e}m")
    top = int(np.argmax(pts[:n1, 2]))
    bottom = int(np.argmin(pts[:n1, 2]))
    check("立てた型紙は上が UV の上になる", uvs[top, 1] > uvs[bottom, 1])
    right = int(np.argmax(pts[:n1, 0]))
    left = int(np.argmin(pts[:n1, 0]))
    check("左右も反転しない", uvs[right, 0] > uvs[left, 0])

    def box(ids):
        return uvs[ids].min(axis=0), uvs[ids].max(axis=0)
    (a0, a1), (b0, b1) = box(list(range(n1))), box(list(range(n1, len(pts))))
    overlap = (a0 < b1).all() and (b0 < a1).all()
    check("ピースどうしが重ならない", not overlap)

    # 寝かせて作った型紙(XY 平面)でも実寸の比が保たれる
    flat = pts.copy()[:n1]
    flat[:, [1, 2]] = flat[:, [2, 1]]
    uvs2, mpu2 = pattern_uv.layout(flat.ravel(), e1)
    on_uv2 = np.linalg.norm(uvs2[np.array(e1)[:, 0]] - uvs2[np.array(e1)[:, 1]], axis=1) * mpu2
    check("寝かせた型紙でも実寸の比が保たれる",
          np.allclose(real[:len(e1)], on_uv2, atol=1e-9))

    # 前身頃と後ろ身頃: どちらも外から見て文字が正しく読める向きになる。
    # Add Pattern Piece と同じ巻き順(面の法線は -Y)で作り、胴を挟んで置く。
    # 後ろ身頃の法線は内側を向くが、外側は服の中心から離れる側で決める
    def tris(nx, nz, base):
        out = []
        for k in range(nz - 1):
            for i in range(nx - 1):
                a = base + k * nx + i
                out += [(a, a + 1, a + nx + 1), (a, a + nx + 1, a + nx)]
        return out

    pf, ef = rect(0.5, 0.7, -0.2, x0=-0.25)
    pb, eb = rect(0.5, 0.7, 0.2, x0=-0.25)
    nf = len(pf)
    garment = np.array(pf + pb)
    g_edges = ef + [(a + nf, b + nf) for a, b in eb]
    g_tris = tris(6, 8, 0) + tris(6, 8, nf)
    g_uv, _ = pattern_uv.layout(garment.ravel(), g_edges, garment.ravel(), g_tris)
    fr, fl = int(np.argmax(garment[:nf, 0])), int(np.argmin(garment[:nf, 0]))
    br, bl = nf + int(np.argmax(garment[nf:, 0])), nf + int(np.argmin(garment[nf:, 0]))
    check("前身頃は正面(-Y)から見て左右が正しい", g_uv[fr, 0] > g_uv[fl, 0])
    # 背中側(+Y)から見ると、画面の右はワールドの -X
    check("後ろ身頃は背中側(+Y)から見て左右が正しい", g_uv[bl, 0] > g_uv[br, 0])
    check("後ろ身頃も上が UV の上", g_uv[nf + int(np.argmax(garment[nf:, 2])), 1]
          > g_uv[nf + int(np.argmin(garment[nf:, 2])), 1])

    # 着せた形(円柱に巻いた形)で判定しても同じ向きになる
    dressed = garment.copy()
    for idx in range(len(dressed)):
        x, y = dressed[idx, 0], dressed[idx, 1]
        theta = x / 0.2 * (np.pi / 2)
        side = -1.0 if y < 0 else 1.0
        dressed[idx, 0] = 0.2 * np.sin(theta)
        dressed[idx, 1] = side * 0.2 * np.cos(theta)
    d_uv, _ = pattern_uv.layout(garment.ravel(), g_edges, dressed.ravel(), g_tris)
    check("着せた形で判定しても向きは同じ", np.allclose(d_uv, g_uv))

    # 単独の平らなピースは外側を決められないので、今までどおり
    single = np.array(pf)
    s_uv, _ = pattern_uv.layout(single.ravel(), ef, single.ravel(), tris(6, 8, 0))
    s_old, _ = pattern_uv.layout(single.ravel(), ef)
    check("外側を決められない単独のピースは従来どおり", np.allclose(s_uv, s_old))


def test_outline_edges():
    """型紙の外周の辺(面を1つしか持たない辺)の判定(bpy 非依存)"""
    if not has_numpy():
        skip("型紙の外周", "numpy が無い")
        return
    import rest_shape

    # 3x2 個の四角形(4x3 頂点)。辺は横 3x3 + 縦 4x2 = 17 本、外周は 10 本
    nx, ny = 4, 3
    edges = []
    index = {}
    for y in range(ny):
        for x in range(nx):
            i = y * nx + x
            if x + 1 < nx:
                index[(i, i + 1)] = len(edges)
                edges.append((i, i + 1))
            if y + 1 < ny:
                index[(i, i + nx)] = len(edges)
                edges.append((i, i + nx))
    loops = []
    for y in range(ny - 1):
        for x in range(nx - 1):
            a = y * nx + x
            for e in ((a, a + 1), (a + 1, a + 1 + nx), (a + nx, a + nx + 1), (a, a + nx)):
                loops.append(index[e])
    # 面に使われていない辺を1本足す(外周には入らない)
    edges.append((0, nx * ny - 1))
    outline = rest_shape.outline_edges(len(edges), loops)
    check("外周の辺だけを拾う", len(outline) == 10, f"{len(outline)} 本")
    inner = {index[(5, 6)], index[(1, 5)]}
    check("内側の辺とぶら下がった辺は拾わない",
          not inner & set(outline.tolist()) and len(edges) - 1 not in outline)


def test_bent_islands():
    """平らでない(曲げて置いた)ピースの判定(bpy 非依存)"""
    if not has_numpy():
        skip("平らでないピースの判定", "numpy が無い")
        return
    import rest_shape

    positions, edges, *_ = build_grid(5, 5)
    check("平らなピースは曲がっていない", rest_shape.bent_islands(positions, edges) == 0)
    curved = list(positions)
    for i in range(len(curved) // 3):
        x = curved[i * 3]
        curved[i * 3 + 2] = 0.3 * x * x            # 放物線に曲げる
    check("曲げたピースは曲がっている", rest_shape.bent_islands(curved, edges) == 1)
    # 平らなピースどうしが別々の平面にあっても、1枚ずつ見れば平ら
    two = list(positions) + [c + (1.0 if k % 3 == 2 else 0.0) for k, c in enumerate(positions)]
    n = len(positions) // 3
    two_edges = list(edges) + [(a + n, b + n) for a, b in edges]
    check("別々の平面にある平らなピースは曲がっていない",
          rest_shape.bent_islands(two, two_edges) == 0)
    tiny = [0, 0, 0, 1, 0, 0, 0, 1, 0.5]
    check("頂点の少ないまとまりは数えない", rest_shape.bent_islands(tiny, [(0, 1), (1, 2)]) == 0)


def test_pattern_mismatch():
    """型紙と今のメッシュの対応を調べる(bpy 非依存)"""
    if not has_numpy():
        skip("型紙の照合", "numpy が無い")
        return
    import rest_shape  # addon/muslin/rest_shape.py(numpy だけに依存)

    pattern = [0, 0, 0, 1, 0, 0, 1, 1, 0]
    edges = [(0, 1), (1, 2)]
    check("同じ形なら対応している",
          rest_shape.pattern_mismatch(pattern, pattern, edges) is None)

    draped = [0, 0, 0, 0.99, 0.1, 0, 1.0, 0.1, -1.0]   # 数 % の伸び縮みと曲がり
    check("着せて数 % ずれた形も対応している",
          rest_shape.pattern_mismatch(pattern, draped, edges) is None)

    broken = [0, 0, 0, 0, 0, 0, 1, 1, 0]     # 頂点を足して属性が 0 で埋まった状態
    check("長さ 0 の辺があれば対応していない",
          rest_shape.pattern_mismatch(broken, pattern, edges) is not None)

    stretched = [0, 0, 0, 3, 0, 0, 3, 1, 0]
    check("辺が倍を超えて違えば対応していない",
          rest_shape.pattern_mismatch(pattern, stretched, edges) is not None)

    check("頂点数が違えば対応していない",
          rest_shape.pattern_mismatch(pattern, pattern[:6], edges) is not None)


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


def test_step_call_matches_signature():
    """sim_state.py の sim.step(...) が Rust 側の引数の並びと一致すること。

    **この呼び出しは全部位置引数**なので、並びがずれても例外にならない。
    friction が floor_z に入る、というような形で黙って間違った値が渡る。
    しかも sim_state.py は bpy に依存するので import できず、構文チェック
    しか掛かっていない = Blender で再生するまで誰も気づけない。

    Rust 側(pyo3)は __text_signature__ を持っているので、呼び出し側を
    AST で読んで突き合わせれば、Blender 無しでもここだけは確かめられる。
    """
    import ast

    sig = cloth_core.ClothSim.step.__text_signature__
    if not sig:
        skip("step の引数の並びが呼び出し側と一致する", "__text_signature__ が無い")
        return
    # "($self, dt, gravity=..., ...)" -> 名前だけ取り出す
    inner = sig[sig.index("(") + 1:sig.rindex(")")]
    params = [p.split("=")[0].strip() for p in inner.split(",")]
    params = [p for p in params if p not in ("$self", "self")]

    tree = ast.parse((ADDON_DIR / "sim_state.py").read_text(encoding="utf-8"))

    def props_attr(node):
        """式の中から props.X の X を1つ取り出す(-abs(props.gravity) など)"""
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "props"):
                return sub.attr
        return None

    call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "step"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sim"):
            call = node
            break

    if call is None:
        check("step の引数の並びが呼び出し側と一致する", False)
        print("       sim_state.py に sim.step(...) が見つからない")
        return

    # 第1引数は dt(ローカル変数)。残りは props.X のはず
    actual = ["dt"] + [props_attr(a) for a in call.args[1:]]

    ok = actual == params
    check("step の引数の並びが呼び出し側と一致する", ok)
    if not ok:
        for i, (want, got) in enumerate(zip(params, actual)):
            if want != got:
                print(f"       {i} 番目: Rust は {want} / 呼び出しは {got}")
        if len(params) != len(actual):
            print(f"       引数の数が違う: Rust {len(params)} / 呼び出し {len(actual)}")


def test_panel_properties_exist():
    """panels.py が名前で参照しているプロパティが properties.py に在ること。

    `col.prop(props, "xxx")` の第2引数は**ただの文字列**なので、綴りを
    間違えても構文チェックは通る。Blender がパネルを描画したときに
    初めて壊れる = こちらも Blender 無しでは踏めない類。

    properties.py は bpy に依存して import できないので、両方 AST で読む。
    """
    import ast

    # properties.py 側: `name: bpy.props.XxxProperty(...)` の name を集める
    prop_tree = ast.parse((ADDON_DIR / "properties.py").read_text(encoding="utf-8"))
    defined = set()
    for node in ast.walk(prop_tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann = node.annotation
            # bpy.props.XxxProperty(...) の形だけ拾う
            if isinstance(ann, ast.Call):
                fn = ann.func
                if isinstance(fn, ast.Attribute) and fn.attr.endswith("Property"):
                    defined.add(node.target.id)

    if not defined:
        check("パネルが参照するプロパティが実在する", False)
        print("       properties.py からプロパティを1つも拾えなかった")
        return

    # panels.py 側: prop(props, "name") / prop_search なども含めて拾う
    panel_tree = ast.parse((ADDON_DIR / "panels.py").read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(panel_tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in ("prop", "prop_search")):
            continue
        if len(node.args) < 2:
            continue
        target, name = node.args[0], node.args[1]
        # props を対象にしたものだけ見る(scene や obj は対象外)
        if not (isinstance(target, ast.Name) and target.id == "props"):
            continue
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            continue
        if name.value not in defined:
            missing.append((name.value, node.lineno))

    ok = not missing
    check("パネルが参照するプロパティが実在する", ok,
          f"{len(defined)} 定義 / panels.py から参照")
    for name, line in missing:
        print(f"       panels.py:{line} の \"{name}\" が properties.py に無い")


# ------------------------------------------------------------ Curve Pattern

def _curve_rect(w, h, uids=(1, 2, 3, 4), piece_uid=1, hole_of=None, origin=(0.0, 0.0)):
    import numpy as np
    x0, y0 = origin
    co = np.array([[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]], dtype=float)
    return {"piece_uid": piece_uid, "uids": list(uids), "co": co, "hl": co.copy(),
            "hr": co.copy(), "hole_of": hole_of}


def test_curve_ids():
    """Curve の点の目印(radius + 検算値)と、記録した構造との比較(bpy 非依存)"""
    if not has_numpy():
        skip("Curve の点の目印", "numpy が無い")
        return
    import random
    import curve_ids as ci

    roundtrip = all(ci.decode(*ci.encode(u)) == u for u in list(range(1, 3000)) + [ci.MAX_UID - 1])
    check("目印は uid に復元できる", roundtrip)
    check("目印の範囲外や検算違いは未知の点",
          ci.decode(2.5, 50.0) is None and ci.decode(0.0, 50.0) is None
          and ci.decode(5.0, ci.encode(5)[1] + 1.0) is None)

    # 補間で生まれた点(隣り合う 2 点の線形補間)を既知の点と取り違えない。
    # 検算値が線形だと約 25% が通ってしまう(スパイクの実測)
    rng = random.Random(7)
    wrong = total = 0
    for _ in range(20000):
        a, b = rng.randint(1, 5000), rng.randint(1, 5000)
        if a == b:
            continue
        (ra, wa), (rb, wb) = ci.encode(a), ci.encode(b)
        for t in (0.5, 1 / 3, 2 / 3):
            total += 1
            wrong += ci.decode(ra + (rb - ra) * t, wa + (wb - wa) * t) is not None
    check("補間で生まれた点を既知の点と取り違えない", wrong <= 2, f"{wrong}/{total}")

    rec = [1, 2, 3, 4]
    d = ci.diff_outline(rec, [1, 2, 3, 4], True, True)
    check("構造が同じなら SAME", d["status"] == ci.SAME and not d["reasons"])
    d = ci.diff_outline(rec, [3, 4, 1, 2], True, True)
    check("cyclic の始点が回っても SAME(回転量を返す)", d["status"] == ci.SAME and d["shift"] == 2, str(d["shift"]))
    d = ci.diff_outline(rec, [4, 3, 2, 1], True, True)
    check("向きが逆でも SAME(反転を返す)", d["status"] == ci.SAME and d["reversed"])
    d = ci.diff_outline(rec, [2, 1, 3, 4], True, True)
    check("点の並びの入れ替えは REORDERED", d["status"] == ci.REORDERED)
    d = ci.diff_outline(rec, [1, 2, None, 3, 4], True, True)
    check("新しい点は NEW_POINTS(前後の既知の点を返す)",
          d["status"] == ci.NEW_POINTS and d["inserted"] == [(2, 2, 3)], str(d["inserted"]))
    d = ci.diff_outline(rec, [1, 2, 3, 3, 4], True, True)
    check("同じ点が 2 つなら DUPLICATED", d["status"] == ci.DUPLICATED and d["duplicated"] == [3])
    d = ci.diff_outline(rec, [1, 2, 4], True, True)
    check("点が無くなれば REMOVED", d["status"] == ci.REMOVED and d["removed"] == [3])
    d = ci.diff_outline(rec, [1, 2, 3, 4], True, False)
    check("開閉の変更は CYCLIC_CHANGED", d["status"] == ci.CYCLIC_CHANGED)
    d = ci.diff_outline(rec, [1, 2, 3, 4], False, False)
    d2 = ci.diff_outline(rec, [2, 3, 4, 1], False, False)
    check("開いた輪郭は回転を許さない", d["status"] == ci.SAME and d2["status"] == ci.REORDERED)
    d = ci.diff_outline(rec, [None, None, 3, 4], True, True)
    check("新しい点が端にあれば輪郭を回り込んで前後を探す", d["inserted"][0][1:] == (4, 3), str(d["inserted"]))

    rec_pieces = {1: [1, 2, 3, 4], 2: [10, 11, 12]}
    taken, new, gone = ci.match_pieces(rec_pieces, [[10, 11, None, 12], [4, 3, 2, 1]])
    check("ピースは所属する点の uid で対応付ける(spline の順が入れ替わっても)",
          taken == {0: 2, 1: 1} and not new and not gone, str(taken))
    taken, new, gone = ci.match_pieces(rec_pieces, [[1, 2, 3, 4], [50, 51, 52]])
    check("記録に無い spline は新しく、無くなったピースも分かる",
          new == [1] and gone == [2], f"new={new} gone={gone}")


def test_curve_eval():
    """Bezier の弧長評価(bpy 非依存)"""
    if not has_numpy():
        skip("Bezier の弧長評価", "numpy が無い")
        return
    import numpy as np
    import curve_eval as ce

    co = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    segs = ce.segments(co, co, co, True)
    check("直線の区間の長さ", abs(segs[0].length - 1.0) < 1e-12 and abs(segs[2].length - 2 ** 0.5) < 1e-9)
    p = segs[0].point_at([0.0, 0.25, 1.0])
    check("直線は弧長比に比例した位置", np.allclose(p, [[0, 0], [0.25, 0], [1, 0]], atol=1e-3))

    # 四分円(制御点で近似したベジェ)の長さは π/2 に近く、弧長比の等分が等間隔になる
    k = 0.5522847498
    seg = ce.Segment((1, 0), (1, k), (k, 1), (0, 1))
    check("四分円の弧長", abs(seg.length - np.pi / 2) < 1e-3, f"{seg.length:.5f}")
    pts = seg.point_at(np.linspace(0, 1, 11))
    gap = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    check("弧長比を等分すると点が等間隔", gap.max() / gap.min() < 1.003, f"比 {gap.max() / gap.min():.4f}")
    check("端点は制御点に一致", np.allclose(pts[0], [1, 0]) and np.allclose(pts[-1], [0, 1], atol=1e-9))
    open_segs = ce.segments(co, co, co, False)
    check("開いた輪郭は n-1 区間", len(open_segs) == 2 and len(segs) == 3)


def _convex_hull_area(pts):
    import numpy as np
    p = sorted(map(tuple, pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    h = np.array(lower[:-1] + upper[:-1])
    x, y = h[:, 0], h[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def test_delaunay2d():
    """Delaunay 三角形分割(bpy 非依存)"""
    if not has_numpy():
        skip("Delaunay 三角形分割", "numpy が無い")
        return
    import numpy as np
    import delaunay2d as dl

    rng = np.random.default_rng(0)
    pts = rng.random((200, 2))
    tris = dl.triangulate(pts)
    areas = dl.signed_areas(pts, tris)
    check("三角形はすべて反時計回りで面積が正", (areas > 0).all())
    # 凸包の面積と三角形の面積の総和が一致する(隙間も重なりも無い)
    hull = _convex_hull_area(pts)
    check("三角形の面積の総和 = 凸包の面積", abs(areas.sum() - hull) < 1e-9, f"{areas.sum():.6f} / {hull:.6f}")

    grid = np.array([(i, j) for i in range(12) for j in range(9)], dtype=float)
    tg = dl.triangulate(grid)
    check("格子(一直線に並ぶ点を含む)でも面積が合う",
          abs(dl.signed_areas(grid, tg).sum() - 88.0) < 1e-9 and len(tg) == 176)
    check("同じ入力なら同じ結果(決定的)", np.array_equal(dl.triangulate(pts), tris))

    outer = np.array([[0, 0], [4, 0], [4, 4], [0, 4]], dtype=float)
    hole = np.array([[1, 1], [3, 1], [3, 3], [1, 3]], dtype=float)
    q = np.array([[0.5, 0.5], [2, 2], [3.5, 3.5], [5, 5], [2, 0.9]])
    check("穴の内側は外、外周と穴のあいだは内(偶奇則)",
          list(dl.point_in_rings(q, [outer, hole])) == [True, False, True, False, True])
    check("輪郭までの距離", np.allclose(dl.distance_to_rings([[2, -1], [2, 2]], [outer]), [1.0, 2.0]))


def test_curve_discretize():
    """輪郭 → 三角形メッシュ(離散化)と、seam の区間の展開(bpy 非依存)"""
    if not has_numpy():
        skip("Curve の離散化", "numpy が無い")
        return
    import numpy as np
    import curve_discretize as cd
    import delaunay2d as dl

    m = cd.discretize([_curve_rect(1.0, 0.7)], 0.02)
    check("区間ごとの分割数 = round(長さ / 目標辺長)",
          m["segment_counts"] == {1: 50, 2: 35, 3: 50, 4: 35}, str(m["segment_counts"]))
    areas = dl.signed_areas(m["positions"], m["triangles"])
    check("三角形の面積の総和 = 型紙の面積", abs(areas.sum() - 0.7) < 1e-9 and (areas > 0).all(),
          f"{areas.sum():.6f}")
    ring = m["rings"][0]
    be = {tuple(e) for e in cd.boundary_edges(m["triangles"])}
    ring_edges = set()
    v = ring["vertices"]
    for k in range(len(v)):
        a, b = int(v[k]), int(v[(k + 1) % len(v)])
        ring_edges.add((min(a, b), max(a, b)))
    check("外周の辺 = 輪郭の辺(ちょうど 170 本)", be == ring_edges and len(be) == 170, str(len(be)))
    corners = m["positions"][[int(v[0]), int(v[50]), int(v[85]), int(v[135])]]
    check("制御点は必ず頂点になる",
          np.allclose(corners, [[0, 0], [1, 0], [1, 0.7], [0, 0.7]]))
    check("境界頂点は区間の uid と弧長比を持ち、内部は -1",
          (m["seg_start"][m["boundary"]] > 0).all() and (m["seg_start"][~m["boundary"]] == -1).all()
          and m["u"][m["boundary"]].min() == 0.0 and m["u"][m["boundary"]].max() < 1.0)
    again = cd.discretize([_curve_rect(1.0, 0.7)], 0.02)
    check("同じ輪郭なら同じメッシュ(決定的)",
          np.array_equal(m["positions"], again["positions"]) and np.array_equal(m["triangles"], again["triangles"]))

    # 穴のあるピース
    outer = _curve_rect(1.0, 1.0)
    hole = _curve_rect(0.4, 0.4, uids=(11, 12, 13, 14), piece_uid=2, hole_of=1, origin=(0.3, 0.3))
    mh = cd.discretize([outer, hole], 0.05)
    ah = dl.signed_areas(mh["positions"], mh["triangles"])
    check("穴のあるピース: 面積 = 外周 - 穴", abs(ah.sum() - (1.0 - 0.16)) < 1e-9, f"{ah.sum():.5f}")
    cen = mh["positions"][mh["triangles"]].mean(axis=1)
    in_hole = (cen[:, 0] > 0.3) & (cen[:, 0] < 0.7) & (cen[:, 1] > 0.3) & (cen[:, 1] < 0.7)
    check("穴の中に三角形が無い", not in_hole.any())
    check("穴の輪郭も辺として残る", len(cd.boundary_edges(mh["triangles"])) ==
          sum(mh["segment_counts"].values()))

    # 2 ピース(頂点番号がつながる)
    two = cd.discretize([_curve_rect(0.5, 0.5), _curve_rect(0.5, 0.5, uids=(21, 22, 23, 24),
                                                           piece_uid=2, origin=(2.0, 0.0))], 0.05)
    check("2 ピース: 頂点は通し番号でピースごとに分かれる",
          set(np.unique(two["piece"])) == {1, 2} and two["triangles"].max() == len(two["positions"]) - 1)

    try:
        bad = _curve_rect(1.0, 0.7)
        bad["co"][1] = bad["co"][0]
        bad["hl"], bad["hr"] = bad["co"].copy(), bad["co"].copy()
        cd.discretize([bad], 0.02)
        rejected = False
    except cd.DiscretizeError:
        rejected = True
    check("長さ 0 の区間は理由つきで失敗する", rejected)

    # seam の区間の展開
    def edges_of(*args):
        return cd.expand_range(ring, *args)

    whole = edges_of(1, 0.0, 2, 0.0)
    check("区間 1 まるごとの seam = その区間の辺 50 本(頂点は始点から終点まで)",
          len(whole) == 50 and whole[0][0] == int(v[0]) and whole[-1][1] == int(v[50]))
    check("複数の区間をまたぐ seam", len(edges_of(1, 0.0, 3, 0.0)) == 85)
    check("終点が始点より手前なら一周して回り込む", len(edges_of(4, 0.0, 2, 0.0)) == 85)
    half = len(edges_of(1, 0.5, 2, 0.5))
    check("区間の途中から途中まで(弧長比)", half in (42, 43), str(half))
    check("始点 = 終点なら一周", len(edges_of(1, 0.0, 1, 0.0)) == 170)
    check("隣り合う 2 本の seam は辺を共有しない",
          not (set(edges_of(1, 0.0, 2, 0.0)) & set(edges_of(2, 0.0, 3, 0.0))))
    check("知らない uid は空", edges_of(99, 0.0, 2, 0.0) == [])


def test_curve_update():
    """Shape Update: topology を保ったまま Curve に追従する(bpy 非依存)"""
    if not has_numpy():
        skip("Shape Update", "numpy が無い")
        return
    import numpy as np
    import curve_discretize as cd
    import curve_update as cu

    m = cd.discretize([_curve_rect(1.0, 0.7)], 0.02)
    gen = m["positions"]

    same = cu.update(m, {1: _curve_rect(1.0, 0.7)}, 0.02)
    check("Curve が変わらなければ座標も変わらない",
          same["status"] == cu.SHAPE_UPDATE and np.abs(same["positions"] - gen).max() == 0.0)

    # 幅 1.00m → 1.05m: 頂点数を変えずに追従する
    wide = cu.update(m, {1: _curve_rect(1.05, 0.7)}, 0.02)
    p = wide["positions"]
    check("幅 1.00 → 1.05m は Shape Update(頂点数・接続は不変)",
          wide["status"] == cu.SHAPE_UPDATE and len(p) == len(gen), str(wide["reasons"]))
    check("新しい寸法に追従する", abs(p[:, 0].max() - 1.05) < 1e-9 and abs(p[:, 1].max() - 0.7) < 1e-9)
    check("内部の頂点も伸びに追従する(一様な伸びからのずれが 1mm 未満)",
          np.abs(p[:, 0] - gen[:, 0] * 1.05).max() < 1e-3, f"{np.abs(p[:, 0] - gen[:, 0] * 1.05).max():.2e}")
    check("更新後の品質は許容内",
          wide["quality"]["inverted"] == 0 and 1.0 <= wide["quality"]["edge_ratio_max"] < 1.1)
    check("同じ入力なら同じ結果(決定的)",
          np.array_equal(p, cu.update(m, {1: _curve_rect(1.05, 0.7)}, 0.02)["positions"]))

    big = cu.update(m, {1: _curve_rect(3.0, 0.7)}, 0.02)
    check("大きな変更は Rebuild Required(座標は返さない)",
          big["status"] == cu.REBUILD_REQUIRED and big["positions"] is None and big["reasons"])
    check("目標辺長の変更は Rebuild Required",
          cu.update(m, {1: _curve_rect(1.0, 0.7)}, 0.03)["status"] == cu.REBUILD_REQUIRED)
    check("構造の変化を受け取ったら Rebuild Required",
          cu.update(m, {1: _curve_rect(1.0, 0.7)}, 0.02, ["点が増えた"])["reasons"] == ["点が増えた"])
    changed = cu.update(m, {1: _curve_rect(1.0, 0.7, uids=(1, 2, 3, 9))}, 0.02)
    check("生成時の区間が今の Curve に無ければ Rebuild Required",
          changed["status"] == cu.REBUILD_REQUIRED, str(changed["reasons"]))

    # 向きが逆になった輪郭・始点が回った輪郭でも同じ形に追従する(uid で対応するので)
    for label, order in (("向きが逆", [0, 3, 2, 1]), ("始点が回った", [2, 3, 0, 1])):
        o = _curve_rect(1.0, 0.7)
        o["uids"] = [o["uids"][k] for k in order]
        o["co"] = o["co"][order]
        o["hl"], o["hr"] = o["co"].copy(), o["co"].copy()
        r = cu.update(m, {1: o}, 0.02)
        check(f"輪郭の{label}でも、変わらなければ座標は同じ",
              r["status"] == cu.SHAPE_UPDATE and np.abs(r["positions"] - gen).max() < 1e-12)

    # 曲線(ハンドルつき)の輪郭でも動く。上辺を膨らませて、その後さらに膨らませる
    def bulged(amount):
        o = _curve_rect(1.0, 0.7)
        # 上辺(点 2 → 点 3)のハンドルを持ち上げる
        o["hr"][2] = o["co"][2] + [-0.3, amount]
        o["hl"][3] = o["co"][3] + [0.3, amount]
        return o

    mc = cd.discretize([bulged(0.1)], 0.02)
    up = cu.update(mc, {1: bulged(0.14)}, 0.02)
    check("曲線の輪郭でも Shape Update できる",
          up["status"] == cu.SHAPE_UPDATE and up["quality"]["inverted"] == 0, str(up["reasons"]))
    inside = up["positions"][~mc["boundary"]]
    check("内部の頂点は輪郭の内側に留まる",
          inside[:, 0].min() > 0 and inside[:, 0].max() < 1.0 and inside[:, 1].min() > 0)

    # 分割数が変わるほどの伸びでも、Shape Update の間は頂点数が同じ(Rebuild は別の判断)
    step = cu.update(m, {1: _curve_rect(1.09, 0.7)}, 0.02)
    check("分割数が変わるほどの伸びでも Shape Update の間は頂点数が同じ",
          step["status"] == cu.SHAPE_UPDATE and len(step["positions"]) == len(gen))


def test_curve_selection_runs():
    """選んだ点の連なり(縫い目の片側を選択から作る。bpy 非依存)"""
    import curve_ids as ci
    runs = ci.selection_runs
    check("選択が無ければ連なりは無い", runs([False] * 5, True) == [])
    check("離れた 2 か所は 2 本の連なり(輪郭に沿った順)",
          runs([True, True, False, True, True, False], False) == [[0, 1], [3, 4]])
    check("cyclic は始点をまたぐ連なりを 1 本にする",
          runs([True, False, False, True, True], True) == [[3, 4, 0]])
    check("開いた輪郭は始点をまたがない",
          runs([True, False, False, True, True], False) == [[0], [3, 4]])
    check("全部を選ぶと一周分の 1 本", runs([True] * 4, True) == [[0, 1, 2, 3]])
    check("1 点だけの連なりも返す(呼び出し側が 2 点以上を要求する)", runs([False, True, False], True) == [[1]])
    check("並びが 0 番始まりでない連なり", runs([False, True, True, False, True], False) == [[1, 2], [4]])


def test_curve_transfer():
    """Rebuild での姿勢の引き継ぎ(型紙空間を介した重心座標の補間。bpy 非依存)"""
    if not has_numpy():
        skip("姿勢の引き継ぎ", "numpy が無い")
        return
    import numpy as np
    import curve_discretize as cd
    import curve_transfer as ct

    def bend(xy):
        # 型紙(平ら)を円柱に巻き付けた姿勢: 幅 1.0 を 3/4 周
        r = 1.0 / (1.5 * np.pi)
        theta = (xy[:, 0] - 0.5) / r
        return np.column_stack([r * np.sin(theta), -r * np.cos(theta), xy[:, 1]])

    old = cd.discretize([_curve_rect(1.0, 0.7)], 0.03)
    pose = bend(old["positions"])
    new = cd.discretize([_curve_rect(1.0, 0.7)], 0.021)      # 辺の長さが違う(頂点数が違う)
    moved, outside = ct.transfer(old["positions"], old["triangles"], pose,
                                 new["positions"], old["piece"], new["piece"])
    expected = bend(new["positions"])
    err = np.linalg.norm(moved - expected, axis=1)
    check("頂点数が違うメッシュへ姿勢を写せる", len(moved) == len(new["positions"]) != len(old["positions"]))
    check("滑らかな姿勢はほぼそのまま写る(最大 3mm、平均 0.5mm 未満)",
          err.max() < 3e-3 and err.mean() < 5e-4, f"最大 {err.max() * 1000:.2f}mm / 平均 {err.mean() * 1000:.3f}mm")
    check("同じ輪郭なら旧メッシュの外側の頂点は(ほぼ)無い", outside <= 2, str(outside))
    same, _ = ct.transfer(old["positions"], old["triangles"], pose, old["positions"],
                          old["piece"], old["piece"])
    check("同じメッシュへ写せば元の姿勢に戻る", np.abs(same - pose).max() < 1e-9)

    # 輪郭が広がった分は旧メッシュの外側。縁へ寄せて補間し、壊れない
    wide = cd.discretize([_curve_rect(1.2, 0.7)], 0.03)
    moved, outside = ct.transfer(old["positions"], old["triangles"], pose, wide["positions"],
                                 old["piece"], wide["piece"])
    check("広がった分は外側として数える(姿勢は有限で、旧メッシュの範囲に収まる)",
          outside > 0 and np.isfinite(moved).all()
          and moved[:, 2].max() <= 0.7 + 1e-9 and np.abs(moved[:, :2]).max() <= pose[:, :2].max() * 1.001 + 1e-9,
          f"外側 {outside} / {len(wide['positions'])}")

    # 旧メッシュの型紙空間は「今の Curve の形」: 輪郭を動かした後の座標を返す
    def outline_of(w, h, uids=(1, 2, 3, 4)):
        o = _curve_rect(w, h, uids=uids)
        return {1: {"uids": list(uids), "co": o["co"], "hl": o["hl"], "hr": o["hr"]}}

    now = ct.old_pattern_now(old, outline_of(1.0, 0.7))
    check("Curve が変わっていなければ生成時の座標と同じ", now is not None and np.abs(now - old["positions"]).max() < 1e-9)
    now = ct.old_pattern_now(old, outline_of(1.2, 0.7))
    check("Curve を広げた後の旧メッシュの座標に追従する(幅 1.2m)",
          now is not None and abs(now[:, 0].max() - 1.2) < 1e-9 and len(now) == len(old["positions"]))

    # 区間の途中に点が足された(uid は未知 = None)。形は同じなので座標は変わらない
    sub = _curve_rect(1.0, 0.7)
    co = np.vstack([sub["co"][:2], [[1.0, 0.35]], sub["co"][2:]])
    cur = {1: {"uids": [1, 2, None, 3, 4], "co": co, "hl": co.copy(), "hr": co.copy()}}
    now = ct.old_pattern_now(old, cur)
    # 弧長の逆引きは折れ線(96 分割)なので、評価のしかたが変わると 0.03mm ほどずれる
    check("区間の途中に点が足されても、足された点をまたいで対応する",
          now is not None and np.abs(now - old["positions"]).max() < 1e-4)

    # 向きが逆の輪郭
    order = [0, 3, 2, 1]
    rev = _curve_rect(1.0, 0.7)
    rev_outline = {1: {"uids": [rev["uids"][k] for k in order], "co": rev["co"][order],
                       "hl": rev["co"][order], "hr": rev["co"][order]}}
    now = ct.old_pattern_now(old, rev_outline)
    check("輪郭の向きが逆でも対応する", now is not None and np.abs(now - old["positions"]).max() < 1e-9)

    # 旧メッシュの区間が見つからない(点が消えた)なら None(呼び出し側が生成時の座標で代用する)
    gone = _curve_rect(1.0, 0.7)
    cur = {1: {"uids": [1, 2, 4], "co": gone["co"][[0, 1, 3]], "hl": gone["co"][[0, 1, 3]],
               "hr": gone["co"][[0, 1, 3]]}}
    check("旧メッシュの点が消えていれば None", ct.old_pattern_now(old, cur) is None)

    # 2 ピース: 別のピースの三角形には写さない
    two = cd.discretize([_curve_rect(0.5, 0.5), _curve_rect(0.5, 0.5, uids=(21, 22, 23, 24),
                                                           piece_uid=2, origin=(2.0, 0.0))], 0.05)
    pose2 = np.column_stack([two["positions"][:, 0] * (1 + two["piece"]), two["positions"][:, 1],
                             two["piece"].astype(float)])
    moved2, _ = ct.transfer(two["positions"], two["triangles"], pose2, two["positions"],
                            two["piece"], two["piece"])
    check("ピースごとに写る(別ピースの姿勢が混ざらない)", np.abs(moved2 - pose2).max() < 1e-9)


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
    test_set_reference()
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
    test_seam_codes()
    test_grab()
    test_pattern_uv()
    test_outline_edges()
    test_bent_islands()
    test_pattern_mismatch()
    test_cache_io()
    test_transform()
    test_curve_ids()
    test_curve_eval()
    test_delaunay2d()
    test_curve_discretize()
    test_curve_update()
    test_curve_selection_runs()
    test_curve_transfer()
    test_step_call_matches_signature()
    test_panel_properties_exist()
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
