"""パターン描画モード: Curve に型紙の輪郭を手で描き、確定して Curve Pattern のピースにする。

- Draw Pattern: Curve Pattern の Curve(選んでいなければ新しく作る)を編集モードにし、真上から
  見る正投影にして、Blender の Draw ツール(Bezier に当てはめる)を選ぶ。左右対称を選ぶと
  Mirror モディファイアで反対側が見える(見た目だけ。Muslin は Curve の点を読む)
- Finish Piece: 描き始めてから足された線を閉じた Bezier にし、左右対称なら焼き込んで
  (`curve_draw.mirror_half`)、Mirror モディファイアを外し、Curve Pattern のピースとして取り込む

型紙は XY 平面に描く約束なので、真上から描く。点の Z は無視される。
"""

import bpy
import numpy as np

from . import curve_draw
from . import curve_pattern

# 描き始めたときの spline の数(これより後ろが、描いた線)
BASE_KEY = "muslin_draw_base"
# 左右対称で描いているか
MIRROR_KEY = "muslin_draw_mirror"
MIRROR_NAME = "Muslin Mirror"
# 始点と終点がこの距離(m)より近ければ、閉じた線として扱う(終点を落とす)
CLOSE_TOLERANCE = 0.02
DEFAULT_TARGET_LENGTH = 0.02


def is_drawing(curve):
    return curve is not None and curve.type == 'CURVE' and BASE_KEY in curve


def _view3d(context):
    if context.area is not None and context.area.type == 'VIEW_3D':
        return context.area
    return next((a for a in context.screen.areas if a.type == 'VIEW_3D'), None)


def start_drawing(context, curve=None, mirror=False):
    """描き始める。curve が None なら 3D カーソルの位置に新しい Curve を作る。戻り値: Curve。"""
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    if curve is None:
        data = bpy.data.curves.new("Pattern", 'CURVE')
        data.dimensions = '2D'
        curve = bpy.data.objects.new("Pattern", data)
        (context.collection or context.scene.collection).objects.link(curve)
        curve.location = context.scene.cursor.location
    for o in context.view_layer.objects:
        o.select_set(o is curve)
    context.view_layer.objects.active = curve
    curve[BASE_KEY] = len(curve.data.splines)
    curve[MIRROR_KEY] = bool(mirror)
    set_mirror_preview(curve, mirror)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.curve.select_all(action='DESELECT')
    area = _view3d(context)
    if area is not None:
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        with context.temp_override(area=area, region=region):
            try:
                bpy.ops.view3d.view_axis(type='TOP', align_active=False)
                area.spaces.active.region_3d.view_perspective = 'ORTHO'
                bpy.ops.wm.tool_set_by_id(name="builtin.draw")
            except Exception:          # 画面を操作できない文脈(バックグラウンドなど)
                pass
    paint = context.scene.tool_settings.curve_paint_settings
    paint.curve_type = 'BEZIER'
    paint.depth_mode = 'CURSOR'
    return curve


def set_mirror_preview(curve, mirror):
    """左右対称の見た目(Mirror モディファイア、X 軸、軸の上で止める)を付け外しする。"""
    mod = curve.modifiers.get(MIRROR_NAME)
    if mirror and mod is None:
        mod = curve.modifiers.new(MIRROR_NAME, 'MIRROR')
        mod.use_axis = (True, False, False)
        mod.use_clip = True
    elif not mirror and mod is not None:
        curve.modifiers.remove(mod)
    curve[MIRROR_KEY] = bool(mirror)


def _read(sp):
    pts = sp.bezier_points
    n = len(pts)
    co = np.empty((n, 3))
    hl = np.empty((n, 3))
    hr = np.empty((n, 3))
    pts.foreach_get("co", co.ravel())
    pts.foreach_get("handle_left", hl.ravel())
    pts.foreach_get("handle_right", hr.ravel())
    return co[:, :2], hl[:, :2], hr[:, :2]


def _write(curve, co, hl, hr):
    sp = curve.data.splines.new('BEZIER')
    sp.bezier_points.add(len(co) - 1)
    for bp, c, l_, r_ in zip(sp.bezier_points, co, hl, hr):
        bp.handle_left_type = 'FREE'
        bp.handle_right_type = 'FREE'
        bp.co = (c[0], c[1], 0.0)
        bp.handle_left = (l_[0], l_[1], 0.0)
        bp.handle_right = (r_[0], r_[1], 0.0)
    sp.use_cyclic_u = True
    return sp


