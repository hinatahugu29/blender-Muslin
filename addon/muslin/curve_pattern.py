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

座標の対応: Curve の (x, y) → 布のローカル座標 (x, 0, y)。既存の型紙(Add Pattern Piece)と同じく
XZ 平面に立てる。Curve オブジェクトのスケールは型紙の寸法として掛ける。
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
            "next_uid": 1, "next_piece_uid": 1, "pieces": [], "seams": []}


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

    attr = mesh.attributes.get(seams.SEAM_ATTRIBUTE)
    if attr is None:
        attr = mesh.attributes.new(seams.SEAM_ATTRIBUTE, 'INT', 'EDGE')
    attr.data.foreach_set("value", codes)
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
        "target": record["target_edge_length"],
        "problems": structure_problems(curve_obj, record),
        "cloth": cloth_of(curve_obj),
        "seams": [(s["uid"], s["name"]) for s in record["seams"]],
        "gen_id": record["gen_id"],
    }


# ---------------------------------------------------------------- 派生データ(布のメッシュ)

def _to_local(xy):
    """型紙の座標 (x, y) → 布のローカル座標 (x, 0, y)。"""
    out = np.zeros((len(xy), 3))
    out[:, 0] = xy[:, 0]
    out[:, 2] = xy[:, 1]
    return out


def _build_mesh(name, data, record):
    mesh = bpy.data.meshes.new(name)
    verts = _to_local(data["positions"])
    mesh.from_pydata(verts.tolist(), [], data["triangles"].tolist())
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


def _cloth_placement(curve_obj, data):
    width = float(np.ptp(data["positions"][:, 0])) if len(data["positions"]) else 1.0
    return curve_obj.matrix_world.translation.copy(), max(width, 0.1) * 1.5


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
    if np.abs(pose - _to_local(old["positions"])).max() < 1e-6:
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


def rebuild(context, curve_obj, target_length=None, keep_pose=True):
    """Curve から布のメッシュを(作り直して)生成する。

    keep_pose なら、旧メッシュの姿勢(着せた形)を型紙空間を介して新しい頂点へ引き継ぐ。
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

    record["gen_id"] += 1
    name = f"{curve_obj.name}_Cloth"
    mesh = _build_mesh(name, data, record)
    flat = _to_local(data["positions"]).astype(np.float32).ravel()

    if cloth is None:
        cloth = bpy.data.objects.new(name, mesh)
        collections = list(curve_obj.users_collection) or [context.collection]
        collections[0].objects.link(cloth)
        loc, dx = _cloth_placement(curve_obj, data)
        cloth.location = loc
        cloth.location.x += dx
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


def arrange_around(curve_obj, collider, margin=None, angles=None):
    """布のピースを、コライダー(体)の縦軸のまわりに巻き付けて置く。姿勢(rest)も置いた形にする。

    ピースは向き(度。0 = 正面 = -Y)ごとに円柱の外側へ置かれる。angles を省略すると、
    ピース数で周りに等間隔(1 枚は正面、2 枚は正面と背面)。着せた段階は解除される。
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
    flat_local = _to_local(data["positions"])
    flat_world = flat_local @ mw[:3, :3].T + mw[:3, 3]

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
    arranged, warnings = _arrange(groups, data, flat_world, collider_points, axis_xy, margin, angles)

    world = np.empty_like(flat_world)
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


def _arrange(groups, data, flat_world, collider_points, axis_xy, margin, angles):
    pieces = [{"xy": data["positions"][idx], "z": flat_world[idx][:, 2]} for idx in groups]
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
        record = load_record(_target_curve(context))
        self.edge_length = record["target_edge_length"]
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, "edge_length")
        self.layout.prop(self, "keep_pose")
        self.layout.label(text="ゴム紐は失われます(姿勢とピン留めは引き継げます)", icon='ERROR')

    def execute(self, context):
        curve = _target_curve(context)
        try:
            cloth, warnings = rebuild(context, curve, self.edge_length, keep_pose=self.keep_pose)
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
        try:
            warnings = arrange_around(curve, cloth.muslin.collider_object,
                                      margin=self.margin or None, angles=angles)
        except CurvePatternError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        for w in warnings:
            self.report({'WARNING'}, w)
        self.report({'INFO'}, f"'{cloth.name}' のピースを体の周りに置きました")
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
            MUSLIN_OT_curve_arrange, MUSLIN_OT_curve_seam_add, MUSLIN_OT_curve_seam_remove)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
