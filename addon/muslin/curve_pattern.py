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
    recorded = {p["piece_uid"]: p["uids"] for p in record["pieces"]}
    taken, new_splines, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])

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
    for si, s in enumerate(splines):
        if si not in parent:
            if s["bezier"]:
                warnings.append(f"spline {si} は閉じていない(または 3 点未満)ので型紙にしません")
            continue
        if si in taken:
            piece_of[si] = taken[si]
        else:
            piece_of[si] = next_piece
            next_piece += 1
    for si in sorted(piece_of):
        p = parent[si]
        pieces.append({"piece_uid": piece_of[si], "uids": per_spline[si], "cyclic": True,
                       "hole_of": piece_of[p] if p is not None else None})
    record["pieces"] = pieces
    record["next_piece_uid"] = next_piece
    return warnings


def structure_problems(curve_obj, record=None):
    """記録した構造と今の Curve を比べ、Shape Update を止める理由の一覧を返す。空なら追従できる。"""
    record = record or load_record(curve_obj)
    if record is None:
        return ["Curve Pattern として初期化されていません"]
    splines = read_splines(curve_obj)
    recorded = {p["piece_uid"]: p["uids"] for p in record["pieces"]}
    taken, new_splines, gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    reasons = []
    for si in new_splines:
        if splines[si]["bezier"] and splines[si]["cyclic"]:
            reasons.append(f"新しい輪郭(spline {si})があります")
    for pid in gone:
        reasons.append(f"ピース {pid} の輪郭が無くなりました")
    for si, pid in sorted(taken.items()):
        piece = next(p for p in record["pieces"] if p["piece_uid"] == pid)
        d = curve_ids.diff_outline(piece["uids"], splines[si]["uids"], True, splines[si]["cyclic"])
        for why in d["reasons"]:
            reasons.append(f"ピース {pid}: {why}")
    return reasons


def current_outlines(curve_obj, record=None):
    """今の Curve を `curve_update.update` に渡す形にする。{輪郭(ピース)の uid: 輪郭}"""
    record = record or load_record(curve_obj)
    splines = read_splines(curve_obj)
    recorded = {p["piece_uid"]: p["uids"] for p in record["pieces"]}
    taken, _new, _gone = curve_ids.match_pieces(recorded, [s["uids"] for s in splines])
    out = {}
    for si, pid in taken.items():
        s = splines[si]
        out[pid] = {"uids": [u for u in s["uids"]], "co": s["co"], "hl": s["hl"], "hr": s["hr"]}
    return out


def _outlines_for_generation(curve_obj, record):
    splines = read_splines(curve_obj)
    recorded = {p["piece_uid"]: p for p in record["pieces"]}
    taken, _new, _gone = curve_ids.match_pieces(
        {pid: p["uids"] for pid, p in recorded.items()}, [s["uids"] for s in splines])
    outlines = []
    for si, pid in sorted(taken.items(), key=lambda kv: kv[1]):
        s = splines[si]
        outlines.append({"piece_uid": pid, "uids": list(s["uids"]), "co": s["co"], "hl": s["hl"],
                         "hr": s["hr"], "hole_of": recorded[pid]["hole_of"]})
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


def apply_seams(curve_obj):
    """縫い目の定義を、作り直さずに今の布へ書き直す。使えなかった縫い目の名前の一覧を返す。"""
    cloth = cloth_of(curve_obj)
    record = load_record(curve_obj)
    if cloth is None or record is None or generation_record(cloth) is None:
        return []
    return _write_seams(cloth, record, reconstruct(cloth))


def status(curve_obj):
    """パネル用の状態。"""
    record = load_record(curve_obj)
    if record is None:
        return {"initialized": False}
    return {
        "initialized": True,
        "pieces": len(record["pieces"]),
        # 穴ではない輪郭の数(= 袋にできる枚数)
        "panels": sum(1 for p in record["pieces"] if p.get("hole_of") is None),
        "target": record["target_edge_length"],
        "problems": structure_problems(curve_obj, record),
        "cloth": cloth_of(curve_obj),
        "seams": [(s["uid"], s["name"]) for s in record["seams"]],
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
                   "uids": r["uids"], "start": int(r["vertices"][0]), "count": len(r["vertices"])}
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
        rings.append({"piece_uid": r["piece_uid"], "outline_uid": r["outline_uid"], "hole": r["hole"],
                      "uids": list(r["uids"]), "vertices": vertices, "s": seg_index + u[vertices],
                      "segment_total": len(r["uids"])})
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
    if cloth is not None and sim_state.is_running(cloth):
        raise CurvePatternError("シミュレーション中は Rebuild できません。停止してから実行してください")

    warnings = []
    carry = None
    if cloth is not None and keep_pose and generation_record(cloth) is not None:
        # 点の uid を振り直す前(足された点がまだ未知のうち)に、旧メッシュの位置を今の Curve に置き直す
        carry, why = _prepare_carry(cloth, curve_obj, record)
        if why:
            warnings.append(why)

    warnings += _adopt(curve_obj, record)
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
        if len(cloth.muslin_elastics):
            cloth.muslin_elastics.clear()
            warnings.append("ゴム紐は作り直しで失われました。付け直してください")

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
    broken = _write_seams(cloth, record, mesh_data)
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
    if sim_state.is_running(cloth):
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
    if cloth is not None and sim_state.is_running(cloth):
        raise CurvePatternError("シミュレーション中は置き直せません。停止してください")

    # 穴ではない輪郭の数 = 袋を作る枚数
    panels = [p["piece_uid"] for p in record["pieces"] if p.get("hole_of") is None]
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
    panels = [p["piece_uid"] for p in record["pieces"] if p.get("hole_of") is None]

    local = to_local(data["positions"], orientation)
    groups = [np.nonzero(data["piece"] == pid)[0] for pid in panels]
    # 型紙の中心をそろえて重ねる(ピースごとに中心が違っても向かい合うように)
    centers = [local[idx].mean(axis=0) for idx in groups]
    base = centers[0]
    for idx, center, shift in zip(groups, centers, stack_offsets(len(groups), gap)):
        local[idx] += base - center
        local[idx, normal_axis] += shift
    return local


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
        if cloth is not None and sim_state.is_running(cloth):
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
            layout.label(text="ゴム紐は失われます(姿勢とピン留めは引き継げます)", icon='ERROR')

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
        if sim_state.is_running(cloth):
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
        panels = [p for p in (record or {}).get("pieces", []) if p.get("hole_of") is None]
        if len(panels) < 2:
            return ui_poll.reject(cls, "袋にするには輪郭が 2 つ以上必要です")
        cloth = cloth_of(curve)
        if cloth is not None and sim_state.is_running(cloth):
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


_classes = (MUSLIN_OT_curve_pattern_init, MUSLIN_OT_curve_pattern_rebuild,
            MUSLIN_OT_curve_arrange, MUSLIN_OT_curve_stack,
            MUSLIN_OT_curve_seam_add, MUSLIN_OT_curve_seam_remove)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
