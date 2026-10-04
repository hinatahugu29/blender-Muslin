"""再現: 着せた布を Adjust で回し続けたとき、円柱のまわりをずっと回転しないか。

サンプル 07 を開き、Adjust と同じ条件(Dresser、終了しない)で数百ステップ回して、
最大速度と、Z 軸まわりの角運動量(平均の角速度)を測る。縫い目をまたぐ曲げ制約を入れた場合と、
外した場合(モンキーパッチ)を比べる。

  blender -b --factory-startup --python scripts/spikes/repro_spin.py -- [Quality] [gravity]
"""
import sys
from pathlib import Path

import bpy
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "addon"))
import muslin  # noqa: E402
from muslin import dress as dress_mod, mesh_io, sim_state  # noqa: E402

muslin.register()
args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
quality = args[0].upper() if args else None
gravity = float(args[1]) if len(args) > 1 else None
nopin = "nopin" in args
rebuild = "rebuild" in args or "far" in args
far = "far" in args


def run(label, disable_bending):
    bpy.ops.wm.open_mainfile(filepath=str(REPO / "samples" / "07_curve_arrange.blend"))
    cloth = bpy.data.objects["Pair"]
    if rebuild:
        # 利用者の手順: 布を消して Rebuild し直し(新しい布 = ピン無し・設定は既定)、デニムにして、体に置いて着せる
        curve = bpy.data.objects["Pair_Pattern"]
        body = bpy.data.objects["Body"]
        bpy.data.objects.remove(cloth)
        from muslin import curve_pattern as cp
        cloth, _w = cp.rebuild(bpy.context, curve)
        cloth.muslin.fabric = 'DENIM'
        cloth.muslin.collider_object = body
        cloth.muslin.collision_enabled = True
        cloth.muslin.self_collision_enabled = False
        if not far:
            bpy.context.view_layer.update()      # matrix_world を更新してから置く(GUI では常に更新済み)
        cp.arrange_around(curve, body)           # far: 更新しないと布が体から遠くに置かれ、自由な布になる
    props = cloth.muslin
    if quality:
        props.quality = quality
    if gravity is not None:
        props.gravity = gravity
    print(f"\n== {label}: Quality={props.quality} gravity={props.gravity} "
          f"collision={props.collision_enabled} thickness={props.collision_thickness}", flush=True)
    if nopin and not rebuild:
        # ピン留めを外す(Rebuild で作り直した布はピンが無い)。重力 0 の自由な布にして、再配置・着せ付けからやり直す
        props.pin_vertex_group = ""
        if "Pin" in cloth.vertex_groups:
            cloth.vertex_groups.remove(cloth.vertex_groups["Pin"])
        curve = bpy.data.objects["Pair_Pattern"]
        from muslin import curve_pattern as cp
        cp.arrange_around(curve, props.collider_object)
    original = mesh_io.build_seam_bending
    if disable_bending:
        mesh_io.build_seam_bending = lambda *a, **k: ([], [])
    try:
        for o in bpy.context.scene.objects:
            o.select_set(o is cloth)
        bpy.context.view_layer.objects.active = cloth
        if nopin or rebuild:
            print("  Dress:", bpy.ops.muslin.dress(), flush=True)
        d = dress_mod.Dresser(cloth, props, sim_state.effective_dt(bpy.context.scene),
                              max_steps=10 ** 6, auto_finish=False)
        print("  警告:", d.warnings, flush=True)
        st = d.state
        for block in range(6):
            for _ in range(99):
                d.step()
            sim = st["sim"]
            before = np.array(sim.get_positions()).reshape(-1, 3)
            d.step()
            pos = np.array(sim.get_positions()).reshape(-1, 3)
            vel = (pos - before) / d.dt          # 1 ステップの変位から速度を見積もる
            speed = float(np.linalg.norm(vel, axis=1).max())
            lz = float((pos[:, 0] * vel[:, 1] - pos[:, 1] * vel[:, 0]).mean())
            r = np.hypot(pos[:, 0], pos[:, 1])
            ang = np.arctan2(pos[:, 1], pos[:, 0])
            if "debug" in args and block == 0:
                top = np.argsort(-np.linalg.norm(vel, axis=1))[:5]
                for i in top:
                    print(f"    頂点 {i}: 速度 {np.linalg.norm(vel[i]) * 100:.1f} cm/s  1 ステップ前 {np.round(before[i], 4)} → {np.round(pos[i], 4)}", flush=True)
                n = len(pos)
                print(f"    頂点数 {n} / 縫い目の曲げ {st['sim'].seam_bending_count} / 縫い目のペア {len(st['seam_pairs'])}", flush=True)
            print(f"  step {d.steps:4d}  最大速度 {speed * 100:6.2f} cm/s  平均 Lz {lz:+.5f}  "
                  f"半径 {r.min():.3f}〜{r.max():.3f}  平均角 {np.degrees(np.angle(np.exp(1j * ang).mean())):+7.2f}°",
                  flush=True)
    finally:
        mesh_io.build_seam_bending = original


run("縫い目の曲げ制約あり", False)
run("縫い目の曲げ制約なし", True)
