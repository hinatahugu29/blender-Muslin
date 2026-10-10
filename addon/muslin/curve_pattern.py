"""Curve Pattern: Blender の Curve を型紙の一次データにし、そこから布のメッシュを作る。

設計は docs/design/curve_pattern.md。bpy に依存しない部分は curve_ids / curve_eval /
curve_discretize / curve_update にあり、ここは Blender とのつなぎだけを持つ。

- Curve(従来型・Bezier・閉じた spline)が **一次データ**。点ごとに `radius` に uid、
  `weight_softbody` に検算値を書き、同一性を保つ(curve_ids)
- 記録(ピースの点 uid の並び、seam の定義、世代番号など)は Curve データの
  カスタムプロパティ `muslin_curve` に JSON で持つ。Undo と複製にそのまま乗る
- 布のメッシュ(Generated Mesh)は**派生データ**。`rebuild` で作り直せる。頂点属性に
  区間の uid と弧長比を持ち、Shape Update で Curve を評価し直せる
- 布のオブジェクトと Curve は ID ポインタで相互に結ぶ(名前を変えても切れない)

座標の対応は記録の `orientation` で決まる(布を初めて作るときに選ぶ):
- `FLAT`(既定): Curve の (x, y) → 布のローカル座標 (x, y, 0)。描いた平面のまま出す
- `STANDING`: (x, y) → (x, 0, y)。Add Pattern Piece と同じく XZ 平面に立てる
布は Curve と同じ向きで、Curve の輪郭の外側(選んだ方向に隙間を空けて)に置く。
体の周りへ立てて置くのは Arrange Around Collider の役目。
Curve オブジェクトのスケールは型紙の寸法として掛ける。
"""

import json

import bmesh  # noqa: F401  (将来の編集用に読み込んでおく)
import bpy
import numpy as np

from . import curve_arrange
from . import curve_discretize
from . import curve_eval
from . import curve_gusset
from . import curve_ids
from . import curve_transfer
from . import curve_update
from . import rest_shape
from . import seams

RECORD_KEY = "muslin_curve"          # Curve データの JSON 記録
CLOTH_KEY = "muslin_cloth"           # Curve オブジェクト → 布オブジェクト(ID ポインタ)
SOURCE_KEY = "muslin_curve_source"   # 布オブジェクト → Curve オブジェクト(ID ポインタ)
GEN_KEY = "muslin_gen"               # 布のメッシュの生成記録(JSON)

RECORD_VERSION = 1

ATTR_SEG = "muslin_gen_seg"
ATTR_SEG_END = "muslin_gen_seg_end"
ATTR_U = "muslin_gen_u"
ATTR_PIECE = "muslin_gen_piece"
ATTR_PATTERN = "muslin_gen_pattern"


class CurvePatternError(Exception):
    """Curve Pattern の操作を続けられない。メッセージはそのまま利用者に見せる。"""


# ---------------------------------------------------------------- 記録

