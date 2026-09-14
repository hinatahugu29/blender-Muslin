"""ベイク(M6): シミュレーション結果のディスク書き出しと再生。

2種類のベイクを用意している:
- **Bake to Disk**: フレームごとの頂点座標をファイルに保存する。
  .blend を閉じても残り、スクラブが即座に効く。メモリも食わない。
- **Bake to Shape Keys**: シェイプキー + キーフレームに変換する。
  アドオン無しで再生でき、他ソフトへのエクスポートやレンダーファームで使える。

ベイク中は各フレームで `frame_set` を呼ぶので、アーマチュアで動く
コライダーも正しく追従する(通常再生時の追いつき計算にある制約が無い)。
"""

import os

import bpy

from . import cache_io
from . import mesh_io
from . import sim_state


def cache_directory(obj):
    """オブジェクト用のキャッシュ保存先を返す。

    .blend が保存済みならその隣の `blendcache_cloth_md/` に、
    未保存なら一時ディレクトリに置く(Blender の点キャッシュと同じ考え方)。
    """
    blend_path = bpy.data.filepath
    if blend_path:
        base = os.path.join(os.path.dirname(blend_path), "blendcache_cloth_md")
    else:
        base = os.path.join(bpy.app.tempdir, "blendcache_cloth_md")
    # オブジェクト名にファイル名として使えない文字が含まれても壊れないようにする
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in obj.name)
    return os.path.join(base, safe_name)


def is_baked(obj):
    return bool(obj is not None and obj.get("cloth_md_baked", False))


def baked_range(obj):
    return int(obj.get("cloth_md_bake_start", 0)), int(obj.get("cloth_md_bake_end", 0))


def apply_baked_frame(obj, frame):
    """ベイク済みキャッシュから指定フレームをメッシュに適用する。

    戻り値: 適用できたら True
    """
    directory = obj.get("cloth_md_cache_dir", "")
    if not directory:
        return False

    start, end = baked_range(obj)
    clamped = min(max(frame, start), end)  # 範囲外は端のフレームで固定する

    try:
        positions = cache_io.read_frame(directory, clamped, len(obj.data.vertices))
    except cache_io.CacheError as exc:
        print(f"[cloth_md] ベイク再生に失敗: {exc}")
        return False

    obj.data.vertices.foreach_set("co", positions)
    obj.data.update()
    return True


