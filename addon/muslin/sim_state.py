"""実行中のクロスシミュレーション状態管理とフレーム再生ハンドラー。

再生モデル:
- `start_frame` を基準に、フレーム番号 = 経過ステップ数として決定的に計算する。
- フレームが飛んだ場合(スクラブ)はキャッシュから復元するか、
  必要なら開始フレームから再計算して整合性を保つ。
- これにより「早送りすると結果が変わる」という PBD 系でありがちな破綻を防ぐ。
"""

import bpy
from bpy.app.handlers import persistent

from . import mesh_io
from . import pattern_link
from . import rest_shape

# オブジェクトキー(session_uid) -> state dict。
# 名前をキーにするとリネーム・複製で状態が迷子になるため、
# データブロック固有の session_uid を使う(セッション内で一意・リネームで不変)。
_running = {}

# 1回のスクラブで再計算を許容する最大フレーム数(これを超えたら諦めて現状維持)
MAX_RESIMULATE_FRAMES = 600

# メモリキャッシュに保持する最大フレーム数。
# 1.5万頂点なら 1フレーム約 350KB なので、500フレームで約 175MB。
# これを超えたら古いフレームから捨てる(長いシーンではディスクベイクを使うこと)。
MAX_CACHED_FRAMES = 500


def obj_key(obj):
    """オブジェクトを一意に指すキー。session_uid が無い環境では名前にフォールバックする。"""
    uid = getattr(obj, "session_uid", 0)
    return uid if uid else obj.name


def _find_object(key, hint_name=""):
    """キーからオブジェクトを引く。直近の名前をヒントに使って全走査を避ける。"""
    if hint_name:
        obj = bpy.data.objects.get(hint_name)
        if obj is not None and obj_key(obj) == key:
            return obj
    for obj in bpy.data.objects:
        if obj_key(obj) == key:
            return obj
    return None


def is_running(obj):
    return obj is not None and obj_key(obj) in _running


def get_state(obj):
    if obj is None:
        return None
    return _running.get(obj_key(obj))


def create_state(obj, props):
    """シミュレーション状態を作る(ハンドラには登録しない)。

    ベイクのように「ハンドラを経由せず自分でフレームを進めたい」処理から使う。
    obj に重ね着のグループ(`sim_group`)が付いていれば、同じグループの布を
    まとめて1つの状態にする。props は互換のために受け取るが、設定は各布から取る。
    """
    members = mesh_io.group_members(obj)
    for m in members:
        # 元の形をどう扱うかを決めてから組み立てる。
        # (Muslin が書いた形なら戻す / 人が作った形ならそれを元の形にする)
        rest_shape.sync_before_start(m)
        # 型紙オブジェクトがあればそれが型紙。無ければ、型紙を作っている段階なら
        # 今の形を型紙として取り直し、着せた段階なら固定する
        if not pattern_link.sync_before_start(m):
            rest_shape.sync_pattern_before_start(m)

    sim, info = mesh_io.build_group_sim(members)
    reference = info.pop("reference")
    seam_pairs = info.pop("seam_pairs")
    rest = sim.get_positions()
    start_frame = bpy.context.scene.frame_current

    state = {
        "sim": sim,
        "start_frame": start_frame,
        "rest_positions": rest,
        "current_frame": start_frame,
        "cache": {start_frame: rest},
        "info": info,
        "last_error": 0.0,
        "last_contacts": 0,
        "name": members[0].name,
        # (オブジェクトのキー, 最初の頂点番号, 頂点数)。1着でも1件ある
        "members": [(obj_key(m), s, c) for m, (_n, s, c) in zip(members, info["members"])],
        "member_names": [m.name for m in members],
        "material": _group_material_signature(members),
        "seam_count": sum(_enabled_seam_count(m) for m in members),
        "elastic": tuple(mesh_io.elastic_signature(m) for m in members),
        "reference": reference,
        "seam_pairs": seam_pairs,
        "pattern_warnings": [],
    }
    # 組み立てに使った型紙オブジェクトの指紋を覚える(以後の変化だけを流すため)
    state["pattern_signatures"] = {
        key: sig for (key, _s, _c), sig in
        ((entry, pattern_link.signature(m)) for entry, m in zip(state["members"], members))
        if sig is not None and not isinstance(sig, str)
    }
    return state