def load_record(curve_obj):
    """Curve の記録(dict)。まだ Curve Pattern になっていなければ None。"""
    if curve_obj is None or curve_obj.type != 'CURVE':
        return None
    raw = curve_obj.data.get(RECORD_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def save_record(curve_obj, record):
    curve_obj.data[RECORD_KEY] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _stale_cloth(curve_obj):
    """ビューポートなどで消された布(シーンから外れただけで、Curve からの ID ポインタで
    データには残っているオブジェクト)。生きている布なら None。"""
    cloth = curve_obj.get(CLOTH_KEY) if curve_obj is not None else None
    if cloth is not None and cloth.type == 'MESH' and not cloth.users_collection:
        return cloth
    return None


def cloth_of(curve_obj):
    """Curve の布。消された布(どのコレクションにも無い)は無いものとして扱う。"""
    cloth = curve_obj.get(CLOTH_KEY) if curve_obj is not None else None
    if cloth is None or cloth.type != 'MESH' or not cloth.users_collection:
        return None
    return cloth


def _discard_stale_cloth(curve_obj):
    """消された布を、データからも片付ける(名前を空けて、新しい布を同じ名前で作れるように)。"""
    stale = _stale_cloth(curve_obj)
    if stale is None:
        return
    mesh = stale.data
    if CLOTH_KEY in curve_obj:
        del curve_obj[CLOTH_KEY]
    bpy.data.objects.remove(stale)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def curve_of(cloth_obj):
    src = cloth_obj.get(SOURCE_KEY) if cloth_obj is not None else None
    return src if src is not None and src.type == 'CURVE' else None


def _new_record(target_length):
    return {"version": RECORD_VERSION, "gen_id": 0, "target_edge_length": float(target_length),
            "next_uid": 1, "next_piece_uid": 1, "pieces": [], "seams": [], "flipped": [],
            "orientation": DEFAULT_ORIENTATION}


# 布の向き。記録に持ち、布を初めて作るときに決める(以後は変えない)
ORIENTATIONS = ('FLAT', 'STANDING')
DEFAULT_ORIENTATION = 'FLAT'


def orientation_of(record):
    """記録の向き。持っていなければ既定(平ら)。"""
    value = record.get("orientation", DEFAULT_ORIENTATION)
    return value if value in ORIENTATIONS else DEFAULT_ORIENTATION


# ---------------------------------------------------------------- Curve の読み出し

def read_splines(curve_obj):
    """Curve の spline を読む(閉じた Bezier だけが型紙になる)。

    戻り値: [{"co", "hl", "hr"(座標 (n, 2)。オブジェクトのスケールを掛けた寸法),
              "radius", "weight"(点ごと), "uids"(復元した uid。未知の点は None),
              "cyclic", "bezier"}]
    """
    if curve_obj.mode == 'EDIT':
        curve_obj.update_from_editmode()
    sx, sy, _sz = curve_obj.matrix_world.to_scale()
    out = []
    for sp in curve_obj.data.splines:
        if sp.type != 'BEZIER':
            out.append({"bezier": False, "cyclic": sp.use_cyclic_u, "co": np.zeros((0, 2)),
                        "hl": np.zeros((0, 2)), "hr": np.zeros((0, 2)), "radius": [],
                        "weight": [], "uids": []})
            continue
        pts = sp.bezier_points
        n = len(pts)
        co = np.empty((n, 3))
        hl = np.empty((n, 3))
        hr = np.empty((n, 3))
        pts.foreach_get("co", co.ravel())
        pts.foreach_get("handle_left", hl.ravel())
        pts.foreach_get("handle_right", hr.ravel())
        radius = np.empty(n)
        weight = np.empty(n)
        pts.foreach_get("radius", radius)
        pts.foreach_get("weight_softbody", weight)
        scale = np.array([sx, sy])
        out.append({
            "bezier": True, "cyclic": bool(sp.use_cyclic_u),
            "co": co[:, :2] * scale, "hl": hl[:, :2] * scale, "hr": hr[:, :2] * scale,
            "radius": radius, "weight": weight,
            "uids": curve_ids.decode_all(radius, weight),
        })
    return out


def _write_uids(curve_obj, per_spline_uids):
    """spline ごとの uid の並びを、点の radius / weight_softbody に書く。"""
    for sp, uids in zip(curve_obj.data.splines, per_spline_uids):
        if sp.type != 'BEZIER':
            continue
        for p, uid in zip(sp.bezier_points, uids):
            radius, weight = curve_ids.encode(uid)
            p.radius = radius
            p.weight_softbody = weight


def _polygon_area(poly):
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


_NESTED_MESSAGE = ("輪郭が XY 平面で 3 重以上に重なっています(Z 方向に重ねたピースも重なりと見なされます)。"
                   "型紙のピースは XY 平面に重ならないよう並べてください(穴は 1 段だけ作れます)")


# 穴の面積が外周のこの割合を超えたら、重ねて描いた 2 枚とみなす(縁が数 % しかない穴は
# 型紙としてまず無い)
STACKED_HOLE_RATIO = 0.9
_STACKED_MESSAGE = ("同じ大きさの輪郭が XY 平面で重なっています(Z 方向に重ねて描いた 2 枚は、"
                    "穴あきの 1 枚と解釈されてしまいます)。ピースは XY 平面に並べて描き、"
                    "袋にするなら Stack Pieces for Bag で重ねてください")


def _classify_holes(splines):
    """閉じた spline ごとに、外周か穴か(穴なら外側の spline の index)を決める。

    1 段の入れ子だけを扱う(穴の中の島は扱わない)。
    """
    from . import delaunay2d
    polys = {}
    for i, s in enumerate(splines):
        if s["bezier"] and s["cyclic"] and len(s["co"]) >= 3:
            polys[i] = s["co"]
    parent = {}
    for i, poly in polys.items():
        containing = [j for j, other in polys.items()
                      if j != i and delaunay2d.point_in_rings(poly[:1], [other])[0]]
        if len(containing) > 1:
            raise CurvePatternError(_NESTED_MESSAGE)
        parent[i] = containing[0] if containing else None
    for i, p in parent.items():
        if p is not None and parent.get(p) is not None:
            raise CurvePatternError(_NESTED_MESSAGE)
        # 外周とほぼ同じ大きさの穴は、Z 方向に重ねて描いた 2 枚とみなす(型紙は XY 平面で
        # 見るので、そのままでは「縁の細い穴あきの 1 枚」になってしまう)
        if p is not None and _polygon_area(polys[i]) > STACKED_HOLE_RATIO * _polygon_area(polys[p]):
            raise CurvePatternError(_STACKED_MESSAGE)
    return parent


# ---------------------------------------------------------------- 初期化と構造の取り込み

def initialize(curve_obj, target_length):
    """Curve を Curve Pattern にする。全ての点に uid を振り、ピースを記録する。"""
    if curve_obj.type != 'CURVE':
        raise CurvePatternError("Curve オブジェクトを選んでください")
    if load_record(curve_obj) is not None:
        raise CurvePatternError("すでに Curve Pattern です(作り直すには Rebuild を使います)")
    if curve_obj.mode == 'EDIT':
        raise CurvePatternError("オブジェクトモードで実行してください")
    record = _new_record(target_length)
    _adopt(curve_obj, record)
    save_record(curve_obj, record)
    return record


def _adopt(curve_obj, record):
    """今の Curve の構造を、新しい正として記録に取り込む(uid の振り直しを含む)。

    - 目印が合う点は uid を保つ。目印が合わない点(細分化・手描き)と、同じ uid の 2 つ目以降
      (複製)には新しい uid を振る
    - spline は点の uid の重なりで、記録のピースと対応付ける。対応するものが無ければ新しいピース
    - 閉じていない、または 3 点未満の spline は型紙にできないので取り込まない(理由を返す)
    戻り値: 警告の一覧
    """
    splines = read_splines(curve_obj)
    warnings = []
    recorded = _recorded_shapes(record)
    taken, new_splines, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    line_uids = {ln["line_uid"] for ln in record.get("lines", [])}

    used = {u for s in splines for u in s["uids"] if u is not None}
    next_uid = max([record["next_uid"]] + [u + 1 for u in used])
    seen = set()
    per_spline = []
    for si, s in enumerate(splines):
        if not s["bezier"]:
            warnings.append(f"spline {si} は Bezier ではないので型紙にしません")
            per_spline.append([])
            continue
        uids = []
        for u in s["uids"]:
            if u is None or u in seen:
                u = next_uid
                next_uid += 1
            seen.add(u)
            uids.append(u)
        per_spline.append(uids)
    _write_uids(curve_obj, per_spline)
    record["next_uid"] = next_uid

    parent = _classify_holes(splines)
    pieces = []
    next_piece = record["next_piece_uid"]
    piece_of = {}
    containers = _line_containers(splines, parent)
    for si, s in enumerate(splines):
        if si not in parent:
            if s["bezier"] and si not in containers:
                if not s["cyclic"] and len(s["co"]) >= 2:
                    warnings.append(f"spline {si} は開いた線ですが、どのピースの内側にもないので使いません")
                else:
                    warnings.append(f"spline {si} は閉じていない(または 3 点未満)ので型紙にしません")
            continue
        if si in taken and taken[si] not in line_uids:
            piece_of[si] = taken[si]
        else:
            piece_of[si] = next_piece
            next_piece += 1
    for si in sorted(piece_of):
        p = parent[si]
        pieces.append({"piece_uid": piece_of[si], "uids": per_spline[si], "cyclic": True,
                       "hole_of": piece_of[p] if p is not None else None})
    # 内部線: ピースの内側にある開いた Bezier(キルティングの縫い線)
    old_lines = {ln["line_uid"]: ln for ln in record.get("lines", [])}
    lines = []
    for si in sorted(containers):
        if si in taken and taken[si] in line_uids:
            line_uid = taken[si]
        else:
            line_uid = next_piece
            next_piece += 1
        entry = dict(old_lines.get(line_uid, {}))
        entry.update({"line_uid": line_uid, "uids": per_spline[si], "line_of": piece_of[containers[si]]})
        lines.append(entry)
    record["pieces"] = pieces
    record["lines"] = lines
    record["next_piece_uid"] = next_piece
    return warnings


def _recorded_shapes(record):
    """記録した輪郭と内部線の {uid: 点の uid の並び}(spline との対応付けに使う。uid は共通の番号)。"""
    shapes = {p["piece_uid"]: p["uids"] for p in record["pieces"]}
    for ln in record.get("lines", []):
        shapes[ln["line_uid"]] = ln["uids"]
    return shapes


def _line_containers(splines, parent):
    """開いた Bezier(2 点以上)のうち、ピースの内側にあるものの {spline の index: ピースの spline の index}。

    全ての点が、ある外周の内側で、その穴の外側にあること。
    """
    from . import delaunay2d
    outers = [i for i, p in parent.items() if p is None]
    holes = {i: [j for j, p in parent.items() if p == i] for i in outers}
    out = {}
    for si, s in enumerate(splines):
        if not s["bezier"] or s["cyclic"] or len(s["co"]) < 2:
            continue
        for i in outers:
            rings = [splines[i]["co"]] + [splines[j]["co"] for j in holes[i]]
            if delaunay2d.point_in_rings(s["co"], rings).all():
                out[si] = i
                break
    return out


def lines_of(record, piece_uid=None):
    """内部線の記録の一覧(piece_uid を渡せば、そのピースのものだけ)。"""
    return [ln for ln in (record or {}).get("lines", [])
            if piece_uid is None or ln["line_of"] == piece_uid]


def structure_problems(curve_obj, record=None):
    """記録した構造と今の Curve を比べ、Shape Update を止める理由の一覧を返す。空なら追従できる。"""
    record = record or load_record(curve_obj)
    if record is None:
        return ["Curve Pattern として初期化されていません"]
    splines = read_splines(curve_obj)
    recorded = _recorded_shapes(record)
    lines = {ln["line_uid"]: ln for ln in record.get("lines", [])}
    taken, new_splines, gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    reasons = []
    for si in new_splines:
        if splines[si]["bezier"] and splines[si]["cyclic"]:
            reasons.append(f"新しい輪郭(spline {si})があります")
        elif splines[si]["bezier"] and len(splines[si]["co"]) >= 2:
            try:
                inside = si in _line_containers(splines, _classify_holes(splines))
            except CurvePatternError:
                inside = False
            if inside:
                reasons.append(f"新しい内部線(spline {si})があります")
    for pid in gone:
        if pid in lines:
            reasons.append(f"内部線 {pid} が無くなりました")
        else:
            reasons.append(f"ピース {pid} の輪郭が無くなりました")
    for si, pid in sorted(taken.items()):
        if pid in lines:
            d = curve_ids.diff_outline(lines[pid]["uids"], splines[si]["uids"], False, splines[si]["cyclic"])
            for why in d["reasons"]:
                reasons.append(f"内部線 {pid}: {why}")
            continue
        piece = next(p for p in record["pieces"] if p["piece_uid"] == pid)
        d = curve_ids.diff_outline(piece["uids"], splines[si]["uids"], True, splines[si]["cyclic"])
        for why in d["reasons"]:
            reasons.append(f"ピース {pid}: {why}")
    return reasons


def current_outlines(curve_obj, record=None, derive=True):
    """今の Curve を `curve_update.update` に渡す形にする。{輪郭(ピース)・内部線の uid: 輪郭}

    derive なら、Quilt Through が裏へ写した線は Curve 上の座標ではなく、表の線から導いた形にする
    (表の線を動かせば、写した線も一緒に動く。`_quilt_copy_geometry`)。
    """
    record = record or load_record(curve_obj)
    splines = read_splines(curve_obj)
    recorded = _recorded_shapes(record)
    taken, _new, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    out = {}
    for si, pid in taken.items():
        s = splines[si]
        out[pid] = {"uids": [u for u in s["uids"]], "co": s["co"], "hl": s["hl"], "hr": s["hr"]}
    if derive:
        for uid, geometry in _quilt_copy_geometry(record, out).items():
            out[uid] = dict(out[uid], **geometry)
    return out


def _outlines_for_generation(curve_obj, record):
    splines = read_splines(curve_obj)
    recorded = {p["piece_uid"]: p for p in record["pieces"]}
    lines = {ln["line_uid"]: ln for ln in record.get("lines", [])}
    taken, _new, _gone = curve_ids.match_pieces(_recorded_shapes(record), [s["uids"] for s in splines])
    gussets = gussets_of(record)
    derived = _quilt_copy_geometry(record, current_outlines(curve_obj, record, derive=False))
    outlines = []
    for si, pid in sorted(taken.items(), key=lambda kv: kv[1]):
        s = splines[si]
        if pid in lines:
            geo = derived.get(pid, s)
            outlines.append({"piece_uid": pid, "uids": list(s["uids"]), "co": geo["co"], "hl": geo["hl"],
                             "hr": geo["hr"], "hole_of": None, "line_of": lines[pid]["line_of"]})
            continue
        outlines.append({"piece_uid": pid, "uids": list(s["uids"]), "co": s["co"], "hl": s["hl"],
                         "hr": s["hr"], "hole_of": recorded[pid]["hole_of"],
                         "grid": pid in gussets})
    return outlines


# ---------------------------------------------------------------- seam

def _all_seam_uids():
    used = 0
    for obj in bpy.data.objects:
        for seam in getattr(obj, "muslin_seams", []):
            used = max(used, seam.uid)
        if obj.type == 'CURVE':
            rec = load_record(obj)
            if rec:
                used = max([used] + [s["uid"] for s in rec["seams"]])
    return used + 1


def add_seam(curve_obj, ranges_a, ranges_b, name=None):
    """縫い目を定義する。ranges は [(始点 uid, t0, 終点 uid, t1), ...](各辺が 1 本の連なりになること)。

    始点 uid の区間の弧長比 t0 から、終点 uid の区間の弧長比 t1 まで、輪郭の向きに沿って進む。
    戻り値: 縫い目の uid
    """
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    known = {u for p in record["pieces"] for u in p["uids"]}
    for rng in list(ranges_a) + list(ranges_b):
        if rng[0] not in known or rng[2] not in known:
            raise CurvePatternError(f"縫い目の区間の点 {rng[0]}/{rng[2]} が Curve にありません")
    uid = _all_seam_uids()

    def clean(ranges):
        return [[int(r[0]), float(r[1]), int(r[2]), float(r[3])] for r in ranges]

    record["seams"].append({
        "uid": uid, "name": name or f"Seam {len(record['seams']) + 1}", "invert": False,
        "enabled": True, "a": clean(ranges_a), "b": clean(ranges_b),
    })
    save_record(curve_obj, record)
    return uid


def _faces_each_other(record, mesh_data, seam):
    """縫い目の両側が、裏返したピースとそうでないピースか(= 向かい合わせに縫う袋の縁)。"""
    flipped = flipped_pieces(record)
    if not flipped:
        return False
    sides = []
    for key in ("a", "b"):
        if not seam[key]:
            return False
        ring = _ring_containing(mesh_data, seam[key][0][0])
        if ring is None:
            return False
        sides.append(int(ring["piece_uid"]) in flipped)
    return sides[0] != sides[1]


def set_seam_folded(curve_obj, uid, folded):
    """縫い目の折り返しの指定を記録に書く(None で自動に戻す)。"""
    record = load_record(curve_obj)
    if record is None:
        return
    for seam in record["seams"]:
        if seam["uid"] == uid:
            if folded is None:
                seam.pop("folded", None)
            else:
                seam["folded"] = bool(folded)
            save_record(curve_obj, record)
            return


def _ring_containing(mesh_data, uid):
    for ring in mesh_data["rings"]:
        if uid in ring["uids"]:
            return ring
    return None


def _write_seams(cloth, record, mesh_data):
    """縫い目の定義を、布のメッシュの辺の属性 `muslin_seam` と `muslin_seams` に展開する。

    戻り値: 使えなかった縫い目の名前の一覧(区間が輪郭に無い、片側が 1 本の連なりにならない)
    """
    mesh = cloth.data
    edge_index = {tuple(sorted(e.vertices)): e.index for e in mesh.edges}
    codes = np.zeros(len(mesh.edges), dtype=np.int32)
    broken = []
    edge_vertices = [tuple(e.vertices) for e in mesh.edges]
    cloth.muslin_seams.clear()
    for seam in record["seams"]:
        ok = True
        for side, key in ((0, "a"), (1, "b")):
            pairs = []
            for start, t0, end, t1 in seam[key]:
                ring = _ring_containing(mesh_data, start)
                if ring is None or end not in ring["uids"]:
                    ok = False
                    continue
                pairs += curve_discretize.expand_range(ring, start, t0, end, t1)
            for a, b in pairs:
                codes[edge_index[tuple(sorted((a, b)))]] = seams.seam_code(seam["uid"], side)
        for side in (0, 1):
            if len(seams.chains_from_codes(codes, edge_vertices, seam["uid"], side)) != 1:
                ok = False
        if not ok:
            broken.append(seam["name"])
        item = cloth.muslin_seams.add()
        item.name = seam["name"]
        item.uid = seam["uid"]
        item.invert = bool(seam["invert"])
        item.enabled = bool(seam["enabled"])
        # 折り返し(袋の縁)。記録に指定が無ければ、裏返したピースとそうでないピースを
        # つなぐ縫い目(Stack Pieces で向かい合わせにした縁)を折り返しとみなす。
        # 書き込みの間は記録へ書き戻す更新を走らせない(記録が正なので)
        folded = seam.get("folded")
        if folded is None:
            folded = _faces_each_other(record, mesh_data, seam)
        item["folded"] = bool(folded)

    attr = mesh.attributes.get(seams.SEAM_ATTRIBUTE)
    if attr is None:
        attr = mesh.attributes.new(seams.SEAM_ATTRIBUTE, 'INT', 'EDGE')
    attr.data.foreach_set("value", codes)
    from . import weld
    weld.sync_if_enabled(cloth)
    return broken


def selection_runs_of(curve_obj):
    """選択中の点の連なり。[(spline の index, [点の index, ...]), ...](輪郭に沿った順)。"""
    if curve_obj.mode == 'EDIT':
        curve_obj.update_from_editmode()
    out = []
    for si, sp in enumerate(curve_obj.data.splines):
        if sp.type != 'BEZIER':
            continue
        flags = [bool(p.select_control_point) for p in sp.bezier_points]
        for run in curve_ids.selection_runs(flags, sp.use_cyclic_u):
            out.append((si, run))
    return out


def seam_from_selection(curve_obj, name=None):
    """選んだ 2 つの点の連なりを、縫い目の両側にする。戻り値: 縫い目の uid。

    連なりは、最初に選んだ点から最後の点までの区間になる。輪郭の向きは自動で決まる
    (縫い合わせるとき、向きは今の形から判定するので)。
    """
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    problems = structure_problems(curve_obj, record)
    if problems:
        raise CurvePatternError("点の構成が変わっています(Rebuild してから縫い目を作ってください): "
                                + problems[0])
    splines = read_splines(curve_obj)
    runs = selection_runs_of(curve_obj)
    ranges = []
    for si, run in runs:
        uids = splines[si]["uids"]
        if len(run) < 2:
            continue
        # 閉じた輪郭を全部選んだときは、一周する縫い目にする(クッションの外周)。
        # 終点を最後の点にすると最後の 1 辺が縫われず、袋が開いたままになる
        whole_ring = splines[si]["cyclic"] and len(run) == len(uids)
        if whole_ring:
            ranges.append((uids[run[0]], 0.0, uids[run[0]], 0.0))
        else:
            ranges.append((uids[run[0]], 0.0, uids[run[-1]], 0.0))
    if len(runs) != 2 or len(ranges) != 2:
        raise CurvePatternError(
            f"縫い合わせる 2 か所の点の連なりを選んでください(今は {len(runs)} か所。"
            "各連なりは 2 点以上)")
    uid = add_seam(curve_obj, [ranges[0]], [ranges[1]], name=name)
    apply_seams(curve_obj)
    return uid


def remove_seam(curve_obj, uid):
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    before = len(record["seams"])
    record["seams"] = [s for s in record["seams"] if s["uid"] != uid]
    if len(record["seams"]) == before:
        raise CurvePatternError(f"縫い目 {uid} はありません")
    save_record(curve_obj, record)
    apply_seams(curve_obj)


# ---------------------------------------------------------------- ゴム紐(Curve 側)

def add_elastic(curve_obj, ranges, name=None, scale=0.8):
    """ゴム紐を Curve の区間で定義する。ranges は縫い目の片側と同じ [(始点 uid, t0, 終点 uid, t1), ...]。

    区間は何本でもよい(ウエストの前後など)。戻り値: ゴム紐の uid(この Curve の中で一意)
    """
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    known = {u for p in record["pieces"] for u in p["uids"]}
    for rng in ranges:
        if rng[0] not in known or rng[2] not in known:
            raise CurvePatternError(f"ゴム紐の区間の点 {rng[0]}/{rng[2]} が Curve にありません")
    elastics = record.setdefault("elastics", [])
    # 布で直接付けたゴム紐と番号が重ならないよう、全体で一意にする(縫い目と同じ考え方)
    from . import mesh_io
    uid = max([mesh_io.next_elastic_uid()] + [e["uid"] + 1 for e in elastics])
    elastics.append({
        "uid": uid, "name": name or f"Elastic {len(elastics) + 1}",
        "scale": float(scale), "enabled": True,
        "ranges": [[int(r[0]), float(r[1]), int(r[2]), float(r[3])] for r in ranges],
    })
    save_record(curve_obj, record)
    return uid


def elastic_from_selection(curve_obj, name=None, scale=0.8):
    """選んだ点の連なり(何か所でも)をゴム紐にする。輪郭を全部選べば一周。戻り値: uid"""
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    problems = structure_problems(curve_obj, record)
    if problems:
        raise CurvePatternError("点の構成が変わっています(Rebuild してからゴム紐を付けてください): "
                                + problems[0])
    splines = read_splines(curve_obj)
    ranges = []
    for si, run in selection_runs_of(curve_obj):
        uids = splines[si]["uids"]
        if len(run) < 2:
            continue
        if splines[si]["cyclic"] and len(run) == len(uids):
            ranges.append((uids[run[0]], 0.0, uids[run[0]], 0.0))
        else:
            ranges.append((uids[run[0]], 0.0, uids[run[-1]], 0.0))
    if not ranges:
        raise CurvePatternError("ゴムを入れる点の連なり(2 点以上)を選んでください")
    uid = add_elastic(curve_obj, ranges, name=name, scale=scale)
    apply_seams(curve_obj)
    return uid


def remove_elastic(curve_obj, uid):
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    elastics = record.get("elastics", [])
    record["elastics"] = [e for e in elastics if e["uid"] != uid]
    if len(record["elastics"]) == len(elastics):
        raise CurvePatternError(f"ゴム紐 {uid} はありません")
    save_record(curve_obj, record)
    # 布の側の項目と辺の印も消す(残すと、書き直しで「布で直接付けたもの」として残ってしまう)
    cloth = cloth_of(curve_obj)
    if cloth is not None:
        from . import mesh_io
        for i in reversed(range(len(cloth.muslin_elastics))):
            if cloth.muslin_elastics[i].uid == uid:
                cloth.muslin_elastics.remove(i)
        attr = cloth.data.attributes.get(mesh_io.ELASTIC_ATTRIBUTE)
        if attr is not None:
            codes = np.zeros(len(attr.data), dtype=np.int32)
            attr.data.foreach_get("value", codes)
            codes[codes == uid] = 0
            attr.data.foreach_set("value", codes)
    apply_seams(curve_obj)


def set_elastic(curve_obj, uid, scale=None, enabled=None):
    """ゴム紐の倍率・有効を記録に書く(布の Elastic の欄から変えたときに呼ばれる)。"""
    record = load_record(curve_obj)
    if record is None:
        return
    for e in record.get("elastics", []):
        if e["uid"] == uid:
            if scale is not None:
                e["scale"] = float(scale)
            if enabled is not None:
                e["enabled"] = bool(enabled)
            save_record(curve_obj, record)
            return


def _write_elastics(cloth, record, mesh_data, keep_local=False):
    """ゴム紐の定義を、布の辺の属性 `muslin_elastic` と `muslin_elastics` に展開する。

    戻り値: 使えなかったゴム紐の名前の一覧(区間が輪郭に無い)。
    keep_local なら、布のメッシュで直接付けたゴム紐(Curve に無いもの)を残す。メッシュを
    作り直していない(縫い目を足しただけなどの)書き直しのとき。作り直したときは辺が
    変わるので残せない(呼び出し側で警告する)。
    """
    from . import mesh_io
    mesh = cloth.data
    edge_index = {tuple(sorted(e.vertices)): e.index for e in mesh.edges}
    codes = np.zeros(len(mesh.edges), dtype=np.int32)
    broken = []
    curve_uids = {e["uid"] for e in record.get("elastics", [])}
    local = []
    if keep_local:
        old_codes, _edges = mesh_io.read_edge_ints(mesh, mesh_io.ELASTIC_ATTRIBUTE)
        local = [(e.name, e.uid, e.scale, e.enabled) for e in cloth.muslin_elastics
                 if e.uid not in curve_uids]
        if old_codes is not None and len(old_codes) == len(codes):
            keep = {u for _n, u, _s, _e in local}
            for i, c in enumerate(old_codes):
                if c in keep:
                    codes[i] = c
    cloth.muslin_elastics.clear()
    for name, uid, scale, enabled in local:
        item = cloth.muslin_elastics.add()
        item["name"], item["uid"], item["scale"], item["enabled"] = name, uid, scale, enabled
    for e in record.get("elastics", []):
        ok = True
        for start, t0, end, t1 in e["ranges"]:
            ring = _ring_containing(mesh_data, start)
            if ring is None or end not in ring["uids"]:
                ok = False
                continue
            for a, b in curve_discretize.expand_range(ring, start, t0, end, t1):
                codes[edge_index[tuple(sorted((a, b)))]] = e["uid"]
        if not ok:
            broken.append(e["name"])
        item = cloth.muslin_elastics.add()
        # 書き込みの間は記録へ書き戻す更新を走らせない(記録が正なので)
        item["name"] = e["name"]
        item["uid"] = e["uid"]
        item["scale"] = float(e["scale"])
        item["enabled"] = bool(e["enabled"])
    attr = mesh.attributes.get(mesh_io.ELASTIC_ATTRIBUTE)
    if attr is None:
        if not record.get("elastics") and not local:
            return broken
        attr = mesh.attributes.new(mesh_io.ELASTIC_ATTRIBUTE, 'INT', 'EDGE')
    attr.data.foreach_set("value", codes)
    return broken


def apply_seams(curve_obj):
    """縫い目の定義を、作り直さずに今の布へ書き直す。使えなかった縫い目の名前の一覧を返す。"""
    cloth = cloth_of(curve_obj)
    record = load_record(curve_obj)
    if cloth is None or record is None or generation_record(cloth) is None:
        return []
    sync_quilt_copies(curve_obj, record)
    mesh_data = reconstruct(cloth)
    # ゴム紐も Curve の区間で持つので、縫い目と一緒に書き直す(作り直していないので、
    # 布で直接付けたゴム紐も残す)
    return (_write_seams(cloth, record, mesh_data)
            + _write_elastics(cloth, record, mesh_data, keep_local=True))


def status(curve_obj):
    """パネル用の状態。"""
    record = load_record(curve_obj)
    if record is None:
        return {"initialized": False}
    return {
        "initialized": True,
        "pieces": len(record["pieces"]),
        # 穴でもマチでもない輪郭の数(= 袋にできる枚数)
        "panels": len(bag_panels(record)),
        "gusset": bool(gussets_of(record)),
        "lines": len(record.get("lines", [])),
        "quilted": any(s.get("quilt") for s in record["seams"]),
        "target": record["target_edge_length"],
        "problems": structure_problems(curve_obj, record),
        "cloth": cloth_of(curve_obj),
        "seams": [(s["uid"], s["name"]) for s in record["seams"]],
        "elastics": [(e["uid"], e["name"]) for e in record.get("elastics", [])],
        "gen_id": record["gen_id"],
    }


# ---------------------------------------------------------------- 派生データ(布のメッシュ)

def to_local(xy, orientation):
    """型紙の座標 (x, y) → 布のローカル座標。FLAT なら (x, y, 0)、STANDING なら (x, 0, y)。"""
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    out = np.zeros((len(xy), 3))
    out[:, 0] = xy[:, 0]
    out[:, 2 if orientation == 'STANDING' else 1] = xy[:, 1]
    return out


def flipped_pieces(record):
    """面を裏返すピースの piece_uid の集合(袋の下側になるピース)。"""
    return set(int(u) for u in (record or {}).get("flipped", []))


def orient_triangles(triangles, piece, flipped):
    """裏返すピースの三角形だけ、頂点の並びを逆にする(= 法線が逆を向く)。

    袋(クッション)は 2 枚を重ねて縫うので、下側になる枚の面は裏返っていないと、
    圧力が内側へ向いてしまい、陰影も暗くなる。現実の縫い方(2 枚重ねて裁ち、
    片方の表が下を向く)と同じ扱い。bpy に依存しない。
    """
    tris = np.asarray(triangles, dtype=np.int64).reshape(-1, 3).copy()
    if not flipped:
        return tris
    piece = np.asarray(piece)
    mask = np.isin(piece[tris[:, 0]], list(flipped))
    tris[mask] = tris[mask][:, ::-1]
    return tris


def _build_mesh(name, data, record):
    mesh = bpy.data.meshes.new(name)
    verts = to_local(data["positions"], orientation_of(record))
    tris = orient_triangles(data["triangles"], data["piece"], flipped_pieces(record))
    mesh.from_pydata(verts.tolist(), [], tris.tolist())
    mesh.update()

    def point_attr(attr_name, dtype, values):
        attr = mesh.attributes.new(attr_name, dtype, 'POINT')
        if dtype == 'FLOAT_VECTOR':
            attr.data.foreach_set("vector", np.asarray(values, dtype=np.float32).ravel())
        else:
            attr.data.foreach_set("value", np.asarray(values))

    point_attr(ATTR_SEG, 'INT', data["seg_start"].astype(np.int32))
    point_attr(ATTR_SEG_END, 'INT', data["seg_end"].astype(np.int32))
    point_attr(ATTR_U, 'FLOAT', data["u"].astype(np.float32))
    point_attr(ATTR_PIECE, 'INT', data["piece"].astype(np.int32))
    xy3 = np.zeros((len(data["positions"]), 3))
    xy3[:, :2] = data["positions"]
    point_attr(ATTR_PATTERN, 'FLOAT_VECTOR', xy3)

    q = curve_update.quality(data, data["positions"])
    gen = {
        "gen_id": record["gen_id"],
        "algorithm_version": data["algorithm_version"],
        "target_length": data["target_length"],
        "segment_counts": {str(k): v for k, v in data["segment_counts"].items()},
        "segment_lengths": {str(k): v for k, v in data["segment_lengths"].items()},
        "fingerprint": [len(mesh.vertices), len(mesh.edges), len(mesh.polygons)],
        "quality": q,
        "rings": [{"piece_uid": r["piece_uid"], "outline_uid": r["outline_uid"], "hole": r["hole"],
                   "uids": r["uids"], "start": int(r["vertices"][0]), "count": len(r["vertices"]),
                   "open": bool(r.get("open", False))}
                  for r in data["rings"]],
    }
    mesh[GEN_KEY] = json.dumps(gen, separators=(",", ":"))
    return mesh


def generation_record(cloth):
    raw = cloth.data.get(GEN_KEY) if cloth is not None and cloth.type == 'MESH' else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def reconstruct(cloth):
    """布のメッシュから、離散化の結果(`curve_discretize.discretize` と同じ形)を組み直す。

    Shape Update は生成時の座標・三角形・区間の対応だけを使う。それらはメッシュの頂点属性と
    生成記録に持っているので、メッシュが編集されていない限りこれで元に戻る。
    メッシュが生成時と食い違っていれば CurvePatternError。
    """
    gen = generation_record(cloth)
    if gen is None:
        raise CurvePatternError("この布は Curve Pattern から生成されたものではありません")
    mesh = cloth.data
    n = len(mesh.vertices)
    if [n, len(mesh.edges), len(mesh.polygons)] != gen["fingerprint"]:
        raise CurvePatternError("布のメッシュが生成時から編集されています(Rebuild で作り直してください)")

    def read(attr_name, dtype, width=1):
        attr = mesh.attributes.get(attr_name)
        if attr is None or len(attr.data) != n:
            raise CurvePatternError(f"属性 {attr_name} がありません(Rebuild で作り直してください)")
        if dtype == 'FLOAT_VECTOR':
            out = np.empty(n * 3, dtype=np.float32)
            attr.data.foreach_get("vector", out)
            return out.reshape(n, 3)
        out = np.empty(n, dtype=np.float32 if dtype == 'FLOAT' else np.int32)
        attr.data.foreach_get("value", out)
        return out

    seg_start = read(ATTR_SEG, 'INT').astype(np.int64)
    seg_end = read(ATTR_SEG_END, 'INT').astype(np.int64)
    u = read(ATTR_U, 'FLOAT').astype(np.float64)
    piece = read(ATTR_PIECE, 'INT').astype(np.int64)
    positions = read(ATTR_PATTERN, 'FLOAT_VECTOR')[:, :2].astype(np.float64)
    tris = np.empty(len(mesh.polygons) * 3, dtype=np.int64)
    mesh.polygons.foreach_get("vertices", tris)

    rings = []
    for r in gen["rings"]:
        vertices = np.arange(r["start"], r["start"] + r["count"])
        index_of = {uid: k for k, uid in enumerate(r["uids"])}
        seg_index = np.array([index_of[int(s)] for s in seg_start[vertices]])
        ring = {"piece_uid": r["piece_uid"], "outline_uid": r["outline_uid"], "hole": r["hole"],
                "uids": list(r["uids"]), "vertices": vertices, "s": seg_index + u[vertices],
                "segment_total": len(r["uids"]) - (1 if r.get("open") else 0)}
        if r.get("open"):
            ring["open"] = True
        rings.append(ring)
    return {
        "positions": positions, "triangles": tris.reshape(-1, 3),
        "boundary": seg_start >= 0, "seg_start": seg_start, "seg_end": seg_end, "u": u,
        "piece": piece, "rings": rings,
        "segment_counts": {int(k): v for k, v in gen["segment_counts"].items()},
        "segment_lengths": {int(k): v for k, v in gen["segment_lengths"].items()},
        "target_length": gen["target_length"], "algorithm_version": gen["algorithm_version"],
    }


# 布を初めて作るときに置く方向(Curve の輪郭の外側のどちらへ出すか)
SIDES = {'+X': (0, 1.0), '-X': (0, -1.0), '+Y': (1, 1.0), '-Y': (1, -1.0)}
DEFAULT_GAP = 0.1


def placement_offset(local, rotation, side='+X', gap=DEFAULT_GAP):
    """布を Curve の輪郭に重ねず、`side` の方向に `gap` だけ空けて置くためのずらし量(ワールド)。

    布は Curve と同じ原点・同じ向きで作ると、Curve の線にちょうど重なる(FLAT のとき)。
    そこから、輪郭のその方向の幅 + 隙間だけずらす。bpy に依存しない。
    local: 布のローカル座標 (n, 3) / rotation: Curve の回転行列 (3, 3)
    """
    axis, sign = SIDES.get(side, SIDES['+X'])
    world = np.asarray(local, dtype=np.float64) @ np.asarray(rotation, dtype=np.float64).T
    extent = float(np.ptp(world[:, axis])) if len(world) else 0.0
    offset = np.zeros(3)
    offset[axis] = sign * (extent + max(gap, 0.0))
    return offset


# 旧メッシュの外側だった新頂点がこの割合を超えたら、姿勢が歪むので警告する
OUTSIDE_WARN_RATIO = 0.02
# 頂点グループの重みを補間したあと、この重み以上の頂点だけを新しいグループに入れる
PIN_WEIGHT_KEEP = 0.5


def _prepare_carry(cloth, curve_obj, record):
    """Rebuild で引き継ぐ姿勢を、旧メッシュを捨てる前に集める。引き継ぐ姿勢が無ければ None。

    姿勢が型紙(平ら)のままなら引き継ぐ意味がない(新しい型紙のまま作ればよい)。
    旧メッシュが編集されている(生成時と食い違う)ときは引き継げない。
    頂点グループ(ピン留め)の重みも一緒に集め、姿勢と同じ補間で新しい頂点へ写す。
    戻り値: (dict(old, pose, old_xy, approximate, dressed, group_names, group_weights) または None, 理由 または None)
    """
    try:
        old = reconstruct(cloth)
    except CurvePatternError as exc:
        return None, f"姿勢は引き継げませんでした({exc})"
    n = len(cloth.data.vertices)
    pose = np.empty(n * 3, dtype=np.float32)
    cloth.data.vertices.foreach_get("co", pose)
    pose = pose.reshape(-1, 3).astype(np.float64)
    if np.abs(pose - to_local(old["positions"], orientation_of(record))).max() < 1e-6:
        return None, None                      # 平らなまま
    now = curve_transfer.old_pattern_now(old, current_outlines(curve_obj, record))
    names = [g.name for g in cloth.vertex_groups]
    weights = np.zeros((n, len(names)))
    for v in cloth.data.vertices:
        for g in v.groups:
            weights[v.index, g.group] = g.weight
    return {
        "old": old, "pose": pose, "group_names": names, "group_weights": weights,
        "old_xy": old["positions"] if now is None else now,
        "approximate": now is None,
        "dressed": rest_shape.is_dressed(cloth),
    }, None


def rebuild(context, curve_obj, target_length=None, keep_pose=True,
            orientation=None, side='+X', gap=DEFAULT_GAP):
    """Curve から布のメッシュを(作り直して)生成する。

    keep_pose なら、旧メッシュの姿勢(着せた形)を型紙空間を介して新しい頂点へ引き継ぐ。
    orientation / side / gap は布を初めて作るときだけ効く(向きは記録に残り、以後は変えない。
    すでに布があれば、その位置と向きのまま中身だけを作り直す)。
    走っているシミュレーションがあれば拒否する(構造を作り直すと状態を意味的に保てない)。
    戻り値: (布オブジェクト, 警告の一覧)
    """
    from . import sim_state

    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    if curve_obj.mode == 'EDIT':
        raise CurvePatternError("オブジェクトモードで実行してください")
    # 布を消してから Rebuild し直したとき、シーンから外れただけの古い布を使い回すと、
    # メッシュはできても見えない。片付けて、新しい布を作る(姿勢も引き継がない)
    _discard_stale_cloth(curve_obj)
    cloth = cloth_of(curve_obj)
    if cloth is not None and sim_state.is_busy(cloth):
        raise CurvePatternError("シミュレーション中は Rebuild できません。停止してから実行してください")

    warnings = []
    carry = None
    if cloth is not None and keep_pose and generation_record(cloth) is not None:
        # 点の uid を振り直す前(足された点がまだ未知のうち)に、旧メッシュの位置を今の Curve に置き直す
        carry, why = _prepare_carry(cloth, curve_obj, record)
        if why:
            warnings.append(why)

    recreated = _recreate_quilt_copies(curve_obj, record)
    warnings += _adopt(curve_obj, record)
    _rebind_quilt_copies(curve_obj, record, recreated)
    sync_quilt_copies(curve_obj, record)
    if target_length is not None:
        record["target_edge_length"] = float(target_length)
    outlines = _outlines_for_generation(curve_obj, record)
    if not outlines:
        raise CurvePatternError("型紙にできる輪郭(閉じた Bezier)がありません")
    try:
        data = curve_discretize.discretize(outlines, record["target_edge_length"])
    except curve_discretize.DiscretizeError as exc:
        raise CurvePatternError(str(exc)) from exc

    if cloth is None and orientation is not None:
        if orientation not in ORIENTATIONS:
            raise CurvePatternError(f"向きは {' / '.join(ORIENTATIONS)} のどれかです")
        record["orientation"] = orientation
    record["gen_id"] += 1
    name = f"{curve_obj.name}_Cloth"
    mesh = _build_mesh(name, data, record)
    local_flat = to_local(data["positions"], orientation_of(record))
    flat = local_flat.astype(np.float32).ravel()

    if cloth is None:
        cloth = bpy.data.objects.new(name, mesh)
        collections = list(curve_obj.users_collection) or [context.collection]
        collections[0].objects.link(cloth)
        # Curve と同じ原点・向きで作り、輪郭の外側へずらす(Curve の線に重ねない)
        rotation = curve_obj.matrix_world.to_3x3().normalized()
        offset = placement_offset(local_flat, rotation, side, gap)
        cloth.location = curve_obj.matrix_world.translation.copy()
        cloth.location.x += offset[0]
        cloth.location.y += offset[1]
        cloth.location.z += offset[2]
        cloth.rotation_euler = curve_obj.matrix_world.to_euler()
    else:
        old = cloth.data
        had_pins = any(len(v.groups) for v in old.vertices)
        cloth.data = mesh
        if old.users == 0:
            bpy.data.meshes.remove(old)
        cloth.vertex_groups.clear()
        if had_pins and carry is None:
            warnings.append("頂点グループ(ピン留め)は作り直しで失われました。付け直してください")
        # Curve で定義したゴム紐は作り直しても書き直す。布で直接付けたものだけが失われる
        curve_uids = {e["uid"] for e in record.get("elastics", [])}
        if any(e.uid not in curve_uids for e in cloth.muslin_elastics):
            warnings.append("布で直接付けたゴム紐は作り直しで失われました。"
                            "Curve Pattern パネルで Curve に付けると保たれます")

    curve_obj[CLOTH_KEY] = cloth
    cloth[SOURCE_KEY] = curve_obj

    # 型紙は Curve が決めるので確定扱いにする。姿勢は、引き継げれば旧メッシュの姿勢、
    # 無ければ平らな型紙のまま
    rest_shape.store_pattern(cloth, flat)
    if carry is not None:
        pose, outside, weights = curve_transfer.transfer(
            carry["old_xy"], carry["old"]["triangles"], carry["pose"], data["positions"],
            carry["old"]["piece"], data["piece"], values=carry["group_weights"])
        pose32 = pose.astype(np.float32).ravel()
        for k, group_name in enumerate(carry["group_names"]):
            group = cloth.vertex_groups.new(name=group_name)
            members = np.nonzero(weights[:, k] >= PIN_WEIGHT_KEEP)[0]
            for vi in members:
                group.add([int(vi)], float(min(weights[vi, k], 1.0)), 'REPLACE')
        cloth.data.vertices.foreach_set("co", pose32)
        cloth.data.update()
        rest_shape.store(cloth, pose32)
        if carry["dressed"]:
            rest_shape.mark_dressed(cloth)
        else:
            rest_shape.clear_dressed(cloth)
        if carry["approximate"]:
            warnings.append("Curve の点の構成が大きく変わったため、姿勢の引き継ぎは近似です")
        if outside > OUTSIDE_WARN_RATIO * len(pose):
            warnings.append(f"頂点の {outside * 100 // len(pose)}% は旧メッシュの外側だったため、"
                            "縁の姿勢で補っています(Curve を大きく広げたときに起きます)")
    else:
        rest_shape.store(cloth, flat)
        rest_shape.clear_dressed(cloth)
    cloth[rest_shape.LOCKED_FLAG] = True

    mesh_data = reconstruct(cloth)
    broken = _write_seams(cloth, record, mesh_data) + _write_elastics(cloth, record, mesh_data)
    if broken:
        warnings.append(f"使えない縫い目があります({', '.join(broken[:3])}"
                        f"{' ほか' if len(broken) > 3 else ''})。区間が輪郭に無いか、片側が 1 本につながりません")
    save_record(curve_obj, record)
    return cloth, warnings


def arrange_around(curve_obj, collider, margin=None, angles=None, base_z=None):
    """布のピースを、コライダー(体)の縦軸のまわりに巻き付けて置く。姿勢(rest)も置いた形にする。

    ピースは向き(度。0 = 正面 = -Y)ごとに円柱の外側へ置かれる。angles を省略すると、
    ピース数で周りに等間隔(1 枚は正面、2 枚は正面と背面)。着せた段階は解除される。
    高さは型紙の y をそのまま使い、型紙の y = 0 をワールドの高さ base_z に置く。
    base_z を省略すると布の原点の高さ。布が平らでも立っていても同じ置き方になる
    (以前は今の布の高さを使っていたので、平らに寝かせた布は1つの高さに潰れた)。
    戻り値: 警告の一覧
    """
    from . import sim_state

    cloth = cloth_of(curve_obj)
    if cloth is None:
        raise CurvePatternError("布がまだありません(Rebuild で作ってください)")
    if sim_state.is_busy(cloth):
        raise CurvePatternError("シミュレーション中は置き直せません。停止してください")
    if collider is None or collider.type != 'MESH':
        raise CurvePatternError("体にするメッシュ(コライダー)を指定してください")
    data = reconstruct(cloth)

    mw = np.array(cloth.matrix_world, dtype=np.float64)
    if base_z is None:
        base_z = float(mw[2, 3])

    cmw = np.array(collider.matrix_world, dtype=np.float64)
    cv = np.empty(len(collider.data.vertices) * 3, dtype=np.float32)
    collider.data.vertices.foreach_get("co", cv)
    collider_points = cv.reshape(-1, 3).astype(np.float64) @ cmw[:3, :3].T + cmw[:3, 3]
    axis_xy = (float(collider_points[:, 0].min() + collider_points[:, 0].max()) / 2.0,
               float(collider_points[:, 1].min() + collider_points[:, 1].max()) / 2.0)
    if margin is None:
        margin = 2.0 * float(cloth.muslin.collision_thickness) + 0.005

    pieces = sorted(set(int(x) for x in data["piece"]))
    groups = [np.nonzero(data["piece"] == pid)[0] for pid in pieces]
    arranged, warnings = _arrange(groups, data, base_z, collider_points, axis_xy, margin, angles)

    world = np.empty((len(data["positions"]), 3))
    for idx, pts in zip(groups, arranged):
        world[idx] = pts
    inv = np.linalg.inv(mw)
    local = world @ inv[:3, :3].T + inv[:3, 3]
    pose32 = local.astype(np.float32).ravel()
    cloth.data.vertices.foreach_set("co", pose32)
    cloth.data.update()
    rest_shape.store(cloth, pose32)
    rest_shape.clear_dressed(cloth)
    return warnings


# 袋にするときに 2 枚を離す既定の隙間。縫い目が閉じるときに潰れるので、厚みより少し広く取る
DEFAULT_BAG_GAP = 0.02


def stack_offsets(count, gap):
    """重ねる枚数ぶんの、面に垂直な方向のずらし量。中央をはさんで等間隔に並べる。

    2 枚なら ±gap/2。3 枚以上(マチのある袋など)でも等間隔に散らす。bpy に依存しない。
    """
    if count <= 1:
        return [0.0]
    span = gap * (count - 1)
    return [span / 2.0 - gap * k for k in range(count)]


def stack_pieces(context, curve_obj, gap=DEFAULT_BAG_GAP):
    """ピースを向かい合わせに重ねて置き、下側のピースの面を裏返す(クッションなどの袋)。

    体の周りに巻く `arrange_around` に対応する、体を使わない置き方。ピースを型紙の
    中心でそろえて重ねるので、外周を縫う縫い目がほとんど動かずに閉じる。
    2 枚目以降の面は裏返して記録する(圧力が外へ向き、陰影も正しくなる)。面を裏返すには
    メッシュを作り直すので、姿勢は引き継がない(置き直す操作なので引き継ぐ意味がない)。
    戻り値: (布オブジェクト, 警告の一覧)
    """
    from . import sim_state

    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    if curve_obj.mode == 'EDIT':
        raise CurvePatternError("オブジェクトモードで実行してください")
    cloth = cloth_of(curve_obj)
    if cloth is not None and sim_state.is_busy(cloth):
        raise CurvePatternError("シミュレーション中は置き直せません。停止してください")

    # 穴でもマチでもない輪郭の数 = 袋を作る枚数
    panels = bag_panels(record)
    if len(panels) < 2:
        raise CurvePatternError(
            f"袋にするには輪郭が 2 つ以上必要です(今は {len(panels)} つ)。"
            "同じ大きさのピースを XY 平面にもう 1 枚描いてください")

    # 下側になる枚の面を裏返す(先頭だけ表のまま)。現実の縫い方と同じ扱い
    record["flipped"] = [int(u) for u in panels[1:]]
    save_record(curve_obj, record)

    cloth, warnings = rebuild(context, curve_obj, keep_pose=False)
    local = stacked_layout(cloth, record, gap)
    pose32 = local.astype(np.float32).ravel()
    cloth.data.vertices.foreach_set("co", pose32)
    cloth.data.update()
    rest_shape.store(cloth, pose32)
    rest_shape.clear_dressed(cloth)
    return cloth, warnings


def stacked_layout(cloth, record=None, gap=DEFAULT_BAG_GAP):
    """袋のピースを向かい合わせに重ねた配置(布のローカル座標 (n, 3))。

    面の裏返しは記録(と生成したメッシュ)に入っているので、作り直さずに計算できる。
    Close Bag が、満杯に膨らんだ袋を詰め具合(Fill)まで戻すときにも使う
    (満杯から吸っても、張った布は曲げに強く縮まないので、平らに重ねた所から膨らませ直す)。
    """
    if record is None:
        source = curve_of(cloth)
        record = load_record(source) if source is not None else None
    if record is None:
        raise CurvePatternError("Curve Pattern の布ではありません")
    data = reconstruct(cloth)
    orientation = orientation_of(record)
    # 面に垂直な方向(FLAT なら Z、STANDING なら Y)
    normal_axis = 2 if orientation == 'FLAT' else 1
    # 表(先頭)の面が向く側。FLAT は +Z、STANDING は -Y((x, y) → (x, 0, y) で面の向きが -Y になる)。
    # 表をその側に置かないと、袋の面がすべて内を向き、圧力で内側へ潰れる
    outward = 1.0 if orientation == 'FLAT' else -1.0
    panels = bag_panels(record)

    groups = [np.nonzero(data["piece"] == pid)[0] for pid in panels]
    box = _box_layout(cloth, record, data, groups)
    if box is not None:
        return box
    local = to_local(data["positions"], orientation)
    # 型紙の中心をそろえて重ねる(ピースごとに中心が違っても向かい合うように)
    centers = [local[idx].mean(axis=0) for idx in groups]
    base = centers[0]
    for idx, center, shift in zip(groups, centers, stack_offsets(len(groups), gap)):
        local[idx] += base - center
        local[idx, normal_axis] += outward * shift
    return local


def _box_layout(cloth, record, data, groups):
    """マチのある袋の配置: 表と裏をマチの幅だけ離して重ね、マチを表の外周に沿って立てる。

    縫い目は初めから閉じた形になる(表の外周とマチの上の縁、裏の外周と下の縁が重なる)。
    マチが無い・表と裏が 2 枚でないときは None(呼び出し側が平らに重ねる)。
    """
    gussets = gussets_of(record)
    if not gussets or len(groups) != 2:
        return None
    source = curve_of(cloth)
    top = current_outlines(source, record).get(bag_panels(record)[0]) if source is not None else None
    g_idx = np.nonzero(np.isin(data["piece"], list(gussets)))[0]
    if top is None or not len(g_idx):
        return None
    xy = data["positions"]
    frame = np.zeros((len(xy), 3))
    frame[:, :2] = xy
    wall, height = curve_gusset.wall_positions(xy[g_idx], top)
    frame[g_idx] = wall
    top_idx, bottom_idx = groups
    frame[top_idx, 2] = height / 2.0
    frame[bottom_idx, :2] += xy[top_idx].mean(axis=0) - xy[bottom_idx].mean(axis=0)
    frame[bottom_idx, 2] = -height / 2.0
    # 型紙の面の座標 (x, y, 面に垂直) -> 布のローカル座標。STANDING は (x, -面に垂直, y)
    # (回転として写す。表の面は -Y を向くので、表を -Y の側に置く。重ねた配置と同じ)
    if orientation_of(record) == 'STANDING':
        return np.column_stack([frame[:, 0], -frame[:, 2], frame[:, 1]])
    return frame


# ---------------------------------------------------------------- マチ(M11)

DEFAULT_GUSSET_HEIGHT = 0.08
# マチの帯を Curve に置くとき、ほかの輪郭から離す距離
GUSSET_GAP = 0.05


def gussets_of(record):
    """マチのピースの piece_uid の集合。"""
    return set(int(u) for u in (record or {}).get("gussets", []))


def bag_panels(record):
    """袋の面になるピース(穴でもマチでもない輪郭)の piece_uid。記録の順。"""
    gussets = gussets_of(record)
    return [p["piece_uid"] for p in (record or {}).get("pieces", [])
            if p.get("hole_of") is None and p["piece_uid"] not in gussets]


def _check_bag_editable(context, curve_obj):
    from . import sim_state
    record = load_record(curve_obj)
    if record is None:
        raise CurvePatternError("Curve Pattern として初期化されていません")
    if curve_obj.mode == 'EDIT':
        raise CurvePatternError("オブジェクトモードで実行してください")
    cloth = cloth_of(curve_obj)
    if cloth is not None and sim_state.is_busy(cloth):
        raise CurvePatternError("シミュレーション中は変えられません。停止してください")
    return record


def add_gusset(context, curve_obj, height=DEFAULT_GUSSET_HEIGHT):
    """表と裏の 2 枚の袋にマチ(帯)を足し、縫い目を張って、箱形に置いた布を作る。

    帯は Curve の輪郭の下に長方形として描き足す(長さ = 表の外周、幅 = height)。
    縫い目の割り当ては `curve_gusset.plan`。マチと表・裏の縫い目は折り返し(角になる縁)、
    帯の両端の縫い目は、表の始点が角なら折り返し、滑らかなら平らにつなぐ。
    帯は縦横の格子で分ける(角で素直に折れるように。`curve_discretize._grid`)。
    戻り値: (布オブジェクト, 警告の一覧)
    """
    record = _check_bag_editable(context, curve_obj)
    if gussets_of(record):
        raise CurvePatternError("すでにマチがあります(作り直すには一度外してください)")
    panels = bag_panels(record)
    if len(panels) != 2:
        raise CurvePatternError(f"マチは表と裏の 2 枚の袋に付けます(今は {len(panels)} 枚)")
    outlines = current_outlines(curve_obj, record)
    if panels[0] not in outlines or panels[1] not in outlines:
        raise CurvePatternError("表か裏の輪郭が Curve にありません(Rebuild してください)")
    try:
        p = curve_gusset.plan(outlines[panels[0]], outlines[panels[1]], height)
    except curve_gusset.GussetError as exc:
        raise CurvePatternError(str(exc)) from exc

    # 帯を、全ての輪郭の下に置く(XY で重なると穴と解釈されるため)
    all_co = np.vstack([s["co"] for s in read_splines(curve_obj) if len(s["co"])])
    ox = float(all_co[:, 0].min())
    oy = float(all_co[:, 1].min()) - GUSSET_GAP - height
    sx, sy, _sz = curve_obj.matrix_world.to_scale()
    pts = [(ox + x, oy + y) for x, y in p["points"]]
    sp = curve_obj.data.splines.new('BEZIER')
    sp.bezier_points.add(len(pts) - 1)
    n = len(pts)
    for i, (x, y) in enumerate(pts):
        prev, nxt = pts[i - 1], pts[(i + 1) % n]
        bp = sp.bezier_points[i]
        bp.handle_left_type = 'FREE'
        bp.handle_right_type = 'FREE'
        bp.co = (x / sx, y / sy, 0.0)
        # 直線の区間(取っ手を隣の点への 1/3 に置く)
        bp.handle_left = ((x + (prev[0] - x) / 3.0) / sx, (y + (prev[1] - y) / 3.0) / sy, 0.0)
        bp.handle_right = ((x + (nxt[0] - x) / 3.0) / sx, (y + (nxt[1] - y) / 3.0) / sy, 0.0)
    sp.use_cyclic_u = True

    before = {q["piece_uid"] for q in record["pieces"]}
    warnings = _adopt(curve_obj, record)
    new = [q for q in record["pieces"] if q["piece_uid"] not in before]
    if len(new) != 1 or len(new[0]["uids"]) != len(p["points"]):
        raise CurvePatternError("マチの輪郭を取り込めませんでした")
    g_uids = new[0]["uids"]
    record["gussets"] = [int(new[0]["piece_uid"])]

    def resolve(rng):
        a, t0, b, t1 = rng
        a = g_uids[a[1]] if isinstance(a, tuple) else a
        b = g_uids[b[1]] if isinstance(b, tuple) else b
        return [[int(a), float(t0), int(b), float(t1)]]

    uid = _all_seam_uids()
    for name, side_a, side_b, folded in p["seams"]:
        record["seams"].append({
            "uid": uid, "name": name, "invert": False, "enabled": True,
            "a": resolve(side_a), "b": resolve(side_b), "folded": bool(folded), "gusset": True,
        })
        uid += 1
    save_record(curve_obj, record)
    cloth, more = stack_pieces(context, curve_obj)
    return cloth, warnings + more


def remove_gusset(context, curve_obj):
    """マチの帯と、その縫い目を取り除き、平らに重ねた袋に戻す。戻り値: (布 または None, 警告)"""
    record = _check_bag_editable(context, curve_obj)
    gussets = gussets_of(record)
    if not gussets:
        raise CurvePatternError("マチがありません")
    splines = read_splines(curve_obj)
    recorded = {q["piece_uid"]: q["uids"] for q in record["pieces"]}
    taken, _new, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    doomed = sorted((si for si, pid in taken.items() if pid in gussets), reverse=True)
    for si in doomed:
        curve_obj.data.splines.remove(curve_obj.data.splines[si])
    record["seams"] = [s for s in record["seams"] if not s.get("gusset")]
    record.pop("gussets", None)
    warnings = _adopt(curve_obj, record)
    save_record(curve_obj, record)
    if cloth_of(curve_obj) is None:
        return None, warnings
    cloth, more = stack_pieces(context, curve_obj)
    return cloth, warnings + more


# ---------------------------------------------------------------- 内部線とキルティング(M11)

def _add_bezier(curve_obj, co, hl, hr, cyclic):
    """Curve に Bezier の spline を足す(座標は型紙の寸法。オブジェクトのスケールで割って置く)。"""
    sx, sy, _sz = curve_obj.matrix_world.to_scale()
    sp = curve_obj.data.splines.new('BEZIER')
    sp.bezier_points.add(len(co) - 1)
    for bp, c, l_, r_ in zip(sp.bezier_points, co, hl, hr):
        bp.handle_left_type = 'FREE'
        bp.handle_right_type = 'FREE'
        bp.co = (c[0] / sx, c[1] / sy, 0.0)
        bp.handle_left = (l_[0] / sx, l_[1] / sy, 0.0)
        bp.handle_right = (r_[0] / sx, r_[1] / sy, 0.0)
    sp.use_cyclic_u = cyclic
    return sp


def _whole_line(uids):
    return [[int(uids[0]), 0.0, int(uids[-1]), 0.0]]


def _panel_shift(outlines, panels):
    """表の型紙を裏の型紙へ重ねるずらし量(型紙の中心どうし。Stack Pieces と同じそろえ方)。"""
    top, bottom = outlines[panels[0]], outlines[panels[1]]
    t_segs = curve_gusset.outline_arcs(top["co"], top["hl"], top["hr"])[0]
    b_segs = curve_gusset.outline_arcs(bottom["co"], bottom["hl"], bottom["hr"])[0]
    return curve_gusset.centroid(b_segs) - curve_gusset.centroid(t_segs)


def _quilt_pairs(record):
    """キルティングの (表の線の uid, 写した線の uid, 縫い目)。"""
    return [(int(s["quilt"][0]), int(s["quilt"][1]), s) for s in record["seams"] if s.get("quilt")]


def _quilt_copy_geometry(record, outlines):
    """Quilt Through が裏へ写した線の、表の線から導いた形 {写した線の uid: {"co", "hl", "hr"}}。

    写した線は表の線に従う(表の線 + 型紙の中心のずれ)。表の線を動かせば、布の裏の筋も
    一緒に動く。点の数が表と違う(表の線の点を足した・消した)ものは導けないので含めない
    (そのときは表の線の構成の変化で Rebuild Required になり、Rebuild で写し直す)。
    """
    panels = bag_panels(record)
    copies = {ln["line_uid"] for ln in record.get("lines", []) if ln.get("quilt_copy")}
    if len(panels) != 2 or not copies or panels[0] not in outlines or panels[1] not in outlines:
        return {}
    shift = None
    out = {}
    for top_uid, copy_uid, _seam in _quilt_pairs(record):
        top, copy = outlines.get(top_uid), outlines.get(copy_uid)
        if copy_uid not in copies or top is None or copy is None or len(top["co"]) != len(copy["co"]):
            continue
        if shift is None:
            shift = _panel_shift(outlines, panels)
        out[copy_uid] = {k: np.asarray(top[k], dtype=np.float64) + shift for k in ("co", "hl", "hr")}
    return out


def sync_quilt_copies(curve_obj, record=None):
    """Curve 上の写した線を、表の線から導いた形に書き直す(見た目を布に合わせる)。

    編集モードの Curve には書けない(抜けるときに編集中のデータで上書きされる)ので何もしない。
    形が合っていれば書かない(呼ぶたびに Curve を変えて、読み直しを繰り返さないように)。
    戻り値: 書き直したか。
    """
    record = record or load_record(curve_obj)
    if record is None or curve_obj.mode == 'EDIT' or not _quilt_pairs(record):
        return False
    raw = current_outlines(curve_obj, record, derive=False)
    derived = _quilt_copy_geometry(record, raw)
    if not derived:
        return False
    splines = read_splines(curve_obj)
    taken, _new, _gone = curve_ids.match_pieces(_recorded_shapes(record), [s["uids"] for s in splines])
    index_of = {pid: si for si, pid in taken.items()}
    sx, sy, _sz = curve_obj.matrix_world.to_scale()
    wrote = False
    for uid, geo in derived.items():
        si = index_of.get(uid)
        if si is None:
            continue
        s = splines[si]
        if max(np.abs(geo[k] - s[k]).max() for k in ("co", "hl", "hr")) < 1e-7:
            continue
        try:
            for bp, c, l_, r_ in zip(curve_obj.data.splines[si].bezier_points, geo["co"], geo["hl"], geo["hr"]):
                bp.handle_left_type = 'FREE'
                bp.handle_right_type = 'FREE'
                bp.co = (c[0] / sx, c[1] / sy, bp.co[2])
                bp.handle_left = (l_[0] / sx, l_[1] / sy, bp.handle_left[2])
                bp.handle_right = (r_[0] / sx, r_[1] / sy, bp.handle_right[2])
        except AttributeError:
            return wrote                # 書き込めない文脈(描画中など)
        wrote = True
    return wrote


def _recreate_quilt_copies(curve_obj, record):
    """点の数が表と食い違った写した線を、表の線から写し直す(Rebuild の前に呼ぶ)。

    戻り値: [(縫い目, 足した spline の index)]。`_adopt` のあとに `_rebind_quilt_copies` へ渡す。
    """
    pairs = _quilt_pairs(record)
    if not pairs:
        return []
    panels = bag_panels(record)
    raw = current_outlines(curve_obj, record, derive=False)
    if len(panels) != 2 or panels[0] not in raw or panels[1] not in raw:
        return []
    copies = {ln["line_uid"] for ln in record.get("lines", []) if ln.get("quilt_copy")}
    stale = [(top_uid, copy_uid, seam) for top_uid, copy_uid, seam in pairs
             if copy_uid in copies and top_uid in raw
             and (copy_uid not in raw or len(raw[copy_uid]["co"]) != len(raw[top_uid]["co"]))]
    if not stale:
        return []
    shift = _panel_shift(raw, panels)
    splines = read_splines(curve_obj)
    taken, _new, _gone = curve_ids.match_pieces(_recorded_shapes(record), [s["uids"] for s in splines])
    doomed = sorted((si for si, pid in taken.items() if pid in {c for _t, c, _s in stale}), reverse=True)
    for si in doomed:
        curve_obj.data.splines.remove(curve_obj.data.splines[si])
    out = []
    for top_uid, _copy_uid, seam in stale:
        top = raw[top_uid]
        _add_bezier(curve_obj, np.asarray(top["co"]) + shift, np.asarray(top["hl"]) + shift,
                    np.asarray(top["hr"]) + shift, False)
        out.append((seam, len(curve_obj.data.splines) - 1))
    return out


def _rebind_quilt_copies(curve_obj, record, recreated):
    """写し直した線を記録の内部線と縫い目に結び直す(`_adopt` のあと)。

    キルティングの縫い目は常に線の端から端までなので、両側の区間を今の線の端で書き直す
    (線の端に点を足すと、記録の区間は元の終点で止まったままになり、足した分が縫われなかった)。
    """
    if recreated:
        splines = read_splines(curve_obj)
        by_uids = {tuple(ln["uids"]): ln for ln in record.get("lines", [])}
        for seam, si in recreated:
            entry = by_uids.get(tuple(splines[si]["uids"]))
            if entry is None:
                continue
            entry["quilt_copy"] = True
            seam["quilt"] = [int(seam["quilt"][0]), int(entry["line_uid"])]
    lines = {ln["line_uid"]: ln for ln in record.get("lines", [])}
    for top_uid, copy_uid, seam in _quilt_pairs(record):
        if top_uid in lines and copy_uid in lines:
            seam["a"] = _whole_line(lines[top_uid]["uids"])
            seam["b"] = _whole_line(lines[copy_uid]["uids"])


def quilt_through(context, curve_obj):
    """表のピースの内部線を、裏のピースの同じ位置へ写し、表と裏の線どうしを縫い止める(キルティング)。

    表と裏は型紙の中心をそろえて向かい合わせに重ねる(Stack Pieces と同じ)ので、裏の型紙の
    同じ相対位置に線を置けば、重ねたときにちょうど重なる。裏にすでに同じ形の線があれば写さずに使う。
    縫い目は折り返し(縫い目をまたぐ曲げ制約を掛けない)。縫ったら布を作り直して重ねて置く。
    戻り値: (布 または None, 警告, 縫った本数)
    """
    record = _check_bag_editable(context, curve_obj)
    panels = bag_panels(record)
    if len(panels) != 2:
        raise CurvePatternError(f"キルティングは表と裏の 2 枚の袋に付けます(今は {len(panels)} 枚)")
    top_lines = lines_of(record, panels[0])
    if not top_lines:
        raise CurvePatternError("表のピースに内部線がありません(表の輪郭の内側に、開いた線を描いてください)")
    if structure_problems(curve_obj, record):
        raise CurvePatternError("点の構成が変わっています。先に Rebuild してください")
    outlines = current_outlines(curve_obj, record)
    shift = _panel_shift(outlines, panels)
    tol = 0.25 * record["target_edge_length"]

    quilted = {tuple(s["quilt"])[0] for s in record["seams"] if s.get("quilt")}
    used_bottom = {tuple(s["quilt"])[1] for s in record["seams"] if s.get("quilt")}
    pending = []                       # (表の線の uid, ("line", 裏の線の uid) または ("spline", 足した spline の番号))
    for ln in top_lines:
        if ln["line_uid"] in quilted:
            continue
        cur = outlines.get(ln["line_uid"])
        if cur is None:
            continue
        target = np.asarray(cur["co"], dtype=np.float64) + shift
        match = None
        for b in lines_of(record, panels[1]):
            other = outlines.get(b["line_uid"])
            if (b["line_uid"] in used_bottom or other is None or len(other["co"]) != len(target)):
                continue
            co = np.asarray(other["co"], dtype=np.float64)
            if np.abs(co - target).max() < tol or np.abs(co[::-1] - target).max() < tol:
                match = b["line_uid"]
                break
        if match is not None:
            used_bottom.add(match)
            pending.append((ln["line_uid"], ("line", match)))
            continue
        _add_bezier(curve_obj, target, np.asarray(cur["hl"]) + shift, np.asarray(cur["hr"]) + shift, False)
        pending.append((ln["line_uid"], ("spline", len(curve_obj.data.splines) - 1)))
    if not pending:
        raise CurvePatternError("縫い止めていない内部線がありません")
    rough = _quilt_resolution_warning(record, outlines, panels[0], top_lines)

    warnings = _adopt(curve_obj, record)
    splines = read_splines(curve_obj)
    by_uids = {tuple(ln["uids"]): ln for ln in record["lines"]}
    uid = _all_seam_uids()
    count = 0
    lines = {ln["line_uid"]: ln for ln in record["lines"]}
    for top_uid, (kind, target) in pending:
        if kind == "spline":
            entry = by_uids.get(tuple(splines[target]["uids"]))
            if entry is None:
                raise CurvePatternError("写した内部線を取り込めませんでした(裏のピースの内側に収まりません)")
            entry["quilt_copy"] = True
            bottom_uid = entry["line_uid"]
        else:
            bottom_uid = target
        count += 1
        record["seams"].append({
            "uid": uid, "name": f"Quilt {sum(1 for s in record['seams'] if s.get('quilt')) + 1}",
            "invert": False, "enabled": True,
            "a": _whole_line(lines[top_uid]["uids"]), "b": _whole_line(lines[bottom_uid]["uids"]),
            "folded": True, "quilt": [int(top_uid), int(bottom_uid)],
        })
        uid += 1
    save_record(curve_obj, record)
    warnings = warnings + rough
    if cloth_of(curve_obj) is None:
        return None, warnings, count
    cloth, more = stack_pieces(context, curve_obj)
    return cloth, warnings + more, count


# キルティングの部屋の幅に、辺がこの本数より少ないと膨らみにくい(30cm 角・線の間隔 10cm で、
# 辺 2cm(5 本)は厚み 19mm、辺 1cm(10 本)は 48mm。圧力 100)
QUILT_EDGES_PER_CHANNEL = 8


def _quilt_resolution_warning(record, outlines, top_uid, top_lines):
    """内部線どうし・内部線と輪郭の間隔(部屋の幅)に対して、目標辺長が粗すぎれば警告を返す。"""
    from . import delaunay2d
    h = float(record["target_edge_length"])
    samples = {}
    for ln in top_lines:
        cur = outlines.get(ln["line_uid"])
        if cur is not None:
            segs = curve_eval.segments(cur["co"], cur["hl"], cur["hr"], False)
            samples[ln["line_uid"]] = np.vstack([seg.point_at(np.linspace(0, 1, 9)) for seg in segs])
    top = outlines[top_uid]
    t_segs = curve_eval.segments(top["co"], top["hl"], top["hr"], True)
    ring = np.vstack([seg.point_at(np.linspace(0, 1, 9)[:-1]) for seg in t_segs])
    width = np.inf
    for uid, pts in samples.items():
        # 線の端は輪郭の近くまで届くのが普通なので、輪郭との間隔は線の中ほどで測る
        middle = pts[len(pts) // 2:len(pts) // 2 + 1]
        width = min(width, float(delaunay2d.distance_to_rings(middle, [ring]).min()))
        for other, opts in samples.items():
            if other != uid:
                width = min(width, float(delaunay2d.distance_to_rings(pts, [opts], closed=False).min()))
    if not np.isfinite(width) or width >= QUILT_EDGES_PER_CHANNEL * h:
        return []
    return [f"キルティングの部屋の幅 {width * 100:.1f}cm に対して辺が粗く({h * 1000:.0f}mm)、"
            f"膨らみにくくなります。Rebuild の辺の長さを {width / QUILT_EDGES_PER_CHANNEL * 1000:.0f}mm "
            "以下にすると筋がはっきりします"]


def remove_quilting(context, curve_obj):
    """キルティングの縫い目と、Quilt Through が裏へ写した内部線を取り除く。戻り値: (布 または None, 警告)"""
    record = _check_bag_editable(context, curve_obj)
    if not any(s.get("quilt") for s in record["seams"]):
        raise CurvePatternError("キルティングの縫い目がありません")
    copies = {ln["line_uid"] for ln in record.get("lines", []) if ln.get("quilt_copy")}
    splines = read_splines(curve_obj)
    taken, _new, _gone = curve_ids.match_pieces(_recorded_shapes(record), [s["uids"] for s in splines])
    for si in sorted((si for si, pid in taken.items() if pid in copies), reverse=True):
        curve_obj.data.splines.remove(curve_obj.data.splines[si])
    record["seams"] = [s for s in record["seams"] if not s.get("quilt")]
    warnings = _adopt(curve_obj, record)
    save_record(curve_obj, record)
    if cloth_of(curve_obj) is None:
        return None, warnings
    cloth, more = stack_pieces(context, curve_obj)
    return cloth, warnings + more


def _arrange(groups, data, base_z, collider_points, axis_xy, margin, angles):
    pieces = [{"xy": data["positions"][idx], "z": base_z + data["positions"][idx][:, 1]}
              for idx in groups]
    try:
        return curve_arrange.arrange_pieces(pieces, collider_points, axis_xy, margin, angles)
    except curve_arrange.ArrangeError as exc:
        raise CurvePatternError(str(exc)) from exc


# ---------------------------------------------------------------- オペレータ

class MUSLIN_OT_curve_pattern_init(bpy.types.Operator):
    """選択した Curve を Curve Pattern にする(全ての点に目印を振る)"""

    bl_idname = "muslin.curve_pattern_init"
    bl_label = "Initialize Curve Pattern"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj is None or obj.type != 'CURVE':
            return False
        return context.mode == 'OBJECT'

    def execute(self, context):
        obj = context.active_object
        try:
            record = initialize(obj, context.scene.muslin_tools.pattern_resolution)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, f"Curve Pattern にしました(ピース {len(record['pieces'])})")
        return {'FINISHED'}


def _target_curve(context):
    """アクティブが Curve ならそれ、Curve Pattern から生成した布ならその Curve。"""
    obj = context.active_object
    if obj is None:
        return None
    if obj.type == 'CURVE':
        return obj
    return curve_of(obj)


class MUSLIN_OT_curve_pattern_rebuild(bpy.types.Operator):
    """Curve から布のメッシュを作り直す(姿勢は引き継げる。停止中のみ)"""

    bl_idname = "muslin.curve_pattern_rebuild"
    bl_label = "Rebuild Cloth from Curve"
    bl_options = {'REGISTER', 'UNDO'}

    edge_length: bpy.props.FloatProperty(
        name="Target Edge Length", unit='LENGTH', min=0.001, default=0.02,
        description="メッシュの辺の目標の長さ。変えると頂点数が変わる",
    )
    keep_pose: bpy.props.BoolProperty(
        name="Keep Pose", default=True,
        description="着せた姿勢を新しい頂点へ引き継ぐ(型紙空間を介して補間する)。"
                    "切ると平らな型紙から作り直す",
    )
    # --- 布を初めて作るときだけ効く(すでに布があれば位置も向きも変えない) ---
    orientation: bpy.props.EnumProperty(
        name="Orientation",
        items=[
            ('FLAT', "Flat", "描いた平面のまま出す(体の周りへは Arrange Around Collider で立てて置く)"),
            ('STANDING', "Standing", "XZ 平面に立てて出す(Add Pattern Piece と同じ向き)"),
        ],
        default='FLAT',
    )
    side: bpy.props.EnumProperty(
        name="Place",
        description="Curve の輪郭のどちら側に出すか",
        items=[('+X', "+X", ""), ('-X', "-X", ""), ('+Y', "+Y", ""), ('-Y', "-Y", "")],
        default='+X',
    )
    gap: bpy.props.FloatProperty(
        name="Gap", unit='LENGTH', min=0.0, default=DEFAULT_GAP,
        description="Curve の輪郭と布の間に空ける距離",
    )
    # 布を初めて作る実行か(F9 で調整するときに、置き方の欄を出すかどうかに使う)
    creating: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        from . import sim_state
        curve = _target_curve(context)
        if curve is None:
            return ui_poll.reject(cls, "Curve、または Curve Pattern から作った布を選んでください")
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        if load_record(curve) is None:
            return ui_poll.reject(cls, "先に Initialize Curve Pattern を実行してください")
        cloth = cloth_of(curve)
        if cloth is not None and sim_state.is_busy(cloth):
            return ui_poll.reject(cls, "シミュレーション中は Rebuild できません。停止してください")
        return True

    def invoke(self, context, event):
        curve = _target_curve(context)
        record = load_record(curve)
        self.edge_length = record["target_edge_length"]
        self.creating = cloth_of(curve) is None
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "edge_length")
        if self.creating:
            # 初めて作るときは、向きと置き場所を選べる(あとから F9 でも変えられる)
            layout.prop(self, "orientation", expand=True)
            row = layout.row(align=True)
            row.prop(self, "side", expand=True)
            layout.prop(self, "gap")
        else:
            layout.prop(self, "keep_pose")
            layout.label(text="布で直接付けたゴム紐は失われます(Curve のゴム紐・姿勢・ピンは保たれる)",
                         icon='INFO')

    def execute(self, context):
        curve = _target_curve(context)
        try:
            cloth, warnings = rebuild(context, curve, self.edge_length, keep_pose=self.keep_pose,
                                      orientation=self.orientation, side=self.side, gap=self.gap)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"'{cloth.name}' を作りました({len(cloth.data.vertices)} 頂点)")
        return {'FINISHED'}


