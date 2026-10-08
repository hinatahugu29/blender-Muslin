"""調査: 縫い目で縫った袋(クッション)が、圧力でどう膨らむか。

`scripts/spikes/pressure.py` は縁の頂点を**共有した**袋を測っていて、ほとんど膨らまなかった。
そちらは辺を固めた三角形メッシュが幾何的に膨らめないためで、**実際の作り方(2 枚を縫い目で
縫い合わせる)とは条件が違う**。縫い目で縫った袋は縁が内側へ寄れるので、よく膨らむ。
その違いを測るのがこのスパイク。Curve Pattern と Close Bag を通すので Blender が要る。

分かったこと(2026-10-08、cloth_core v0.3.0、Blender 5.2.2):

| 辺の長さ | 品質 | 圧力 | 頂点 | 厚み | 伸び誤差 |
|---|---|---|---|---|---|
| 2cm | High | 100 | 552 | 374mm | 0.0100 |
| 2cm | Final | 100 | 552 | 290mm | 0.0037 |
| 2cm | High | 20 | 552 | 381mm | 0.0083 |
| 5cm | High | 100 | 102 | 217mm | 0.0048 |
| 1cm | Final | 100 | 2120 | 153mm | 0.0113 |

- 30cm 角のクッションが厚み 15〜38cm まで膨らむ。伸び誤差は 1% 以下なので、
  生地が伸びたのではなく本当に膨らんでいる
- **圧力 20 と 100 で厚みがほぼ同じ(381mm と 374mm)= 膨らみきって飽和している。**
  体積を見ない一定の圧力では、形が幾何的な最大(縁が縮んで紡錘形になる)まで行ってしまい、
  圧力の値で膨らみ方を決められない。**ふくらみ具合を決めるには第 2 段階(体積を考える圧力)が要る**

使い方: blender --background --factory-startup --python scripts/spikes/bag_pressure.py
"""
import sys
from pathlib import Path

import bpy
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon"))
import muslin  # noqa: E402

SIDE = 0.3          # クッションの一辺(m)
STEPS = 400
CASES = ((0.02, 100, 'HIGH'), (0.02, 100, 'FINAL'), (0.02, 20, 'HIGH'),
         (0.05, 100, 'HIGH'), (0.01, 100, 'FINAL'))


def inflate(edge_length, pressure, quality):
    """2 枚を重ねて外周を縫い、圧力で膨らませる。(頂点数, 厚み, 伸び誤差) を返す。"""
    from muslin import curve_pattern as cp, dress

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_set(1)

    curve_data = bpy.data.curves.new("Bag", 'CURVE')
    curve_data.dimensions = '2D'
    for x0 in (0.0, SIDE * 2):
        spline = curve_data.splines.new('BEZIER')
        spline.bezier_points.add(3)
        corners = [(x0, 0.0), (x0 + SIDE, 0.0), (x0 + SIDE, SIDE), (x0, SIDE)]
        for point, (x, y) in zip(spline.bezier_points, corners):
            point.co = (x, y, 0.0)
            point.handle_left_type = point.handle_right_type = 'VECTOR'
        spline.use_cyclic_u = True
    bag = bpy.data.objects.new("Bag", curve_data)
    scene.collection.objects.link(bag)
    bpy.context.view_layer.objects.active = bag

    cp.initialize(bag, edge_length)
    cp.stack_pieces(bpy.context, bag)
    uids_a, uids_b = [p["uids"] for p in cp.load_record(bag)["pieces"]][:2]
    cp.add_seam(bag, [(uids_a[0], 0.0, uids_a[0], 0.0)],
                [(uids_b[0], 0.0, uids_b[0], 0.0)], name="Rim")
    cp.apply_seams(bag)
    cloth, _ = cp.rebuild(bpy.context, bag)

    props = cloth.muslin
    props.collision_enabled = False
    props.self_collision_enabled = True
    props.seam_close_frames = 10
    props.pressure = pressure
    props.quality = quality
    for obj in scene.objects:
        obj.select_set(obj is cloth)
    bpy.context.view_layer.objects.active = cloth

    # Close Bag と同じ回し方(重力 0)。測るだけなので保存はしない
    dresser = dress.Dresser(cloth, props, 1.0 / 60.0, STEPS, overrides={"gravity": 0.0})
    for _ in range(STEPS):
        if dresser.step():
            break
    error = dresser.state["sim"].average_stretch_error()
    positions = np.asarray(dresser.state["sim"].get_positions()).reshape(-1, 3)
    dresser.cancel()
    return len(cloth.data.vertices), float(np.ptp(positions[:, 2])), error


def main():
    muslin.register()
    print(f"クッション {SIDE * 100:.0f}cm 角 / 2 枚を外周で縫う / 最大 {STEPS} ステップ\n")
    print(f"{'辺':>7} {'圧力':>6} {'品質':>7} {'頂点':>6} {'厚み':>9} {'伸び誤差':>9}")
    for edge_length, pressure, quality in CASES:
        count, thickness, error = inflate(edge_length, pressure, quality)
        print(f"{edge_length * 100:>5.0f}cm {pressure:>6} {quality:>7} {count:>6} "
              f"{thickness * 1000:>7.1f}mm {error:>9.4f}")
    muslin.unregister()


if __name__ == "__main__":
    main()