def member_objects(state):
    """状態に含まれる布のオブジェクト(先頭が設定を使う布)。消えた布があれば空。"""
    objs = []
    for (key, _start, _count), name in zip(state["members"], state["member_names"]):
        o = _find_object(key, name)
        if o is None:
            return []
        objs.append(o)
    state["member_names"] = [o.name for o in objs]   # リネームに追従する
    return objs


def write_state_positions(state, positions, mark=True):
    """まとめて解いた座標を、布ごとに切り分けてメッシュへ書く。"""
    for obj, (_key, start, count) in zip(member_objects(state), state["members"]):
        mesh_io.write_positions_to_mesh(obj, positions[start * 3:(start + count) * 3])
        if mark:
            # 今のメッシュは Muslin の出力であって人が作った形ではない、
            # という印。次に開始するときの扱いが変わる。
            rest_shape.mark_deformed(obj)


def _enabled_seam_count(obj):
    """有効になっている縫い目の本数(info["seams"] は頂点ペア数なので別物)。"""
    return sum(1 for s in getattr(obj, "muslin_seams", []) if s.enabled)


def _material_signature(props):
    """組み立て時に焼き込まれる生地の値。変化の検出に使う。"""
    return (props.density, props.stretch_compliance, props.bending_compliance,
            props.pin_vertex_group)


def _group_material_signature(members):
    return tuple(_material_signature(m.muslin) for m in members)


def _sync_material(state, props, obj=None):
    """生地とピン留めが変わっていたら、姿勢を保ったままコアに流し込む。

    density / compliance / ピン留めは ClothSim の組み立て時に焼き込まれる
    ので、以前は走らせたまま生地を選び直しても何も起きなかった。組み立て
    直すと布が初期姿勢に戻って見比べられないため、コアの差し替えを使う。
    重ね着のグループでは布ごとの生地をまとめて渡す。

    既に計算したフレームは古い生地の結果なので、キャッシュは捨てる。
    """
    members = member_objects(state) if obj is not None else []
    offsets = [(s, c) for _k, s, c in state["members"]]

    # ゴム紐の倍率も走らせたまま効かせる(set_rest_scales は安い)
    changed = False
    if members:
        elastic = tuple(mesh_io.elastic_signature(m) for m in members)
        if elastic != state.get("elastic"):
            state["info"]["elastic_edges"] = mesh_io.apply_group_elastics(
                state["sim"], members, offsets)
            state["elastic"] = elastic
            changed = True

    signature = _group_material_signature(members) if members else state["material"]
    if signature == state["material"]:
        if changed and state["cache"] is not None:
            state["cache"] = {state["start_frame"]: state["rest_positions"]}
        return changed

    state["info"]["pinned"] = mesh_io.apply_group_materials(state["sim"], members, offsets)
    state["material"] = signature
    if state["cache"] is not None:
        state["cache"] = {state["start_frame"]: state["rest_positions"]}
    return True


def poll_pattern(state, members=None):
    """型紙オブジェクトの編集を、走っている状態へ流す(M8)。流したら True。

    静止長・曲げ・質量はコアの `set_reference` で作り直す。縫い目の弧長の
    対応付けも型紙で決まるので、変わっていれば張り直す。計算済みのフレームは
    古い寸法の結果なのでキャッシュは捨てる。
    """
    if members is None:
        members = member_objects(state)
    if not members:
        return False
    reference, changed, reasons = pattern_link.references(state, members)
    state["pattern_warnings"] = reasons
    if not changed:
        return False
    sim = state["sim"]
    sim.set_reference(list(reference))
    state["reference"] = reference

    offsets = [(s, c) for _k, s, c in state["members"]]
    pairs = mesh_io.group_seam_pairs(members, offsets, reference, sim.get_positions())
    if pairs != state.get("seam_pairs"):
        sim.set_seams(pairs, members[0].muslin.seam_compliance)
        state["seam_pairs"] = pairs
        state["info"]["seams"] = len(pairs)

    if state.get("cache") is not None:
        # 今の姿勢から先は新しい寸法で計算し直す。開始フレームだけは残す
        state["cache"] = {state["start_frame"]: state["rest_positions"]}
    return True