class MUSLIN_OT_curve_arrange(bpy.types.Operator):
    """ピースを体(コライダー)の周りに巻き付けて置く(アレンジメント)"""

    bl_idname = "muslin.curve_arrange"
    bl_label = "Arrange Around Collider"
    bl_options = {'REGISTER', 'UNDO'}

    margin: bpy.props.FloatProperty(
        name="Margin", unit='LENGTH', min=0.0, default=0.0,
        description="体の外側に空ける距離。0 なら厚みから自動で決める",
    )
    angles: bpy.props.StringProperty(
        name="Angles", default="",
        description="ピースごとの向き(度。0 = 正面 = -Y)をカンマで区切る。空なら周りに等間隔",
    )
    height: bpy.props.FloatProperty(
        name="Height", unit='LENGTH',
        description="型紙の下端(y = 0)を置く高さ(ワールド Z)。型紙の y がそのまま高さになる",
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        from . import sim_state
        curve = _target_curve(context)
        if curve is None:
            return ui_poll.reject(cls, "Curve、または Curve Pattern から作った布を選んでください")
        cloth = cloth_of(curve)
        if cloth is None:
            return ui_poll.reject(cls, "布がまだありません(Rebuild で作ってください)")
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        if cloth.muslin.collider_object is None:
            return ui_poll.reject(cls, "布の Collision で体(コライダー)を指定してください")
        if sim_state.is_busy(cloth):
            return ui_poll.reject(cls, "シミュレーション中は置き直せません。停止してください")
        return True

    def execute(self, context):
        curve = _target_curve(context)
        cloth = cloth_of(curve)
        try:
            angles = [float(a) for a in self.angles.split(",")] if self.angles.strip() else None
        except ValueError:
            self.report({'ERROR'}, "Angles は数をカンマで区切ってください(例: 0,180)")
            return {'CANCELLED'}
        # Height はボタンから押したとき invoke で布の原点の高さに合わせる。
        # スクリプトから指定しなければ arrange_around の既定(布の原点の高さ)
        base_z = self.height if self.properties.is_property_set("height") else None
        try:
            warnings = arrange_around(curve, cloth.muslin.collider_object,
                                      margin=self.margin or None, angles=angles, base_z=base_z)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"'{cloth.name}' のピースを体の周りに置きました")
        return {'FINISHED'}

    def invoke(self, context, event):
        # 既定の高さ(布の原点)を欄に出しておき、F9 で調整できるようにする
        cloth = cloth_of(_target_curve(context))
        if not self.properties.is_property_set("height"):
            self.height = float(cloth.matrix_world.translation.z)
        return self.execute(context)


