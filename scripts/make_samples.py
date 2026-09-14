"""サンプルシーンを生成する。

使い方:
    blender --background --factory-startup --python scripts/make_samples.py

`samples/` に .blend を書き出す。どれも **Muslin を有効にして開き、
`Start Simulation` を押して再生するだけ**で結果が見られる状態にしてある。

手で作った .blend を置くのではなくスクリプトで生成するのは、
アドオンの既定値や API が変わったときに作り直せるようにするため。
"""

import sys
from pathlib import Path

import bpy
from mathutils import Vector

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "addon"))

OUT_DIR = REPO_ROOT / "samples"


# ------------------------------------------------------------------ 補助

def fresh_scene(fps=24, frame_end=120):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = fps
    scene.frame_start = 1
    scene.frame_end = frame_end
    scene.frame_set(1)
    return scene


def add_note(text, location, size=0.12):
    """シーン内に説明文を置く。開いた人が何をすればいいか分かるように。"""
    bpy.ops.object.text_add(location=location)
    obj = bpy.context.active_object
    obj.data.body = text
    obj.data.size = size
    obj.data.align_x = 'CENTER'
    obj.name = "README"
    obj.rotation_euler = (1.5708, 0.0, 0.0)  # 正面向きに立てる
    return obj


def add_camera_and_light(target, distance=4.0, height=2.0):
    bpy.ops.object.light_add(type='SUN', location=(3.0, -4.0, 6.0))
    bpy.context.active_object.data.energy = 3.0

    bpy.ops.object.camera_add(location=(distance, -distance, height))
    cam = bpy.context.active_object
    direction = Vector(target) - cam.location
    cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    bpy.context.scene.camera = cam


def make_cloth_grid(name, size, resolution, location):
    """目標エッジ長 `resolution` の平面を作る。"""
    side = max(int(round(size / resolution)), 2) + 1
    mesh = bpy.data.meshes.new(name)
    step = size / (side - 1)
    verts = [(x * step - size / 2, y * step - size / 2, 0.0)
             for y in range(side) for x in range(side)]
    faces = [
        (y * side + x, y * side + x + 1, (y + 1) * side + x + 1, (y + 1) * side + x)
        for y in range(side - 1)
        for x in range(side - 1)
    ]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj, side


def smooth(obj):
    for poly in obj.data.polygons:
        poly.use_smooth = True


def mark_as_cloth(obj):
    """どれが布なのかを明示する。検証スクリプトがこれを見て対象を選ぶ。"""
    obj["muslin_sample"] = True
    return obj


def save(name):
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / name
    bpy.ops.wm.save_as_mainfile(filepath=str(path))
    size_kb = path.stat().st_size / 1024
    print(f"  -> {path.name}  ({size_kb:.0f} KB)", flush=True)


# ------------------------------------------------------------------ 1. ドレープ

def sample_drape():
    """球に布を被せる。最も基本的な使い方。"""
    print("01_drape: 球に布を被せる", flush=True)
    scene = fresh_scene(frame_end=120)

    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=32, ring_count=24,
                                         location=(0.0, 0.0, 0.0))
    sphere = bpy.context.active_object
    sphere.name = "Collider"
    smooth(sphere)

    cloth, _ = make_cloth_grid("Cloth", size=1.8, resolution=0.025,
                               location=(0.0, 0.0, 0.85))
    smooth(cloth)
    mark_as_cloth(cloth)

    props = cloth.muslin             # 設定は布ごとに持つ
    props.fabric_preset = 'COTTON'     # 物性はプリセットから
    props.collider_object = sphere
    props.collision_enabled = True
    props.self_collision_enabled = True
    props.damping = 0.08               # 揺れを落ち着かせて最終形を見せる
    bpy.ops.muslin.fit_thickness()   # 厚みはメッシュの細かさに合わせる

    add_note("Muslin: Start Simulation を押して再生",
             location=(0.0, 0.0, 1.45))
    add_camera_and_light((0.0, 0.0, 0.2))
    save("01_drape.blend")


# ------------------------------------------------------------------ 2. 旗