def restart_reasons(obj, props):
    """走行中に変えたが、開始し直さないと効かない設定を挙げる。

    生地とピン留めは `_sync_material` が走らせたまま反映する。一方
    コライダーと縫い目は ClothSim の組み立て時に登録され、差し替えるには
    形状を取り直す必要がある。黙って効かないままだと「設定が壊れている」
    としか見えないので、パネルで知らせる。
    """
    state = get_state(obj)
    if state is None:
        return []

    reasons = []
    info = state["info"]

    # グループは先頭の布の設定で組み立てているので、そちらと比べる
    members = member_objects(state)
    if members:
        props = members[0].muslin
    registered = list(info.get("colliders", []))
    current = [o.name for o in mesh_io.collect_collider_objects(props)
               if o not in members]
    if props.collision_enabled and current != registered:
        # 数だけ出すと入れ替えたときに「1 → 1 個」になって何も伝わらない。
        # 短ければ名前を、多ければ数を出す。
        def describe(names):
            if not names:
                return "なし"
            if len(names) <= 2:
                return " / ".join(names)
            return f"{len(names)} 個"

        reasons.append(
            f"コライダーが変わりました ({describe(registered)} → {describe(current)})"
        )

    # info["seams"] は縫い合わせる頂点ペアの数なので、本数とは別に数える
    seams = sum(_enabled_seam_count(m) for m in (member_objects(state) or [obj]))
    if seams != state["seam_count"]:
        reasons.append(f"縫い目の本数が変わりました ({state['seam_count']} → {seams} 本)")

    return reasons


def invalidate_pinning(obj):
    """ピン留めを次のフレームで読み直させる。

    `_material_signature` が見ているのは頂点グループの**名前**なので、
    同じグループの中身が変わっただけでは差し替えが起きない。編集モードや
    ウェイトペイントで頂点を足し引きしたときがこれにあたる。
    印を消して、次のフレームで必ず読み直させる。
    """
    state = get_state(obj)
    if state is not None:
        state["material"] = None


def start_simulation(obj, props):
    """シミュレーションを開始し、フレーム変更ハンドラの管理下に置く。

    重ね着のグループなら、メンバー全員を止めてからまとめて1つで始め、
    全員のキーで同じ状態を指す(どの布から見ても同じ状態が見える)。
    """
    for m in mesh_io.group_members(obj):
        stop_simulation(m)
    state = create_state(obj, props)
    for key, _start, _count in state["members"]:
        _running[key] = state
    return state["info"]


def _drop_state(state):
    """状態を、それを指す全メンバーのキーから外す。"""
    for key in [k for k, s in _running.items() if s is state]:
        _running.pop(key, None)


def stop_simulation(obj):
    """止める。グループのどれか1着を止めると、グループ全体が止まる。"""
    if obj is None:
        return
    state = _running.get(obj_key(obj))
    if state is not None:
        _drop_state(state)


def stop_all():
    _running.clear()


def clear_cache(obj):
    state = get_state(obj)
    if state is not None:
        state["cache"] = {state["start_frame"]: state["rest_positions"]}


def effective_dt(scene, _props=None):
    """1フレームが表す時間(秒)。

    時間の刻みはシーン全体で共通なので、布ごとではなくツール設定から取る。
    """
    tools = scene.muslin_tools
    if tools.use_scene_fps:
        fps = scene.render.fps / max(scene.render.fps_base, 1e-6)
        return 1.0 / max(fps, 1e-6)
    return tools.dt