class MUSLIN_OT_curve_stack(bpy.types.Operator):
    """ピースを向かい合わせに重ねて置く(クッションなど、体に着せない袋のため)"""

    bl_idname = "muslin.curve_stack"
    bl_label = "Stack Pieces for Bag"
    bl_options = {'REGISTER', 'UNDO'}

    gap: bpy.props.FloatProperty(
        name="Gap", unit='LENGTH', min=0.0, default=DEFAULT_BAG_GAP,
        description="重ねる 2 枚を離す距離。縫い目が閉じるときに潰れるので、少し広めで構わない",
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        from . import sim_state
        curve = _target_curve(context)
        if curve is None:
            return ui_poll.reject(cls, "Curve、または Curve Pattern から作った布を選んでください")
        if context.mode != 'OBJECT':
            return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
        record = load_record(curve)
        if len(bag_panels(record)) < 2:
            return ui_poll.reject(cls, "袋にするには輪郭が 2 つ以上必要です")
        cloth = cloth_of(curve)
        if cloth is not None and sim_state.is_busy(cloth):
            return ui_poll.reject(cls, "シミュレーション中は置き直せません。停止してください")
        return True

    def execute(self, context):
        curve = _target_curve(context)
        try:
            cloth, warnings = stack_pieces(context, curve, gap=self.gap)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'},
                    f"'{cloth.name}' のピースを向かい合わせに重ねました"
                    "(外周を縫って Close Bag で閉じてください)")
        return {'FINISHED'}


