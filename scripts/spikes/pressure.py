"""調査: 圧力でクッション(閉じた袋)がどれだけ膨らむか。使える圧力の範囲を測る。

平らな正方形 2 枚を外周で縫い合わせた「枕」を膨らませて、
- 向かい合う面の間の距離(= 厚み)
- 囲む体積
- 伸び誤差(大きすぎる圧力は生地を伸ばしてしまう)
を、メッシュの細かさ・生地の伸び・圧力の組み合わせで見る。

分かったこと(2026-10-08、cloth_core v0.3.0):
- **膨らみ方は生地の伸びではなく、メッシュの細かさで決まる。** 辺を固めた三角形メッシュは
  幾何的に膨らめないので(閉じた三角形分割された面は剛)、細かいほどシワで逃げられる分だけ膨らむ
- 圧力と厚みはほぼ比例する
- 重力もピン留めも無いと、袋ごと傾くことがある(圧力の合力は打ち消し合うので移動はしない)

使い方: python scripts/spikes/pressure.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon" / "muslin"))
sys.path.insert(0, str(REPO / "scripts"))
import cloth_core  # noqa: E402
from verify_core import build_pillow, enclosed_volume  # noqa: E402

SIDE = 0.4          # 枕の一辺(m)
FRAMES = 240
RESOLUTIONS = (9, 17, 33)
STRETCHES = (1e-5, 5e-3)
PRESSURES = (20.0, 100.0, 500.0)


def inflate(n, stretch, pressure):
    positions, edges, bending, tris, pairs = build_pillow(n, SIDE / (n - 1))
    sim = cloth_core.ClothSim(positions, edges, bending, tris, [], 0.2, stretch, 1e-4)
    sim.set_pressure(pressure)
    for _ in range(FRAMES):
        sim.step(1.0 / 60.0, 0.0, 10, 8, 0.3)
    pos = sim.get_positions()

    def gap(a, b):
        return sum((pos[a * 3 + k] - pos[b * 3 + k]) ** 2 for k in range(3)) ** 0.5

    thickness = sum(gap(a, b) for a, b in pairs) / len(pairs)
    return thickness, enclosed_volume(pos, tris), sim.average_stretch_error()


def main():
    print(f"枕 {SIDE} m 角 / {FRAMES} フレーム / Substeps 8\n")
    print(f"{'点':>4} {'辺の長さ':>9} {'伸び':>8} {'圧力':>7} {'厚み':>9} {'体積':>10} {'伸び誤差':>9}")
    for n in RESOLUTIONS:
        for stretch in STRETCHES:
            for pressure in PRESSURES:
                thickness, volume, error = inflate(n, stretch, pressure)
                print(f"{n:>4} {SIDE / (n - 1) * 1000:>7.0f}mm {stretch:>8.0e} "
                      f"{pressure:>7.0f} {thickness * 1000:>7.1f}mm "
                      f"{volume:>9.5f} {error:>9.4f}")


if __name__ == "__main__":
    main()
