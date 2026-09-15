"""ピン留めの頂点グループを、編集モードの選択から作る/足す/外す。

頂点グループそのものは Blender の標準機能で作れるが、

1. オブジェクトデータのプロパティへ行って
2. グループを新規作成して名前を付けて
3. 編集モードで選択して Assign を押して
4. Muslin の Pinning パネルに戻ってそのグループを選ぶ

という往復になる。ピン留めは「この頂点を固定したい」という一つの意図
なので、選択した状態からボタン一つで済ませる。

作ったグループは Muslin の Pin Vertex Group に自動で入る。ピン留めは
走らせたまま反映されるので(sim_state._sync_material)、編集モードを
抜けた時点で効く。
"""

import bmesh
import bpy

from . import sim_state
from . import ui_poll

# 新しく作るときの名前。既に同名があれば Blender が .001 を付ける。
DEFAULT_GROUP_NAME = "Pin"


def _pin_group(obj, create):
    """Pin Vertex Group に指定されたグループを返す。

    create が真で、まだ無ければ作る。名前が空だったり、指しているグループが
    消えていたりする場合も作り直す。
    """
    name = obj.muslin.pin_vertex_group
    group = obj.vertex_groups.get(name) if name else None
    if group is None and create:
        group = obj.vertex_groups.new(name=name or DEFAULT_GROUP_NAME)
        obj.muslin.pin_vertex_group = group.name
    return group


def _edit_mesh(obj):
    """編集中のメッシュから (bmesh, deform レイヤー, 選択頂点) を返す。

    `layers.deform.verify()` はレイヤーを新しく足すときに BMesh を作り直す。
    先に取っておいた BMVert はそこで無効になり、触ると
    `ReferenceError: BMesh data of type BMVert has been removed` になる。
    **レイヤーを用意してから頂点を集めること。**
    """
    bm = bmesh.from_edit_mesh(obj.data)
    layer = bm.verts.layers.deform.verify()
    return bm, layer, [v for v in bm.verts if v.select]


class MUSLIN_OT_pin_selected(bpy.types.Operator):
    """選択した頂点をピン留めのグループに入れる(無ければグループを作る)"""

    bl_idname = "muslin.pin_selected"
    bl_label = "Pin Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return ui_poll.in_edit_mode(cls, context)

    def execute(self, context):
        obj = context.active_object
        bm, layer, selected = _edit_mesh(obj)
        if not selected:
            self.report({'ERROR'}, "頂点が選択されていません")
            return {'CANCELLED'}

        created = _pin_group(obj, create=False) is None
        group = _pin_group(obj, create=True)

        # 編集モードでは vertex_groups.add() が使えない(編集用の BMesh の方が
        # 正しい状態を持っているため)。deform レイヤーに直接書く。
        for v in selected:
            v[layer][group.index] = 1.0
        bmesh.update_edit_mesh(obj.data)
        # 走行中なら、次のフレームでピン留めを読み直させる
        sim_state.invalidate_pinning(obj)

        total = sum(1 for v in bm.verts if group.index in v[layer])
        self.report(
            {'INFO'},
            f"{len(selected)} 頂点を '{group.name}' に"
            + ("追加しました" if not created else "入れました")
            + f"(合計 {total} 頂点)",
        )
        return {'FINISHED'}


class MUSLIN_OT_unpin_selected(bpy.types.Operator):
    """選択した頂点をピン留めのグループから外す"""

    bl_idname = "muslin.unpin_selected"
    bl_label = "Unpin Selected"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not ui_poll.in_edit_mode(cls, context):
            return False
        if _pin_group(context.active_object, create=False) is None:
            return ui_poll.reject(cls, "ピン留めのグループがまだありません")
        return True

    def execute(self, context):
        obj = context.active_object
        group = _pin_group(obj, create=False)
        bm, layer, selected = _edit_mesh(obj)
        if not selected:
            self.report({'ERROR'}, "頂点が選択されていません")
            return {'CANCELLED'}

        removed = 0
        for v in selected:
            if group.index in v[layer]:
                del v[layer][group.index]
                removed += 1
        bmesh.update_edit_mesh(obj.data)
        # 走行中なら、次のフレームでピン留めを読み直させる
        sim_state.invalidate_pinning(obj)

        total = sum(1 for v in bm.verts if group.index in v[layer])
        self.report({'INFO'}, f"{removed} 頂点を外しました(残り {total} 頂点)")
        return {'FINISHED'}


class MUSLIN_OT_select_pinned(bpy.types.Operator):
    """ピン留めされている頂点を選択する"""

    bl_idname = "muslin.select_pinned"
    bl_label = "Select Pinned"

    @classmethod
    def poll(cls, context):
        if not ui_poll.in_edit_mode(cls, context):
            return False
        if _pin_group(context.active_object, create=False) is None:
            return ui_poll.reject(cls, "ピン留めのグループがまだありません")
        return True

    def execute(self, context):
        obj = context.active_object
        group = _pin_group(obj, create=False)
        bm, layer, _selected = _edit_mesh(obj)

        for v in bm.verts:
            v.select_set(False)
        wanted = [v for v in bm.verts if group.index in v[layer]]
        for v in wanted:
            v.select_set(True)
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)

        self.report({'INFO'}, f"{len(wanted)} 頂点を選択しました")
        return {'FINISHED'}


def pinned_count(obj):
    """ピン留めされている頂点数。グループが無ければ 0。

    パネルの表示用。編集モードでは編集用の BMesh の方が新しいので、
    そちらを見る(オブジェクトモードでは obj.data を見る)。
    """
    group = _pin_group(obj, create=False)
    if group is None:
        return 0

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.verts.layers.deform.active
        if layer is None:
            return 0
        return sum(1 for v in bm.verts if group.index in v[layer])

    return sum(
        1 for v in obj.data.vertices
        if any(g.group == group.index and g.weight > 0.0 for g in v.groups)
    )


_classes = (
    MUSLIN_OT_pin_selected,
    MUSLIN_OT_unpin_selected,
    MUSLIN_OT_select_pinned,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
