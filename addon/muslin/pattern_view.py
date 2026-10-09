"""型紙と布を並べて見る作業画面(Marvelous Designer の 2D / 3D の並置に近いもの)。

専用の 2D エディタは作らず、3D ビューを左右に分け、片側を「Curve の型紙だけを真上から
見る、回転できない正投影」にする。型紙(Curve)は XY 平面に描くので、真上から見れば
そのまま 2D の型紙の画面になる。編集は Blender の Curve の編集をそのまま使い、
Shape Update で隣の 3D の布が追従する。

- 型紙の側はローカルビュー(その画面だけ他のオブジェクトを隠す)なので、布や体は映らない
- もう一度押すと、型紙の画面を閉じて元の 1 画面に戻す
"""

import bpy

from . import curve_pattern

def _tagged(area):
    """型紙の画面か。Area には任意のプロパティを持たせられないので、3D ビューの設定の
    組み合わせ(回転固定 + ローカルビュー + 正投影)で見分ける。"""
    space = area.spaces.active
    return (space.region_3d.lock_rotation and space.local_view is not None
            and space.region_3d.view_perspective == 'ORTHO')


def _window_region(area):
    return next((r for r in area.regions if r.type == 'WINDOW'), None)


def _frame(region_3d, obj, keep_height=False):
    """obj の範囲が画面に収まるよう、見る位置と距離を決める(向きは変えない)。

    keep_height なら高さ(Z)の広がりも距離に入れる(3D の布。型紙は XY だけ見る)。
    """
    from mathutils import Vector
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    size = max(hi.x - lo.x, hi.y - lo.y, (hi.z - lo.z) if keep_height else 0.0, 0.1)
    region_3d.view_location = (lo + hi) / 2.0
    region_3d.view_distance = size * (2.0 if keep_height else 1.2)


def open_pattern_view(context, curve):
    """今の 3D ビューを左右に分け、左側を型紙(curve)だけを真上から見る画面にする。"""
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        area = next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        raise RuntimeError("3D ビューがありません")
    before = set(context.screen.areas[:])
    with context.temp_override(area=area, region=_window_region(area)):
        bpy.ops.screen.area_split(direction='VERTICAL', factor=0.45)
    new = [a for a in context.screen.areas if a not in before]
    # 分けると元の領域が左右どちらかに残る。左側(x の小さい方)を型紙の画面にする
    pair = [area] + new
    pattern_area = min(pair, key=lambda a: a.x)
    space = pattern_area.spaces.active
    region = _window_region(pattern_area)
    selected = [o for o in context.view_layer.objects if o.select_get()]
    active = context.view_layer.objects.active
    try:
        for o in context.view_layer.objects:
            o.select_set(o is curve)
        context.view_layer.objects.active = curve
        with context.temp_override(area=pattern_area, region=region):
            if space.local_view is None:
                bpy.ops.view3d.localview(frame_selected=False)
            bpy.ops.view3d.view_axis(type='TOP', align_active=False)
            space.region_3d.view_perspective = 'ORTHO'
        # 分けた直後の画面には view_selected が効かない(大きさが決まる前)ので、型紙の
        # 範囲から見る位置と距離を直接決める
        _frame(space.region_3d, curve)
        space.region_3d.lock_rotation = True
        # 右(3D)は布に寄せる。向きはそのまま
        cloth = curve_pattern.cloth_of(curve)
        other = next((a for a in pair if a != pattern_area), None)
        if cloth is not None and other is not None:
            _frame(other.spaces.active.region_3d, cloth, keep_height=True)
        space.overlay.show_floor = False
        space.overlay.show_axis_x = False
        space.overlay.show_axis_y = False
    finally:
        for o in context.view_layer.objects:
            o.select_set(o in selected)
        context.view_layer.objects.active = active
    return pattern_area


def close_pattern_view(context):
    """型紙の画面を閉じる(隣の画面とつなげて 1 つに戻す)。閉じたら True。"""
    for area in list(context.screen.areas):
        if area.type == 'VIEW_3D' and _tagged(area):
            space = area.spaces.active
            with context.temp_override(area=area, region=_window_region(area)):
                if space.local_view is not None:
                    bpy.ops.view3d.localview(frame_selected=False)
                space.region_3d.lock_rotation = False
                bpy.ops.screen.area_close()
            return True
    return False


def has_pattern_view(screen):
    return any(a.type == 'VIEW_3D' and _tagged(a) for a in screen.areas)


class MUSLIN_OT_pattern_view(bpy.types.Operator):
    """型紙(Curve)と布を並べて見る。3D ビューを左右に分け、左を型紙だけを真上から見る画面にする。
    もう一度押すと元の 1 画面に戻す"""

    bl_idname = "muslin.pattern_view"
    bl_label = "Pattern View"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        if getattr(context, "window", None) is None or getattr(context, "screen", None) is None:
            return ui_poll.reject(cls, "画面を分けられるウィンドウがありません")
        if has_pattern_view(context.screen):
            return True
        obj = context.active_object
        curve = obj if obj is not None and obj.type == 'CURVE' else curve_pattern.curve_of(obj)
        if curve is None or curve_pattern.load_record(curve) is None:
            return ui_poll.reject(cls, "Curve Pattern の Curve か、そこから作った布を選んでください")
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        return True

    def execute(self, context):
        if close_pattern_view(context):
            self.report({'INFO'}, "型紙の画面を閉じました")
            return {'FINISHED'}
        obj = context.active_object
        curve = obj if obj.type == 'CURVE' else curve_pattern.curve_of(obj)
        try:
            open_pattern_view(context, curve)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "左に型紙、右に布を並べました(型紙の画面は真上から固定。"
                              "Curve を編集すると布が追従します)")
        return {'FINISHED'}


_classes = (MUSLIN_OT_pattern_view,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