def _gusset_poll(cls, context, want):
    from . import ui_poll
    from . import sim_state
    curve = _target_curve(context)
    if curve is None:
        return ui_poll.reject(cls, "Curve、または Curve Pattern から作った布を選んでください")
    if context.mode != 'OBJECT':
        return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
    record = load_record(curve)
    if record is None:
        return ui_poll.reject(cls, "Curve Pattern として初期化されていません")
    has = bool(gussets_of(record))
    if want and has:
        return ui_poll.reject(cls, "すでにマチがあります")
    if not want and not has:
        return ui_poll.reject(cls, "マチがありません")
    if want and len(bag_panels(record)) != 2:
        return ui_poll.reject(cls, "マチは表と裏の 2 枚の袋に付けます")
    cloth = cloth_of(curve)
    if cloth is not None and sim_state.is_busy(cloth):
        return ui_poll.reject(cls, "シミュレーション中は変えられません。停止してください")
    return True


class MUSLIN_OT_curve_gusset_add(bpy.types.Operator):
    """表と裏のあいだにマチ(帯)を足し、箱形に組んだ袋にする(座布団・箱形のクッション)"""

    bl_idname = "muslin.curve_gusset_add"
    bl_label = "Add Gusset"
    bl_options = {'REGISTER', 'UNDO'}

    height: bpy.props.FloatProperty(
        name="Height", unit='LENGTH', min=0.005, soft_max=0.5, default=DEFAULT_GUSSET_HEIGHT,
        description="マチの幅(= クッションの側面の高さ)。帯の長さは表の外周に合わせる",
    )

    @classmethod
    def poll(cls, context):
        return _gusset_poll(cls, context, True)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        curve = _target_curve(context)
        try:
            cloth, warnings = add_gusset(context, curve, self.height)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"'{cloth.name}' にマチを足して箱形に組みました(Close Bag で膨らませてください)")
        return {'FINISHED'}


