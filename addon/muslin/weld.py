"""縫い目の溶接(表示・書き出し用)。

縫い目は距離の制約なので、閉じても 2 枚の境界の頂点は別々のまま重なっているだけになる。
そのため縫い目で陰影が割れ、書き出したメッシュも縫い目で切れる。Blender の Weld
モディファイアを使えば、閉じたところから自然に 1 つにつながる。

- 縫い目に関わる頂点を頂点グループ `Muslin_Seam` に入れる(縫い目の辺の属性から作る)
- Weld モディファイア(モード All)を、そのグループに限って先頭に付ける
- Muslin はシミュレーション結果をベースメッシュの頂点に直接書くので、モディファイアは
  毎フレームの結果に対して評価される。縫い目が開いている間は何も起きず、閉じた分だけ溶接される
- シミュレーションの頂点は溶接しない(元のメッシュは変わらない)。最後に Apply すれば、
  閉じた 1 枚のメッシュが得られる

距離の既定は、メッシュの辺の長さの中央値の 2%(最小 0.5mm)。縫い目が閉じきらずに残る
隙間より大きく、隣の頂点まで潰さない長さにする。
"""

import bpy

from . import mesh_io

GROUP_NAME = "Muslin_Seam"
MODIFIER_NAME = "Muslin Weld"

AUTO_RATIO = 0.02
MIN_DISTANCE = 0.0005


def seam_vertices(obj):
    """有効な縫い目に関わる頂点の index(昇順)。"""
    mesh = obj.data
    enabled_uids = {s.uid for s in obj.muslin_seams if s.uid and s.enabled}
    out = set()
    codes, edges = mesh_io.read_seam_codes(mesh)
    for code, e in zip(codes or [], edges or []):
        if code and (code - 1) // 2 in enabled_uids:
            out.update(int(v) for v in e)
    # 旧形式(頂点番号で持つ縫い目)
    count = len(mesh.vertices)
    for seam in obj.muslin_seams:
        if seam.uid == 0 and seam.enabled:
            chain_a, chain_b = mesh_io.seam_chains(seam)
            out.update(v for v in list(chain_a) + list(chain_b) if v < count)
    return sorted(out)


def distance_for(obj):
    """溶接の距離。設定が 0 なら辺の長さから決める。"""
    set_value = float(obj.muslin.weld_distance)
    if set_value > 0.0:
        return set_value
    median = mesh_io.median_edge_length(obj.data)
    if not median:
        return MIN_DISTANCE
    return max(MIN_DISTANCE, AUTO_RATIO * float(median))


def _group_members(obj, group):
    return sorted(v.index for v in obj.data.vertices
                  if any(g.group == group.index for g in v.groups))


def _our_modifier(obj):
    mod = obj.modifiers.get(MODIFIER_NAME)
    return mod if mod is not None and mod.type == 'WELD' else None


def sync(obj):
    """設定に合わせて、頂点グループと Weld モディファイアを作る・更新する・片付ける。

    オブジェクトモードのときだけ動く(編集モードでは頂点グループを書けない)。
    戻り値: 動いたら True。
    """
    if obj is None or obj.type != 'MESH' or obj.mode != 'OBJECT':
        return False
    if obj.muslin.weld_seams:
        members = seam_vertices(obj)
        group = obj.vertex_groups.get(GROUP_NAME)
        if group is None:
            group = obj.vertex_groups.new(name=GROUP_NAME)
        current = _group_members(obj, group)
        if current != members:
            if current:
                group.remove(current)
            if members:
                group.add(members, 1.0, 'REPLACE')
        mod = _our_modifier(obj)
        if mod is None:
            mod = obj.modifiers.new(MODIFIER_NAME, 'WELD')
        mod.mode = 'ALL'
        mod.vertex_group = GROUP_NAME
        mod.merge_threshold = distance_for(obj)
        # 他のモディファイア(細分化など)より前で溶接する
        index = list(obj.modifiers).index(mod)
        if index != 0:
            obj.modifiers.move(index, 0)
        return True

    mod = _our_modifier(obj)
    if mod is not None:
        obj.modifiers.remove(mod)
    group = obj.vertex_groups.get(GROUP_NAME)
    if group is not None:
        obj.vertex_groups.remove(group)
    return True


def sync_if_enabled(obj):
    """縫い目や頂点が変わったあとに呼ぶ。溶接が有効な布だけ更新する。"""
    if obj is not None and obj.type == 'MESH' and obj.muslin.weld_seams:
        return sync(obj)
    return False


def update_property(self, _context):
    """`weld_seams` / `weld_distance` が変わったとき(プロパティの update コールバック)。"""
    obj = self.id_data
    if isinstance(obj, bpy.types.Object):
        sync(obj)


class MUSLIN_OT_update_weld(bpy.types.Operator):
    """縫い目の頂点グループと Weld モディファイアを、今の縫い目に合わせて更新する"""

    bl_idname = "muslin.update_weld"
    bl_label = "Update Weld"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT' and obj.muslin.weld_seams

    def execute(self, context):
        sync(context.active_object)
        self.report({'INFO'}, "縫い目の溶接を更新しました")
        return {'FINISHED'}


_classes = (MUSLIN_OT_update_weld,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