class CLOTHMD_OT_bake(bpy.types.Operator):
    """シーンのフレーム範囲でシミュレーションを実行し、結果をディスクに保存する"""

    bl_idname = "cloth_md.bake"
    bl_label = "Bake to Disk"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        props = scene.cloth_md_props
        obj = context.active_object

        start = scene.frame_start
        end = scene.frame_end
        if end < start:
            self.report({'ERROR'}, "シーンのフレーム範囲が不正です")
            return {'CANCELLED'}

        directory = cache_directory(obj)
        cache_io.clear_cache(directory)

        original_frame = scene.frame_current
        # ベイク中はハンドラ管理下のライブシミュレーションを止める。
        # ベイク自身は `create_state` で独立した状態を持つので、
        # frame_set でハンドラが走っても二重に計算されることはない。
        was_running = sim_state.is_running(obj)
        sim_state.stop_simulation(obj)

        # ベイク前のメッシュ形状を保存しておき、終了後に戻せるようにする
        rest_local = [0.0] * (len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get("co", rest_local)

        window_manager = context.window_manager
        window_manager.progress_begin(0, end - start + 1)

        try:
            scene.frame_set(start)
            state = sim_state.create_state(obj, props)

            dt = sim_state.effective_dt(scene, props)
            local = [0.0] * (len(obj.data.vertices) * 3)

            for offset, frame in enumerate(range(start, end + 1)):
                # コライダーのアニメーションを正しく評価するためフレームを進める
                scene.frame_set(frame)

                if frame > start:
                    sim_state.advance_one_frame(state, props, dt, frame)

                world = state["sim"].get_positions()
                mesh_io.write_positions_to_mesh(obj, world)
                obj.data.vertices.foreach_get("co", local)
                cache_io.write_frame(directory, frame, local)

                window_manager.progress_update(offset)

                if not state["sim"].is_finite():
                    self.report(
                        {'ERROR'},
                        f"フレーム {frame} でシミュレーションが発散しました。ベイクを中断します",
                    )
                    break
        except Exception as exc:
            obj.data.vertices.foreach_set("co", rest_local)
            obj.data.update()
            scene.frame_set(original_frame)
            self.report({'ERROR'}, f"ベイクに失敗: {exc}")
            return {'CANCELLED'}
        finally:
            window_manager.progress_end()

        if was_running:
            print("[cloth_md] ベイクしたのでライブシミュレーションは停止しました")

        baked_frames = sum(
            1 for f in range(start, end + 1) if cache_io.has_frame(directory, f)
        )
        actual_end = start + baked_frames - 1

        cache_io.write_info(directory, {
            "object": obj.name,
            "vertices": len(obj.data.vertices),
            "frame_start": start,
            "frame_end": actual_end,
            "blender": bpy.app.version_string,
        })

        obj["cloth_md_baked"] = True
        obj["cloth_md_bake_start"] = start
        obj["cloth_md_bake_end"] = actual_end
        obj["cloth_md_cache_dir"] = directory

        scene.frame_set(original_frame)
        apply_baked_frame(obj, original_frame)

        size_mb = cache_io.cache_size_bytes(directory) / (1024 * 1024)
        self.report(
            {'INFO'},
            f"ベイク完了: {baked_frames} フレーム / {size_mb:.1f} MB → {directory}",
        )
        return {'FINISHED'}


class CLOTHMD_OT_free_bake(bpy.types.Operator):
    """ベイク済みキャッシュを削除して元のメッシュ形状に戻す"""

    bl_idname = "cloth_md.free_bake"
    bl_label = "Free Bake"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        # 再生を停止した(シェイプキー変換後の)状態でも、キャッシュは削除できるようにする
        return obj is not None and bool(obj.get("cloth_md_cache_dir", ""))

    def execute(self, context):
        obj = context.active_object
        directory = obj.get("cloth_md_cache_dir", "")
        removed = cache_io.clear_cache(directory) if directory else 0

        for key in ("cloth_md_baked", "cloth_md_bake_start",
                    "cloth_md_bake_end", "cloth_md_cache_dir"):
            if key in obj:
                del obj[key]

        self.report({'INFO'}, f"キャッシュを削除しました({removed} ファイル)")
        return {'FINISHED'}


class CLOTHMD_OT_bake_to_shape_keys(bpy.types.Operator):
    """ベイク済みキャッシュをシェイプキー + キーフレームに変換する

    アドオン無しで再生でき、他ソフトへのエクスポートにも使える形になる。
    """

    bl_idname = "cloth_md.bake_to_shape_keys"
    bl_label = "Convert to Shape Keys"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_baked(context.active_object)

    def execute(self, context):
        obj = context.active_object
        directory = obj.get("cloth_md_cache_dir", "")
        start, end = baked_range(obj)
        frame_count = end - start + 1

        if frame_count > 500:
            self.report(
                {'ERROR'},
                f"{frame_count} フレームは多すぎます(上限 500)。"
                "フレーム範囲を狭めてベイクし直してください",
            )
            return {'CANCELLED'}

        mesh = obj.data
        vertex_count = len(mesh.vertices)

        if obj.data.shape_keys is None:
            obj.shape_key_add(name="Basis", from_mix=False)

        created = 0
        for frame in range(start, end + 1):
            try:
                positions = cache_io.read_frame(directory, frame, vertex_count)
            except cache_io.CacheError as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}

            key = obj.shape_key_add(name=f"cloth_md_{frame:04d}", from_mix=False)
            key.data.foreach_set("co", positions)

            # 該当フレームだけ 1.0 になるようキーフレームを打つ
            key.value = 0.0
            key.keyframe_insert("value", frame=frame - 1)
            key.value = 1.0
            key.keyframe_insert("value", frame=frame)
            key.value = 0.0
            key.keyframe_insert("value", frame=frame + 1)
            created += 1

        # 補間を一定にして、意図しないブレンドが起きないようにする
        anim = obj.data.shape_keys.animation_data
        if anim is not None and anim.action is not None:
            for fcurve in anim.action.fcurves:
                for keyframe in fcurve.keyframe_points:
                    keyframe.interpolation = 'LINEAR'

        # シェイプキーは Basis からの相対変位なので、ベイク再生がベースメッシュを
        # 書き換え続けると二重に変形してしまう。変換後は再生を止める。
        obj["cloth_md_baked"] = False

        self.report(
            {'INFO'},
            f"{created} 個のシェイプキーに変換しました(二重変形を防ぐためベイク再生は停止)",
        )
        return {'FINISHED'}


_classes = (
    CLOTHMD_OT_bake,
    CLOTHMD_OT_free_bake,
    CLOTHMD_OT_bake_to_shape_keys,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