class MUSLIN_OT_curve_gusset_remove(bpy.types.Operator):
    """マチの帯と、その縫い目を取り除き、2 枚を平らに重ねた袋に戻す"""

    bl_idname = "muslin.curve_gusset_remove"
    bl_label = "Remove Gusset"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _gusset_poll(cls, context, False)

    def execute(self, context):
        curve = _target_curve(context)
        try:
            _cloth, warnings = remove_gusset(context, curve)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, "マチを外しました")
        return {'FINISHED'}


def _quilt_poll(cls, context, want):
    from . import ui_poll
    from . import sim_state
    curve = _target_curve(context)
    if curve is None:
        return ui_poll.reject(cls, "Curve、または Curve Pattern から作った布を選んでください")
    if context.mode != 'OBJECT':
        return ui_poll.reject(cls, "オブジェクトモードで実行してください (Tab)")
    record = load_record(curve)
    if record is None:
        return ui_poll.reject(cls, "Curve Pattern として初期化されていません")
    panels = bag_panels(record)
    if len(panels) != 2:
        return ui_poll.reject(cls, "キルティングは表と裏の 2 枚の袋に付けます")
    if want and not lines_of(record, panels[0]):
        return ui_poll.reject(cls, "表のピースに内部線がありません")
    if not want and not any(s.get("quilt") for s in record["seams"]):
        return ui_poll.reject(cls, "キルティングの縫い目がありません")
    cloth = cloth_of(curve)
    if cloth is not None and sim_state.is_busy(cloth):
        return ui_poll.reject(cls, "シミュレーション中は変えられません。停止してください")
    return True


