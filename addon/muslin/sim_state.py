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
    """
    sim, info = mesh_io.build_cloth_sim(obj, props)
    rest = sim.get_positions()
    start_frame = bpy.context.scene.frame_current

    return {
        "sim": sim,
        "start_frame": start_frame,
        "rest_positions": rest,
        "current_frame": start_frame,
        "cache": {start_frame: rest},
        "info": info,
        "last_error": 0.0,
        "last_contacts": 0,
        "name": obj.name,
    }


def start_simulation(obj, props):
    """シミュレーションを開始し、フレーム変更ハンドラの管理下に置く。"""
    state = create_state(obj, props)
    _running[obj_key(obj)] = state
    return state["info"]


def stop_simulation(obj):
    if obj is not None:
        _running.pop(obj_key(obj), None)


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
            state["sim"].update_collider(index, positions)
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
    )


def _trim_cache(cache, start_frame):
    """キャッシュが上限を超えたら古いフレームから捨てる。

    開始フレームだけは巻き戻しの基準として必ず残す。
    """
    while len(cache) > MAX_CACHED_FRAMES:
        oldest = min(f for f in cache if f != start_frame)
        del cache[oldest]


def _simulate_to(state, props, dt, target_frame):
    """target_frame の状態まで進める。キャッシュがあれば活用する。"""
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


@persistent
def _frame_change_handler(scene, depsgraph=None):
    _playback_baked(scene)

    if not _running:
        return

    dt = effective_dt(scene)
    frame = scene.frame_current

    for key, state in list(_running.items()):
        obj = _find_object(key, state.get("name", ""))
        if obj is None:
            _running.pop(key, None)
            continue
        state["name"] = obj.name  # リネームに追従する
        obj_name = obj.name
        # 設定は布ごとなので、オブジェクトから取る
        props = obj.muslin

        try:
            positions = _simulate_to(state, props, dt, frame)
            mesh_io.write_positions_to_mesh(obj, positions)
            state["last_error"] = state["sim"].average_stretch_error()
            state["last_contacts"] = state["sim"].last_collision_count
            if not state["sim"].is_finite():
                print(f"[muslin] '{obj_name}' のシミュレーションが発散しました。停止します。")
                _running.pop(key, None)
        except Exception as exc:  # Rust 側の例外もここで受け止めて Blender を落とさない
            print(f"[muslin] '{obj_name}' の更新中にエラー: {exc}")
            _running.pop(key, None)


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