def _update_animated_colliders(state, props):
    """動くコライダーの形状を現在のフレームの状態に合わせて更新する。"""
    if not props.collider_animated or not state["info"].get("colliders"):
        return

    depsgraph = bpy.context.evaluated_depsgraph_get()
    for index, name in enumerate(state["info"]["colliders"]):
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        positions, _ = mesh_io.build_collider_mesh(obj, depsgraph, positions_only=True)
        try:
            # その場で動かすとフレーム頭の1回しか標本化されず、速いコライダーが
            # 布を素通りする。終点として渡してサブステップごとに補間させる
            state["sim"].set_collider_target(index, positions)
        except ValueError as exc:
            # トポロジが変わった場合は追従できない(頂点数が変わるモディファイア等)
            print(f"[muslin] コライダー '{name}' を更新できません: {exc}")


def advance_one_frame(state, props, dt, target_frame):
    """1フレーム分だけシミュレーションを進める。"""
    sim = state["sim"]

    # 縫製の進行度: start_frame から seam_close_frames かけて 0 -> 1
    if state["info"].get("seams", 0) > 0:
        elapsed = target_frame - state["start_frame"]
        span = props.seam_close_frames
        closure = 1.0 if span <= 0 else min(max(elapsed / span, 0.0), 1.0)
        sim.set_seam_closure(closure)

    _update_animated_colliders(state, props)

    sim.step(
        dt,
        -abs(props.gravity),
        props.iterations,
        props.substeps,
        props.damping,
        tuple(props.wind),
        props.floor_enabled,
        props.floor_z,
        props.friction,
        props.collision_enabled,
        props.collision_thickness,
        props.collision_friction,
        props.self_collision_enabled,
        props.self_collision_thickness,
        props.post_collision_iterations,
        props.cache_broadphase,
        props.chebyshev_radius,
        props.continuous_self_collision,
    )


def _trim_cache(cache, start_frame):
    """キャッシュが上限を超えたら古いフレームから捨てる。

    開始フレームだけは巻き戻しの基準として必ず残す。
    """
    while len(cache) > MAX_CACHED_FRAMES:
        oldest = min(f for f in cache if f != start_frame)
        del cache[oldest]


def _simulate_to(state, props, dt, target_frame, obj=None):
    """target_frame の状態まで進める。キャッシュがあれば活用する。"""
    # キャッシュを引く前に生地と型紙の変化を反映する(古い結果を返さない)
    _sync_material(state, props, obj)
    if obj is not None:
        poll_pattern(state)

    cache = state["cache"] if props.use_cache else None
    start_frame = state["start_frame"]
    sim = state["sim"]

    if target_frame <= start_frame:
        sim.set_positions(state["rest_positions"])
        state["current_frame"] = start_frame
        return state["rest_positions"]

    if cache is not None and target_frame in cache:
        positions = cache[target_frame]
        sim.set_positions(positions)
        state["current_frame"] = target_frame
        return positions

    # 巻き戻し方向、または連続していないフレームへのジャンプ → 直近の既知状態から再計算
    if target_frame < state["current_frame"]:
        base_frame = start_frame
        base_positions = state["rest_positions"]
        if cache is not None:
            known = [f for f in cache if f <= target_frame]
            if known:
                base_frame = max(known)
                base_positions = cache[base_frame]
        sim.set_positions(base_positions)
        state["current_frame"] = base_frame

    steps = target_frame - state["current_frame"]
    if steps > MAX_RESIMULATE_FRAMES:
        # 大きく飛びすぎた場合は再計算せず、現在の姿勢のまま追従する
        state["current_frame"] = target_frame
        return sim.get_positions()

    for _ in range(steps):
        state["current_frame"] += 1
        advance_one_frame(state, props, dt, state["current_frame"])
        if cache is not None:
            cache[state["current_frame"]] = sim.get_positions()
            _trim_cache(cache, start_frame)

    positions = cache[target_frame] if (cache is not None and target_frame in cache) else sim.get_positions()
    return positions


def _playback_baked(scene):
    """ベイク済みオブジェクトにキャッシュのフレームを適用する。"""
    from . import bake_ops  # 循環 import を避けるため関数内で読み込む

    for obj in scene.objects:
        if obj.type != 'MESH' or not obj.get("muslin_baked", False):
            continue
        if obj_key(obj) in _running:
            continue  # ライブシミュレーション中はそちらを優先する
        if not bake_ops.apply_baked_frame(obj, scene.frame_current):
            # 読めないキャッシュを毎フレーム叩き続けないよう、ベイク状態を解除する
            obj["muslin_baked"] = False


