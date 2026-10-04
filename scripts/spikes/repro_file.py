"""再現: 保存された .blend を開き、布を Adjust と同じ条件で回して、動き続けないかを測る。

  blender -b --factory-startup --python scripts/spikes/repro_file.py -- <.blend のパス> [nobend]

nobend を付けると、縫い目をまたぐ曲げ制約を外して比べる。
"""
import sys
from pathlib import Path

import bpy
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon"))
import muslin  # noqa: E402
from muslin import dress as dress_mod, mesh_io, rest_shape, sim_state  # noqa: E402

muslin.register()
args = sys.argv[sys.argv.index("--") + 1:]
path = args[0]
nobend = "nobend" in args

bpy.ops.wm.open_mainfile(filepath=path)
print("\n== オブジェクト", flush=True)
for o in bpy.data.objects:
    print(f"  {o.name:22s} {o.type:7s} loc={tuple(round(x, 3) for x in o.location)} "
          f"rot={tuple(round(x, 3) for x in o.rotation_euler)} scale={tuple(round(x, 3) for x in o.scale)}", flush=True)
cloths = [o for o in bpy.data.objects if o.type == 'MESH' and o.muslin.collider_object is not None]
cloth = cloths[0] if cloths else bpy.context.active_object
p = cloth.muslin
print(f"== 布 {cloth.name}: {len(cloth.data.vertices)} 頂点 / 縫い目 {len(cloth.muslin_seams)} / "
      f"頂点グループ {[g.name for g in cloth.vertex_groups]} / pin='{p.pin_vertex_group}'", flush=True)
print(f"   Quality={p.quality} gravity={p.gravity} fabric={p.fabric_preset} density={p.density} "
      f"bending={p.bending_compliance} damping={p.damping} substeps={p.substeps} iterations={p.iterations}", flush=True)
print(f"   collider={p.collider_object.name if p.collider_object else None} collision={p.collision_enabled} "
      f"thickness={p.collision_thickness} friction={getattr(p, 'collision_friction', None)} "
      f"self={p.self_collision_enabled} floor={p.floor_enabled}", flush=True)
print(f"   dressed={rest_shape.is_dressed(cloth)} deformed={rest_shape.is_deformed(cloth)} "
      f"locked={rest_shape.is_pattern_locked(cloth)} seam_close_frames={p.seam_close_frames}", flush=True)

for a in args:
    if a.startswith("set:"):
        key, _, val = a[4:].partition("=")
        cur = getattr(p, key)
        if isinstance(cur, bool):
            setattr(p, key, val == "1")
        elif isinstance(cur, (int, float)):
            setattr(p, key, type(cur)(float(val)))
        else:
            setattr(p, key, val)
        print(f"   設定 {key} = {getattr(p, key)}", flush=True)

for a in args:
    if a.startswith("widen="):
        # 型紙(Curve)を横に広げる: 布の周が体の周より短いと、衝突と伸びがずっと綱引きになる
        factor = float(a[6:])
        curve = [o for o in bpy.data.objects if o.type == 'CURVE'][0]
        for sp in curve.data.splines:
            x0 = min(pt.co.x for pt in sp.bezier_points)
            for pt in sp.bezier_points:
                for v in (pt.co, pt.handle_left, pt.handle_right):
                    v.x = x0 + (v.x - x0) * factor
        print(f"   型紙の幅を {factor} 倍にした", flush=True)

for a in args:
    if a.startswith("fine="):
        # 体の円柱の分割数を変える(多角形の面が原因かを見る)
        seg = int(a[5:])
        old = p.collider_object
        bpy.ops.mesh.primitive_cylinder_add(vertices=seg, radius=0.1, depth=1.2, location=(0, 0, 0))
        new = bpy.context.active_object
        new.name = "BodyFine"
        p.collider_object = new
        old.hide_set(True)
        print(f"   体の円柱を {seg} 角形にした(元は {len(old.data.vertices) // 2} 角形)", flush=True)

if nobend:
    mesh_io.build_seam_bending = lambda *a, **k: ([], [])

for o in bpy.context.scene.objects:
    o.select_set(o is cloth)
bpy.context.view_layer.objects.active = cloth
d = dress_mod.Dresser(cloth, p, sim_state.effective_dt(bpy.context.scene),
                      max_steps=10 ** 6, auto_finish=False)
print("== 警告", d.warnings, flush=True)
st = d.state
print(f"   縫い目のペア {len(st['seam_pairs'])} / 縫い目の曲げ {st['sim'].seam_bending_count} / dt={d.dt}", flush=True)
if "noseam" in args:
    st["sim"].set_seams([], 0.0)          # 縫い目の距離の制約を外して、回転が縫い目由来かを見る
    print("   縫い目の制約を外した", flush=True)
axis = np.array([0.0, 0.0])
body = p.collider_object
if body is not None:
    bv = np.array([tuple(body.matrix_world @ v.co) for v in body.data.vertices])
    axis = np.array([(bv[:, 0].min() + bv[:, 0].max()) / 2, (bv[:, 1].min() + bv[:, 1].max()) / 2])
    print(f"   体の軸 {axis}", flush=True)
for block in range(4):
    for _ in range(99):
        d.step()
    before = np.array(st["sim"].get_positions()).reshape(-1, 3)
    d.step()
    pos = np.array(st["sim"].get_positions()).reshape(-1, 3)
    vel = (pos - before) / d.dt
    rel = pos[:, :2] - axis
    relv = vel[:, :2]
    lz = float((rel[:, 0] * relv[:, 1] - rel[:, 1] * relv[:, 0]).mean())
    ang = np.degrees(np.angle(np.exp(1j * np.arctan2(rel[:, 1], rel[:, 0])).mean()))
    r = np.hypot(rel[:, 0], rel[:, 1])
    print(f"  step {d.steps:4d} 最大速度 {np.linalg.norm(vel, axis=1).max() * 100:7.2f} cm/s "
          f"平均速度 {np.linalg.norm(vel, axis=1).mean() * 100:6.2f} cm/s  平均 Lz {lz:+.5f}  "
          f"半径 {r.min():.3f}〜{r.max():.3f}  平均角 {ang:+8.2f}°", flush=True)
