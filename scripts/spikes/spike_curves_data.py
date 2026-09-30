"""スパイク: 新しい Curves データ(点・カーブごとの任意属性)を型紙の一次表現に使えるか。

Bezier の Curves に点の INT 属性 muslin_uid を持たせ、編集モードの標準操作で
属性がどう維持・複製・補間されるかを見る。操作ごとにプロセスを分けて走らせる
(Curves の編集は不正なデータで落ちるので、落ちた操作を切り分けるため)。
  blender -b --factory-startup --python scripts/spikes/spike_curves_data.py -- <case>
"""
import sys

import bpy


def P(*a):
    sys.stdout.write(" ".join(str(x) for x in a) + "\n")
    sys.stdout.flush()


def build(n=5, cyclic=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cu = bpy.data.hair_curves.new("H")
    ob = bpy.data.objects.new("H", cu)
    bpy.context.scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.curves.add_bezier()          # 正規の Bezier(2 点)を作る
    bpy.ops.object.mode_set(mode='OBJECT')
    cu = ob.data
    if n != 2:
        cu.resize_curves(sizes=[n], indices=[0])
    pos = cu.attributes["position"]
    for i in range(n):
        pos.data[i].vector = (i, i % 2, 0)
    for nm in ("handle_left", "handle_right"):
        a = cu.attributes[nm]
        for i in range(n):
            a.data[i].vector = (i, i % 2, 0)
    cu.attributes.new("muslin_uid", 'INT', 'POINT').data.foreach_set(
        "value", [10 * (k + 1) for k in range(n)])
    cu.attributes.new("muslin_piece", 'INT', 'CURVE').data.foreach_set("value", [7])
    if cyclic:
        cu.attributes["cyclic"].data[0].value = True if "cyclic" in cu.attributes else None
    return ob


def dump(ob, label):
    cu = ob.data
    uid = cu.attributes.get("muslin_uid")
    piece = cu.attributes.get("muslin_piece")
    P(f"  [{label}] curves={len(cu.curves)} points={len(cu.points)} attrs={sorted(a.name for a in cu.attributes if not a.name.startswith('.'))}")
    pos = cu.attributes["position"]
    for ci, cv in enumerate(cu.curves):
        rng = range(cv.first_point_index, cv.first_point_index + cv.points_length)
        row = " ".join("(%.1f,%.1f|uid%s)" % (pos.data[i].vector.x, pos.data[i].vector.y,
                                            uid.data[i].value if uid else "-") for i in rng)
        P(f"    c{ci} piece={piece.data[ci].value if piece else '-'} n={cv.points_length}: {row}")


def set_selection(ob, pts):
    cu = ob.data
    sel = cu.attributes.get(".selection")
    if sel is None:
        sel = cu.attributes.new(".selection", 'BOOLEAN', 'POINT')
    sel.data.foreach_set("value", [i in pts for i in range(len(cu.points))])
    for nm in (".selection_handle_left", ".selection_handle_right"):
        a = cu.attributes.get(nm) or cu.attributes.new(nm, 'BOOLEAN', 'POINT')
        a.data.foreach_set("value", [i in pts for i in range(len(cu.points))])


def run(title, op, select=None, n=5):
    P("\n== " + title)
    ob = build(n)
    dump(ob, "before")
    try:
        bpy.ops.object.mode_set(mode='EDIT')
        if select is None:
            bpy.ops.curves.select_all(action='SELECT')
        else:
            set_selection(ob, select)
        op()
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception as e:
        P("  ERROR", str(e).splitlines()[0][:150])
    dump(bpy.context.view_layer.objects.active, "after")


CASES = {
    "subdivide": lambda: run("全選択 + subdivide", lambda: bpy.ops.curves.subdivide(number_cuts=1)),
    "subdivide_part": lambda: run("P2,P3 のみ選択 + subdivide", lambda: bpy.ops.curves.subdivide(number_cuts=1), {1, 2}),
    "extrude_end": lambda: run("P5 のみ選択 + extrude", lambda: bpy.ops.curves.extrude(), {4}),
    "duplicate": lambda: run("P2,P3 のみ選択 + duplicate", lambda: bpy.ops.curves.duplicate(), {1, 2}),
    "delete": lambda: run("P3 のみ選択 + delete", lambda: bpy.ops.curves.delete(), {2}),
    "switch": lambda: run("全選択 + switch_direction", lambda: bpy.ops.curves.switch_direction()),
    "cyclic": lambda: run("全選択 + cyclic_toggle", lambda: bpy.ops.curves.cyclic_toggle()),
    "split": lambda: run("P2,P3 のみ選択 + split", lambda: bpy.ops.curves.split(), {1, 2}),
    "separate": lambda: run("P2,P3 のみ選択 + separate", lambda: bpy.ops.curves.separate(), {1, 2}),
}

if __name__ == "__main__":
    P("VERSION", bpy.app.version_string)
    which = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else list(CASES)
    for k in which:
        CASES[k]()