def _restore_idle_cloths(scene):
    """走っていない布を、先頭フレームで元の形に戻す。

    Blender では「フレーム 1 に戻す = 元のメッシュの形に戻る」が当たり前
    なので、シミュレーションを止めていてもそう振る舞わせる。Muslin は
    モディファイアではなくメッシュに直接書くので、明示的に戻さないと
    変形したまま残る。

    走っている布はこの関数では触らない(そちらは _simulate_to が
    start_frame 以下で元の形に戻す)。
    """
    if scene.frame_current > scene.frame_start:
        return
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.mode == 'EDIT':
            continue
        if obj_key(obj) in _running:
            continue
        if obj.get("muslin_baked", False):
            continue        # ベイクの再生が形を決めているので触らない
        # 戻すのは Muslin が書いた形だけ。人が編集した形まで戻すと、
        # 型紙を直してから先頭フレームに戻った時点で編集が消える。
        if not rest_shape.is_deformed(obj):
            continue
        rest_shape.restore(obj)


@persistent
def _frame_change_handler(scene, depsgraph=None):
    _playback_baked(scene)
    _restore_idle_cloths(scene)

    if not _running:
        return

    dt = effective_dt(scene)
    frame = scene.frame_current

    # 重ね着のグループはメンバー全員のキーで同じ状態を指すので、1回だけ進める
    seen = set()
    for key, state in list(_running.items()):
        if id(state) in seen:
            continue
        seen.add(id(state))
        members = member_objects(state)
        if not members:
            _drop_state(state)
            continue
        obj = members[0]
        state["name"] = obj.name  # リネームに追従する
        obj_name = obj.name

        # 編集中の布には手を出さない。
        #
        # 編集モードでも obj.data.vertices への書き込み自体は通る(実測で
        # 確認済み)。しかし表示されているのは編集用の BMesh の方で、編集を
        # 抜けるときにそちらが書き戻される。つまり計算した結果は捨てられ、
        # かつシミュレーション側だけがフレームを進めるので、抜けた瞬間に
        # 布が飛ぶ。Blender 標準のクロスも編集中は計算しない。
        # グループはまとめて解くので、1着でも編集中なら全体を止める。
        #
        # 止めている間に進んだフレームは、抜けたあとキャッシュか開始
        # フレームから追いつく(_simulate_to がジャンプを扱う)。
        if any(m.mode == 'EDIT' for m in members):
            state["was_editing"] = True
            continue

        # 編集から戻ってきた直後は、ピン留めの中身が変わっている可能性が
        # ある。グループ名が同じだと差し替えが起きないので、ここで印を消す。
        if state.pop("was_editing", False):
            state["material"] = None
        # 設定は布ごとなので、オブジェクトから取る(グループは先頭の布)
        props = obj.muslin

        try:
            positions = _simulate_to(state, props, dt, frame, obj)
            write_state_positions(state, positions)
            state["last_error"] = state["sim"].average_stretch_error()
            state["last_contacts"] = state["sim"].last_collision_count
            if not state["sim"].is_finite():
                print(f"[muslin] '{obj_name}' のシミュレーションが発散しました。停止します。")
                _drop_state(state)
        except Exception as exc:  # Rust 側の例外もここで受け止めて Blender を落とさない
            print(f"[muslin] '{obj_name}' の更新中にエラー: {exc}")
            _drop_state(state)


@persistent
def _load_post_handler(_dummy=None):
    """別の .blend を読み込んだら、前のファイルのシミュレーション状態を捨てる。

    ハンドラ自体は @persistent で生き残るが、保持している ClothSim は
    既に存在しないオブジェクトのものなので破棄する。
    """
    stop_all()
    from . import overlay
    overlay.invalidate_cache()


def register():
    if _frame_change_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_frame_change_handler)
    if _load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_handler)


def unregister():
    stop_all()
    if _frame_change_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change_handler)
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