class MUSLIN_OT_curve_quilt(bpy.types.Operator):
    """表のピースの内部線を裏の同じ位置へ写し、表と裏を線に沿って縫い止める(キルティング)"""

    bl_idname = "muslin.curve_quilt"
    bl_label = "Quilt Through"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _quilt_poll(cls, context, True)

    def execute(self, context):
        curve = _target_curve(context)
        try:
            _cloth, warnings, count = quilt_through(context, curve)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"内部線 {count} 本で表と裏を縫い止めました(Close Bag で膨らませてください)")
        return {'FINISHED'}


class MUSLIN_OT_curve_quilt_remove(bpy.types.Operator):
    """キルティングの縫い目と、裏へ写した内部線を取り除く"""

    bl_idname = "muslin.curve_quilt_remove"
    bl_label = "Remove Quilting"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _quilt_poll(cls, context, False)

    def execute(self, context):
        curve = _target_curve(context)
        try:
            _cloth, warnings = remove_quilting(context, curve)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, "キルティングを外しました")
        return {'FINISHED'}


class MUSLIN_OT_curve_seam_add(bpy.types.Operator):
    """Curve の点を 2 か所の連なりとして選び、その間を縫い合わせる縫い目にする"""

    bl_idname = "muslin.curve_seam_add"
    bl_label = "Add Seam from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        obj = context.active_object
        if obj is None or obj.type != 'CURVE':
            return ui_poll.reject(cls, "Curve を選んでください")
        if load_record(obj) is None:
            return ui_poll.reject(cls, "先に Initialize Curve Pattern を実行してください")
        return True

    def execute(self, context):
        obj = context.active_object
        try:
            uid = seam_from_selection(obj)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        name = next(s["name"] for s in load_record(obj)["seams"] if s["uid"] == uid)
        self.report({'INFO'}, f"縫い目 '{name}' を追加しました")
        return {'FINISHED'}