def finish_drawing(context, curve, target_length=DEFAULT_TARGET_LENGTH):
    """描いた線を確定してピースにする。戻り値: (確定したピースの数, 警告の一覧)。"""
    if curve.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    base = int(curve.get(BASE_KEY, len(curve.data.splines)))
    mirror = bool(curve.get(MIRROR_KEY, False))
    drawn = [sp for sp in list(curve.data.splines)[base:] if sp.type == 'BEZIER' and len(sp.bezier_points) >= 2]
    others = [sp for sp in list(curve.data.splines)[base:] if sp not in drawn]
    warnings = []
    shapes = []
    for sp in drawn:
        co, hl, hr = _read(sp)
        if mirror:
            co, hl, hr = curve_draw.mirror_half(co, hl, hr)
        else:
            co, hl, hr = curve_draw.snap_closed(co, hl, hr, CLOSE_TOLERANCE)
        if len(co) < 3:
            warnings.append("点が少なすぎる線は使いませんでした")
            continue
        shapes.append((co, hl, hr))
    for sp in drawn + others:
        curve.data.splines.remove(sp)
    for co, hl, hr in shapes:
        _write(curve, co, hl, hr)
    set_mirror_preview(curve, False)
    del curve[BASE_KEY]
    curve.pop(MIRROR_KEY, None)
    if not shapes:
        return 0, warnings + ["確定できる線がありませんでした"]
    record = curve_pattern.load_record(curve)
    if record is None:
        curve_pattern.initialize(curve, target_length)
    else:
        warnings += curve_pattern._adopt(curve, record)
        curve_pattern.save_record(curve, record)
    return len(shapes), warnings


def cancel_drawing(context, curve):
    """描いた線を捨てて、描き始める前に戻す。"""
    if curve.mode == 'EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    base = int(curve.get(BASE_KEY, len(curve.data.splines)))
    for sp in list(curve.data.splines)[base:]:
        curve.data.splines.remove(sp)
    set_mirror_preview(curve, False)
    curve.pop(BASE_KEY, None)
    curve.pop(MIRROR_KEY, None)


def _active_curve(context):
    obj = context.active_object
    if obj is None:
        return None
    return obj if obj.type == 'CURVE' else curve_pattern.curve_of(obj)


class MUSLIN_OT_pattern_draw(bpy.types.Operator):
    """型紙の輪郭を手で描く。Curve Pattern の Curve(無ければ新しく作る)を真上から描ける状態にする"""

    bl_idname = "muslin.pattern_draw"
    bl_label = "Draw Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    mirror: bpy.props.BoolProperty(
        name="Mirror",
        description="左右対称に描く(縦の軸 x = 0 の片側だけ描く。確定のときに反対側を焼き込む)",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        curve = _active_curve(context)
        if is_drawing(curve):
            return ui_poll.reject(cls, "描いている途中です(Finish Piece で確定してください)")
        if curve is not None and curve_pattern.cloth_of(curve) is not None:
            from . import sim_state
            if sim_state.is_busy(curve_pattern.cloth_of(curve)):
                return ui_poll.reject(cls, "シミュレーション中は描けません。停止してください")
        return True

    def execute(self, context):
        curve = _active_curve(context)
        if curve is not None and curve.type != 'CURVE':
            curve = None
        curve = start_drawing(context, curve, self.mirror)
        self.report({'INFO'}, "描いて閉じたら Finish Piece(左右対称なら x = 0 の片側だけ描く)")
        return {'FINISHED'}


class MUSLIN_OT_pattern_draw_finish(bpy.types.Operator):
    """描いた線を閉じた輪郭にして、Curve Pattern のピースにする(左右対称なら反対側を焼き込む)"""

    bl_idname = "muslin.pattern_draw_finish"
    bl_label = "Finish Piece"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        if not is_drawing(_active_curve(context)):
            return ui_poll.reject(cls, "描いていません(Draw Pattern で描き始めてください)")
        return True

    def execute(self, context):
        curve = _active_curve(context)
        try:
            count, warnings = finish_drawing(context, curve)
        except (curve_pattern.CurvePatternError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        if count:
            self.report({'INFO'}, f"ピースを {count} 枚確定しました(Rebuild で布を作れます)")
        return {'FINISHED'}


class MUSLIN_OT_pattern_draw_cancel(bpy.types.Operator):
    """描いた線を捨てて、描き始める前に戻す"""

    bl_idname = "muslin.pattern_draw_cancel"
    bl_label = "Cancel Drawing"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_drawing(_active_curve(context))

    def execute(self, context):
        cancel_drawing(context, _active_curve(context))
        return {'FINISHED'}


class MUSLIN_OT_pattern_draw_mirror(bpy.types.Operator):
    """描いている途中で、左右対称の切り替え"""

    bl_idname = "muslin.pattern_draw_mirror"
    bl_label = "Mirror"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return is_drawing(_active_curve(context))

    def execute(self, context):
        curve = _active_curve(context)
        set_mirror_preview(curve, not bool(curve.get(MIRROR_KEY, False)))
        return {'FINISHED'}


_classes = (MUSLIN_OT_pattern_draw, MUSLIN_OT_pattern_draw_finish, MUSLIN_OT_pattern_draw_cancel,
            MUSLIN_OT_pattern_draw_mirror)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