def sample_flag():
    """片端を固定して風になびかせる。Substeps と人工減衰の違いが見える題材。"""
    print("02_flag: 風になびく旗", flush=True)
    scene = fresh_scene(frame_end=250)

    cloth, side = make_cloth_grid("Flag", size=1.2, resolution=0.03,
                                  location=(0.0, 0.0, 1.2))
    cloth.rotation_euler = (1.5708, 0.0, 0.0)   # 縦に立てる
    smooth(cloth)
    mark_as_cloth(cloth)

    # 左端を固定する頂点グループ
    group = cloth.vertex_groups.new(name="Pin")
    left = min(v.co.x for v in cloth.data.vertices)
    group.add([v.index for v in cloth.data.vertices if abs(v.co.x - left) < 1e-5],
              1.0, 'REPLACE')

    bpy.ops.mesh.primitive_cylinder_add(radius=0.02, depth=2.4,
                                        location=(-0.6, 0.0, 0.6))
    bpy.context.active_object.name = "Pole"

    props = cloth.muslin
    props.fabric_preset = 'SILK'   # 軽くなめらかになびく
    props.pin_vertex_group = "Pin"
    props.collision_enabled = False
    props.self_collision_enabled = False
    props.wind = (0.0, 6.0, 0.0)
    props.damping = 0.0            # 人工減衰の違いを見せたいので 0
    # 揺れを持続させる設定。README の「Substeps は揺れの持ちに効く」の実例。
    # Substeps 16 は生地ごとの曲げの違いが出る領域でもある
    props.iterations = 2
    props.substeps = 16

    add_note("風になびく旗 / Substeps 16・Iterations 2 で揺れが持続する",
             location=(0.0, 0.0, 2.1), size=0.09)
    add_camera_and_light((0.0, 0.0, 1.2), distance=3.5, height=1.6)
    save("02_flag.blend")


# ------------------------------------------------------------------ 3. 縫製

def sample_sewing():
    """2枚のピースを縫い合わせて筒にする。パターンを縫い合わせるワークフローの核心。"""
    print("03_sewing: 2枚を縫って筒にする", flush=True)
    scene = fresh_scene(frame_end=180)

    tools = scene.muslin_tools   # パターンの寸法はシーンの道具設定
    tools.pattern_width = 0.45
    tools.pattern_height = 0.7
    tools.pattern_resolution = 0.025

    # 前身頃と後ろ身頃を向かい合わせに置く
    bpy.ops.muslin.add_pattern_piece()
    front = bpy.context.active_object
    front.name = "Front"
    front.location = (0.0, -0.18, 1.0)

    bpy.ops.muslin.add_pattern_piece()
    back = bpy.context.active_object
    back.name = "Back"
    back.location = (0.0, 0.18, 1.0)

    # 1つのオブジェクトに統合してから縫う(縫い目は同一オブジェクト内でしか作れない)
    for o in bpy.context.selected_objects:
        o.select_set(False)
    front.select_set(True)
    back.select_set(True)
    bpy.context.view_layer.objects.active = front
    bpy.ops.muslin.join_pieces()
    piece = bpy.context.active_object
    piece.name = "Garment"
    smooth(piece)
    mark_as_cloth(piece)

    # 上端(肩の線)をピン留めする。留めないと縫っている途中で落下してしまう
    group = piece.vertex_groups.new(name="Shoulder")
    top = max(v.co.z for v in piece.data.vertices)
    group.add(
        [v.index for v in piece.data.vertices if abs(v.co.z - top) < 1e-5],
        1.0,
        'REPLACE',
    )

    # 左右それぞれの側で、前後の端を縫い合わせる
    xs = [v.co.x for v in piece.data.vertices]
    left, right = min(xs), max(xs)
    made = 0
    for edge_x in (left, right):
        bpy.ops.object.mode_set(mode='OBJECT')
        # 編集モードに入るとき、Blender は頂点の選択からエッジ選択を作り直す。
        # エッジだけ消しても前回の選択が復活するので、頂点と面も消しておく。
        for v in piece.data.vertices:
            v.select = False
        for e in piece.data.edges:
            e.select = False
        for poly in piece.data.polygons:
            poly.select = False
        for e in piece.data.edges:
            a = piece.data.vertices[e.vertices[0]].co
            b = piece.data.vertices[e.vertices[1]].co
            if abs(a.x - edge_x) < 1e-5 and abs(b.x - edge_x) < 1e-5:
                e.select = True
                piece.data.vertices[e.vertices[0]].select = True
                piece.data.vertices[e.vertices[1]].select = True
        bpy.ops.object.mode_set(mode='EDIT')
        if bpy.ops.muslin.add_seam() == {'FINISHED'}:
            made += 1
        bpy.ops.object.mode_set(mode='OBJECT')
    print(f"  シーム {made} 本", flush=True)

    props = piece.muslin
    props.fabric_preset = 'COTTON'
    props.pin_vertex_group = "Shoulder"
    props.collision_enabled = False
    props.self_collision_enabled = True
    props.seam_close_frames = 40
    props.damping = 0.06
    props.show_seams = True
    props.substeps = 16            # 生地の曲げが効く設定にしておく
    bpy.ops.muslin.fit_thickness()

    add_note("2枚のピースを左右で縫う / 再生すると 40 フレームかけて閉じる",
             location=(0.0, 0.0, 1.62), size=0.08)
    add_camera_and_light((0.0, 0.0, 1.0), distance=2.2, height=1.4)
    save("03_sewing.blend")


if __name__ == "__main__":
    import muslin

    muslin.register()
    print(f"サンプルを生成します -> {OUT_DIR}", flush=True)
    sample_drape()
    sample_flag()
    sample_sewing()
    print("完了", flush=True)
