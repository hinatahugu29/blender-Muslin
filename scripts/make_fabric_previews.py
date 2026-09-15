"""生地プリセットのサムネイルを、シミュレータ自身に描かせる。

    blender --background --factory-startup --python scripts/make_fabric_previews.py

各生地で同じ場面(横棒に掛けた布)を Quality High で回し、静止したところを
128x128 で描いて `addon/muslin/previews/<生地>.png` に置く。

手で描いた絵だと実際の挙動とずれていく。ここで作れば、プリセットを
いじったときにサムネイルも作り直せば必ず一致する。

レンダラは Workbench を使う。EEVEE と違って GPU もライトも要らず、
ヘッドレスで確実に動いて速い。生地の違いはシルエットに出るので、
陰影が付けば十分。
"""

import sys
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "addon"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = REPO_ROOT / "addon" / "muslin" / "previews"
SIZE = 128
FRAMES = 60          # 静止するまで
CLOTH_SIDE = 41      # シワが出る程度の細かさ
CLOTH_SIZE = 1.2
ROD_RADIUS = 0.12
ROD_LENGTH = 0.45


def build_scene():
    """棒とその上に浮かべた布を作り、(布, 棒) を返す。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    # 掛ける相手は横向きの棒。球に掛けると、硬い生地は平らな板のまま
    # 傾いて滑り落ちてしまい、生地ではなく球が写る(実際にそうなった)。
    # 棒なら軸方向に対称なので倒れず、硬さが弧の広さに出る。
    bpy.ops.mesh.primitive_cylinder_add(
        radius=ROD_RADIUS, depth=ROD_LENGTH,
        rotation=(0.0, 1.5707963, 0.0), location=(0, 0, 0),
    )
    sphere = bpy.context.active_object
    sphere.name = "Rod"
    bpy.ops.object.shade_smooth()

    step = CLOTH_SIZE / (CLOTH_SIDE - 1)
    half = CLOTH_SIZE / 2.0
    verts = [
        (x * step - half, y * step - half, ROD_RADIUS + 0.25)
        for y in range(CLOTH_SIDE) for x in range(CLOTH_SIDE)
    ]
    faces = []
    for y in range(CLOTH_SIDE - 1):
        for x in range(CLOTH_SIDE - 1):
            i = y * CLOTH_SIDE + x
            faces.append((i, i + 1, i + CLOTH_SIDE + 1, i + CLOTH_SIDE))
    mesh = bpy.data.meshes.new("Cloth")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    cloth = bpy.data.objects.new("Cloth", mesh)
    bpy.context.collection.objects.link(cloth)
    bpy.context.view_layer.objects.active = cloth
    bpy.ops.object.shade_smooth()

    # 棒の真上の一列をピン留めする。
    #
    # 革のように完全剛体(bending = 0)の生地は、平らな板のまま細い棒の上で
    # 必ず倒れて滑り落ちる。物理としては正しいが、サムネイルとしては生地が
    # 写らない。掛け方を全生地で揃えれば、違いは「両脇がどう落ちるか」
    # だけに出る。
    group = cloth.vertex_groups.new(name="Hang")
    on_rod = [
        i for i, v in enumerate(mesh.vertices)
        if abs(v.co.y) < step and abs(v.co.x) < ROD_LENGTH / 2
    ]
    group.add(on_rod, 1.0, 'REPLACE')
    cloth.muslin.pin_vertex_group = "Hang"

    # カメラ: 斜め上から、布が枠いっぱいに入る位置
    cam_data = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    cam.location = (2.1, -2.1, 1.35)
    cam.rotation_euler = (1.02, 0.0, 0.785)
    cam_data.lens = 50
    scene.camera = cam

    scene.render.resolution_x = SIZE
    scene.render.resolution_y = SIZE
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.film_transparent = True
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'SINGLE'
    shading.single_color = (0.75, 0.74, 0.72)
    shading.show_shadows = True
    shading.show_cavity = True          # シワが見えるように
    scene.display.render_aa = '8'

    return cloth, sphere


def render_fabric(name, cloth, sphere):
    from muslin import sim_state

    props = cloth.muslin
    props.fabric_preset = name
    props.quality = 'FINAL'             # 硬い生地どうしの差は Substeps 32 で出る
    props.collision_enabled = True
    props.collider_object = sphere
    props.self_collision_enabled = True
    bpy.ops.muslin.fit_thickness()

    scene = bpy.context.scene
    scene.frame_set(1)
    bpy.ops.muslin.start_sim()
    for frame in range(2, FRAMES + 2):
        scene.frame_set(frame)
    state = sim_state.get_state(cloth)
    finite = state["sim"].is_finite()
    error = state["last_error"]
    bpy.ops.muslin.stop_sim()

    out = OUT_DIR / f"{name.lower()}.png"
    scene.render.filepath = str(out)
    bpy.ops.render.render(write_still=True)
    return out, finite, error


def main():
    from muslin import register
    from muslin.properties import FABRIC_PRESETS

    register()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cloth, sphere = build_scene()
    failed = []
    for name in FABRIC_PRESETS:
        out, finite, error = render_fabric(name, cloth, sphere)
        size = out.stat().st_size if out.exists() else 0
        status = "OK" if (finite and size > 0) else "NG"
        if status == "NG":
            failed.append(name)
        print(f"[{status}] {name:<8} 伸び誤差 {error:.5f}  -> "
              f"{out.name} ({size / 1024:.1f} KB)", flush=True)
        # 次の生地は同じ場面をやり直す(前の結果を引きずらない)
        cloth, sphere = build_scene()

    print(f"\n{len(FABRIC_PRESETS) - len(failed)}/{len(FABRIC_PRESETS)} 枚を書き出した")
    if failed:
        print("失敗: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