class MUSLIN_OT_curve_seam_remove(bpy.types.Operator):
    """Curve の縫い目を削除する"""

    bl_idname = "muslin.curve_seam_remove"
    bl_label = "Remove Curve Seam"
    bl_options = {'REGISTER', 'UNDO'}

    uid: bpy.props.IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        obj = _target_curve(context)
        return obj is not None and load_record(obj) is not None

    def execute(self, context):
        try:
            remove_seam(_target_curve(context), self.uid)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class MUSLIN_OT_curve_elastic_add(bpy.types.Operator):
    """Curve の点の連なり(何か所でも)を選び、そこにゴム紐を入れる(Rebuild しても保たれる)"""

    bl_idname = "muslin.curve_elastic_add"
    bl_label = "Add Elastic from Selection"
    bl_options = {'REGISTER', 'UNDO'}

    scale: bpy.props.FloatProperty(
        name="Length", default=0.8, min=0.2, max=1.5, subtype='FACTOR',
        description="辺の長さの倍率。0.8 なら 2 割縮もうとして、まわりの布を寄せる",
    )

    @classmethod
    def poll(cls, context):
        from . import ui_poll
        obj = context.active_object
        if obj is None or obj.type != 'CURVE':
            return ui_poll.reject(cls, "Curve を選んでください")
        if load_record(obj) is None:
            return ui_poll.reject(cls, "先に Initialize Curve Pattern を実行してください")
        return True

    def execute(self, context):
        obj = context.active_object
        try:
            uid = elastic_from_selection(obj, scale=self.scale)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        name = next(e["name"] for e in load_record(obj)["elastics"] if e["uid"] == uid)
        self.report({'INFO'}, f"ゴム紐 '{name}' を追加しました(長さ ×{self.scale:.2f})")
        return {'FINISHED'}


class MUSLIN_OT_curve_elastic_remove(bpy.types.Operator):
    """Curve のゴム紐を削除する"""

    bl_idname = "muslin.curve_elastic_remove"
    bl_label = "Remove Curve Elastic"
    bl_options = {'REGISTER', 'UNDO'}

    uid: bpy.props.IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        obj = _target_curve(context)
        return obj is not None and load_record(obj) is not None

    def execute(self, context):
        try:
            remove_elastic(_target_curve(context), self.uid)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


_classes = (MUSLIN_OT_curve_pattern_init, MUSLIN_OT_curve_pattern_rebuild,
            MUSLIN_OT_curve_arrange, MUSLIN_OT_curve_stack,
            MUSLIN_OT_curve_gusset_add, MUSLIN_OT_curve_gusset_remove,
            MUSLIN_OT_curve_quilt, MUSLIN_OT_curve_quilt_remove,
            MUSLIN_OT_curve_seam_add, MUSLIN_OT_curve_seam_remove,
            MUSLIN_OT_curve_elastic_add, MUSLIN_OT_curve_elastic_remove)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
