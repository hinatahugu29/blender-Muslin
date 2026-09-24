import bpy


# 生地プリセット。
#
# `density` は実際の目付(g/m^2)を kg/m^2 に直したもので、根拠のある値。
#   シフォン 40-60 / シルク 60-90 / コットンシャツ地 110-150
#   ウール 250-350 / デニム 300-450 / 革 800-1000
# コンプライアンスは物性の実測値ではなく、XPBD 上で「らしく」見える相対値。
# 硬い生地ほど bending を小さく、伸びる生地ほど stretch を大きくしてある。
#
# bending の値はカンチレバー法(片持ちの短冊がどれだけ垂れるか)で較正した。
# 有効なのは 0 〜 0.3 の範囲で、それより大きくしても「完全に柔らかい」で頭打ちになる。
#
# **曲げ剛性は Substeps に強く依存する**。既定の Quality(Normal, Substeps 8)
# では、硬い側の生地どうしがほとんど同じように垂れる。生地の違いを出したい
# なら Quality を High 以上(Substeps 16 以上)にすること。
# (実測は README「パラメータの決め方」と scripts/bench_quality.py を参照)
FABRIC_PRESETS = {
    'CHIFFON': dict(density=0.05, stretch=0.0, bending=0.3, damping=0.02),
    'SILK': dict(density=0.08, stretch=0.0, bending=0.1, damping=0.03),
    'KNIT': dict(density=0.18, stretch=5e-4, bending=5e-2, damping=0.05),
    'COTTON': dict(density=0.15, stretch=0.0, bending=2e-2, damping=0.05),
    'WOOL': dict(density=0.30, stretch=1e-5, bending=5e-3, damping=0.08),
    'DENIM': dict(density=0.40, stretch=0.0, bending=1e-3, damping=0.08),
    'LEATHER': dict(density=0.90, stretch=0.0, bending=0.0, damping=0.12),
}


# 品質の段。数字は scripts/bench_quality.py の実測で決めた。
# 41x41(1681頂点) を 2 隅で吊るした場面、48 フレーム、min-of-5:
#
#   段       it  sub  cheb  ms/frame  伸び誤差  垂れ
#   Draft     5    4  0.00      1.26   0.1600  1.394
#   Normal   10    8  0.98      4.84   0.0142  0.810
#   High     10   16  0.98      9.62   0.0018  0.716
#   Final    15   32  0.98     28.0    0.0002  0.707
#
# 垂れは 0.707 に収束する。Normal で既に形はほぼ出ており、High から先は
# 伸び誤差を詰めるための段。
#
# Substeps を主に振るのは、伸び誤差に対して Iterations より約 3 倍効くため。
# Chebyshev はコスト +3% で垂れの収束が明確に良くなるので、5 を超える
# Iterations では常に入れる(5 以下だと行き過ぎて布が反り返る)。
# cache_broadphase は Substeps 8 以上でだけ得になる。
QUALITY_PRESETS = {
    'DRAFT':  dict(iterations=5,  substeps=4,  chebyshev=0.0,  post=1, cache=False),
    'NORMAL': dict(iterations=10, substeps=8,  chebyshev=0.98, post=2, cache=True),
    'HIGH':   dict(iterations=10, substeps=16, chebyshev=0.98, post=3, cache=True),
    'FINAL':  dict(iterations=15, substeps=32, chebyshev=0.98, post=4, cache=True),
}

# プリセットを適用している最中は、個々の値の update を無視する。これが
# 無いと、プリセットが書き込んだ値そのものが「手で変えた」とみなされて
# 即 Custom に戻ってしまう。
_applying_preset = False


def _apply_preset(props, table, key_prop, mapping):
    """プリセット表の値を、対応するプロパティに流し込む。

    mapping は {表の列名: プロパティ名}。'CUSTOM' のように表に無い
    選択肢のときは何もしない。
    """
    global _applying_preset
    values = table.get(getattr(props, key_prop))
    if values is None:
        return
    _applying_preset = True
    try:
        for column, prop_name in mapping.items():
            setattr(props, prop_name, values[column])
    finally:
        _applying_preset = False


def _fall_back_to_custom(props, key_prop):
    """値が手で変えられたら、プリセットの表示を Custom に落とす。

    プリセット名が出たままなのに中身が違う、という嘘の表示を防ぐ。
    """
    if not _applying_preset and getattr(props, key_prop) != 'CUSTOM':
        setattr(props, key_prop, 'CUSTOM')


QUALITY_MAPPING = {
    "iterations": "iterations",
    "substeps": "substeps",
    "chebyshev": "chebyshev_radius",
    "post": "post_collision_iterations",
    "cache": "cache_broadphase",
}

FABRIC_MAPPING = {
    "density": "density",
    "stretch": "stretch_compliance",
    "bending": "bending_compliance",
    "damping": "damping",
}


def _apply_quality_preset(self, context):
    """品質の段が選ばれたら、解法の各値を流し込む。"""
    _apply_preset(self, QUALITY_PRESETS, "quality", QUALITY_MAPPING)


def _quality_to_custom(self, context):
    _fall_back_to_custom(self, "quality")


def _apply_fabric_preset(self, context):
    """生地が選ばれたら物性値を流し込む。"""
    _apply_preset(self, FABRIC_PRESETS, "fabric_preset", FABRIC_MAPPING)


def _fabric_to_custom(self, context):
    _fall_back_to_custom(self, "fabric_preset")


# 生地の並びと説明。サムネイル付きで出すため、items は関数で作る。
#
# 末尾の数字は .blend に保存される値なので**動かしてはいけない**。
# 動的 items の enum は識別子ではなくこの数字で保存されるので、並べ替えや
# 追加のたびにずれると、開き直したときに別の生地になってしまう。
FABRIC_ITEMS = [
    ('CUSTOM', "Custom", "手動で設定する(選んでも値は変わりません)", 0),
    ('CHIFFON', "Chiffon", "シフォン: 非常に軽く柔らかい (50 g/m^2)", 1),
    ('SILK', "Silk", "シルク: 軽くなめらかに落ちる (80 g/m^2)", 2),
    ('COTTON', "Cotton", "コットン: 標準的なシャツ地 (150 g/m^2)", 3),
    ('KNIT', "Knit", "ニット: 伸びる編み地 (180 g/m^2)", 4),
    ('WOOL', "Wool", "ウール: 厚みがありゆったり落ちる (300 g/m^2)", 5),
    ('DENIM', "Denim", "デニム: 重く硬い (400 g/m^2)", 6),
    ('LEATHER', "Leather", "革: 非常に重く曲がりにくい (900 g/m^2)", 7),
]

# Blender は items コールバックが返した文字列を保持しないので、Python 側で
# 参照を持っておかないと解放されて表示が壊れる(よくある落とし穴)。
_fabric_items_cache = []


def _fabric_items(self, context):
    from . import previews
    _fabric_items_cache[:] = [
        (key, label, description, previews.icon_id(key), number)
        for key, label, description, number in FABRIC_ITEMS
    ]
    return _fabric_items_cache


class MUSLIN_PG_vertex_index(bpy.types.PropertyGroup):
    """シームのチェーンを構成する頂点インデックス(順序を保持する)。"""

    index: bpy.props.IntProperty(name="Vertex Index", default=0, min=0)


class MUSLIN_PG_seam(bpy.types.PropertyGroup):
    """1本の縫い目。2本の頂点チェーンを弧長で対応付けて縫い合わせる。"""

    name: bpy.props.StringProperty(name="Name", default="Seam")
    chain_a: bpy.props.CollectionProperty(type=MUSLIN_PG_vertex_index)
    chain_b: bpy.props.CollectionProperty(type=MUSLIN_PG_vertex_index)
    flipped: bpy.props.BoolProperty(
        name="Flip",
        description="縫い合わせる向きを反転する(縫い目がねじれている場合に切り替える)",
        default=False,
    )
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description="この縫い目を有効にする",
        default=True,
    )
    # 辺の属性 `muslin_seam` での識別子。0 は旧形式(chain_a / chain_b の頂点番号で持つ)
    uid: bpy.props.IntProperty(name="UID", default=0, min=0)
    invert: bpy.props.BoolProperty(
        name="Flip",
        description="縫い合わせる向きを反転する(自動で決めた向きが逆のときに切り替える)",
        default=False,
    )


class MUSLIN_PG_elastic(bpy.types.PropertyGroup):
    """ゴム紐(弾性グループ)。辺の属性 `muslin_elastic` に uid を書いた辺を縮める。"""

    name: bpy.props.StringProperty(name="Name", default="Elastic")
    uid: bpy.props.IntProperty(name="UID", default=0, min=0)
    scale: bpy.props.FloatProperty(
        name="Length",
        description=(
            "辺の長さの倍率。0.8 なら 2 割縮もうとして、まわりの布を寄せる"
            "(ウエストや袖口のゴム)。1 で効かない"
        ),
        default=0.8,
        min=0.2,
        max=1.5,
        subtype='FACTOR',
    )
    enabled: bpy.props.BoolProperty(name="Enabled", default=True)


class MUSLIN_PG_tools(bpy.types.PropertyGroup):
    """シーン単位の設定。ツールと表示に関わるものだけ。"""

    # --- 時間 ---
    use_scene_fps: bpy.props.BoolProperty(
        name="Use Scene FPS",
        description="シーンのフレームレートから自動で1フレームの時間を決める",
        default=True,
    )
    dt: bpy.props.FloatProperty(
        name="Time Step",
        description="1フレームあたりの経過時間(秒)。Use Scene FPS が無効なときに使用",
        default=1.0 / 24.0,
        min=0.0001,
        soft_max=0.1,
        unit='TIME_ABSOLUTE',
    )

    # --- 表示 ---
    show_seams: bpy.props.BoolProperty(
        name="Show Seams",
        description="ビューポートに縫い目を線で表示する",
        default=True,
    )

    # --- 表示 ---

    # --- パターン作成の道具 ---
    pattern_width: bpy.props.FloatProperty(
        name="Width",
        description="作成するパターンピースの幅",
        default=0.5,
        min=0.001,
        soft_max=3.0,
        unit='LENGTH',
    )
    pattern_height: bpy.props.FloatProperty(
        name="Height",
        description="作成するパターンピースの高さ",
        default=0.7,
        min=0.001,
        soft_max=3.0,
        unit='LENGTH',
    )
    pattern_resolution: bpy.props.FloatProperty(
        name="Resolution",
        description="パターンの目標エッジ長。小さいほど細かく、シワが細かくなるが重くなる",
        default=0.03,
        min=0.002,
        soft_max=0.2,
        unit='LENGTH',
    )


class MUSLIN_PG_cloth(bpy.types.PropertyGroup):
    """オブジェクト単位のシミュレーション設定。

    Blender 標準の Cloth モディファイアと同じく、布ごとに持つ。
    こうしないと1つのシーンで生地を作り分けられない。
    """
    # --- ソルバー ---
    quality: bpy.props.EnumProperty(
        name="Quality",
        description=(
            "計算の細かさ。下の各値をまとめて設定する。"
            "段ごとの実測値は scripts/bench_quality.py を参照"
        ),
        items=[
            ('DRAFT', "Draft",
             "形を見るための最速設定。伸びは目に見えて残る (Normal の 0.26倍の時間)"),
            ('NORMAL', "Normal",
             "既定。形はほぼ出る (伸び誤差 1.4%)"),
            ('HIGH', "High",
             "伸びをほぼ取り除く。硬い生地の硬さもここから出る (Normal の 2.0倍)"),
            ('FINAL', "Final",
             "書き出し用。これ以上細かくしても結果は変わらない (Normal の 5.8倍)"),
            ('CUSTOM', "Custom", "下の値を手で設定する"),
        ],
        default='NORMAL',
        update=_apply_quality_preset,
    )
    iterations: bpy.props.IntProperty(
        name="Iterations",
        description="制約解決(XPBD)の反復回数。多いほど硬く安定するが重くなる",
        default=10,
        min=1,
        soft_max=50,
        update=_quality_to_custom,
    )
    substeps: bpy.props.IntProperty(
        name="Substeps",
        description=(
            "1フレームを分割するサブステップ数。"
            "伸び誤差を減らすには Iterations より効率が良い(実測で約3倍)"
        ),
        default=8,
        min=1,
        soft_max=32,
        update=_quality_to_custom,
    )
    chebyshev_radius: bpy.props.FloatProperty(
        name="Convergence Boost",
        description=(
            "制約の収束を加速する(Chebyshev 半復法)。0 で無効。"
            "反復そのものを速めるだけなので材質は変わらない。"
            "Substeps を上げたときに特によく効き、"
            "iterations 10 / substeps 32 では垂れ比 0.70 が 0.41 になる"
            "(コストは +1% 未満)。推奨は 0.98。"
            "Iterations が 5 以下のときは加速されない(短い反復で加速すると"
            "行き過ぎて布が反り返るため)"
        ),
        default=0.98,
        min=0.0,
        max=0.98,
        precision=3,
        update=_quality_to_custom,
    )
    cache_broadphase: bpy.props.BoolProperty(
        name="Cache Collision Search",
        description=(
            "コライダーの探索をフレーム先頭で1回だけ行い、サブステップ間で使い回す。"
            "Substeps が 8 以上のときに効く(実測で 1.1〜1.3倍)。"
            "4 以下ではわずかに遅くなる"
        ),
        default=True,
        update=_quality_to_custom,
    )
    post_collision_iterations: bpy.props.IntProperty(
        name="Post-Collision",
        description=(
            "衝突の押し出しで壊れた伸びを解き直す回数。"
            "厚みを大きく取ったときの布の膨張を抑える(0で無効)"
        ),
        default=2,
        min=0,
        soft_max=16,
        update=_quality_to_custom,
    )

    # --- 外力 ---
    gravity: bpy.props.FloatProperty(
        name="Gravity",
        description="重力加速度(m/s^2、正の値で下方向に落下)",
        default=9.81,
        min=0.0,
        soft_max=20.0,
        unit='ACCELERATION',
    )
    wind: bpy.props.FloatVectorProperty(
        name="Wind",
        description=(
            "一様な風。加速度ではなく面に働く圧力(N/m^2)として扱うので、"
            "同じ風でも Density が高い生地ほどなびきにくい"
        ),
        default=(0.0, 0.0, 0.0),
        size=3,
        subtype='XYZ',
    )
    damping: bpy.props.FloatProperty(
        name="Damping",
        description="速度減衰(0で減衰なし。大きいほど布が早く静止する)",
        default=0.01,
        min=0.0,
        max=1.0,
        update=_fabric_to_custom,
    )

    # --- 生地物性 ---
    fabric_preset: bpy.props.EnumProperty(
        name="Fabric",
        description=(
            "生地のプリセット。選ぶと Density / Stretch / Bending / Damping が"
            "まとめて設定される"
        ),
        items=_fabric_items,
        update=_apply_fabric_preset,
    )
    density: bpy.props.FloatProperty(
        name="Density",
        description="面密度 kg/m^2。頂点質量を面積から算出するのに使う",
        default=0.2,
        min=0.001,
        soft_max=2.0,
        update=_fabric_to_custom,
    )
    stretch_compliance: bpy.props.FloatProperty(
        name="Stretch Compliance",
        description="伸び制約のコンプライアンス(0に近いほど伸びない・硬い)",
        default=0.0,
        min=0.0,
        soft_max=0.01,
        precision=6,
        update=_fabric_to_custom,
    )
    bending_compliance: bpy.props.FloatProperty(
        name="Bending Compliance",
        description=(
            "曲げにくさ(0 で最も硬い。大きいほど柔らかい)。"
            "効くのは 0〜0.3 の範囲。"
            "曲げ剛性は Substeps に強く依存するので、硬い生地は Substeps を上げること"
        ),
        default=0.02,
        min=0.0,
        soft_max=0.5,
        precision=5,
        update=_fabric_to_custom,
    )

    # --- 衝突(暫定: 床のみ。本格的なコリジョンは M2) ---
    floor_enabled: bpy.props.BoolProperty(
        name="Floor Collision",
        description="水平な床面との衝突を有効にする",
        default=False,
    )
    floor_z: bpy.props.FloatProperty(
        name="Floor Z",
        description="床面の高さ(ワールドZ)",
        default=0.0,
        unit='LENGTH',
    )
    friction: bpy.props.FloatProperty(
        name="Friction",
        description=(
            "床面の摩擦(0で滑る、1で止まる)。"
            "Substeps を変えても強さは変わらない"
        ),
        default=0.8,
        min=0.0,
        max=1.0,
    )

    # --- コリジョンオブジェクト(M2) ---
    collision_enabled: bpy.props.BoolProperty(
        name="Object Collision",
        description="指定したオブジェクト/コレクションとの衝突を有効にする",
        default=True,
    )
    collider_object: bpy.props.PointerProperty(
        name="Collider",
        description="衝突させるメッシュオブジェクト(人体アバターなど)",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )
    collider_collection: bpy.props.PointerProperty(
        name="Collider Collection",
        description="複数のコリジョンオブジェクトをまとめて指定するコレクション",
        type=bpy.types.Collection,
    )
    collider_animated: bpy.props.BoolProperty(
        name="Animated Collider",
        description="コライダーが動く場合に有効化(毎フレーム形状を取り込む。その分重い)",
        default=False,
    )
    collision_thickness: bpy.props.FloatProperty(
        name="Thickness",
        description="布の厚み。コライダー表面からこの距離を保つ",
        default=0.005,
        min=0.0,
        soft_max=0.05,
        precision=4,
        unit='LENGTH',
    )
    collision_friction: bpy.props.FloatProperty(
        name="Surface Friction",
        description=(
            "コライダー表面と自己衝突の摩擦(0で滑る、1で貼り付く)。"
            "Substeps を変えても強さは変わらない。"
            "0.7 を下回ると、球に落とした布が留まらず滑り落ちる"
        ),
        default=0.8,
        min=0.0,
        max=1.0,
    )

    # --- 自己衝突(M2) ---
    self_collision_enabled: bpy.props.BoolProperty(
        name="Self Collision",
        description="布同士の貫通を防ぐ(重いので必要なときだけ)",
        default=False,
    )
    continuous_self_collision: bpy.props.BoolProperty(
        name="Catch Fast Motion",
        description=(
            "布どうしの高速な交差を、前後の位置を結ぶ線分で捕まえる。"
            "切ると 厚み x fps x Substeps を超える速度で布が素通りする"
        ),
        default=True,
    )
    # --- 重ね着(M7) ---
    sim_group: bpy.props.StringProperty(
        name="Group",
        description=(
            "同じ名前を付けた布を1つのシミュレーションで一緒に解く(重ね着)。"
            "空なら単独で解く。まとめて解くとき、ソルバーと衝突の設定は"
            "層の一番内側(同じ層なら名前順で先頭)の布のものを使う"
        ),
        default="",
    )
    layer: bpy.props.IntProperty(
        name="Layer",
        description=(
            "重ね着の順番。0 が一番内側。層の違う布どうしが当たると、"
            "内側を動かさず外側だけを押し出す"
        ),
        default=0,
        min=0,
        max=9,
    )
    self_collision_thickness: bpy.props.FloatProperty(
        name="Self Thickness",
        description="自己衝突で保つ頂点間距離。メッシュのエッジ長より小さくすること",
        default=0.01,
        min=0.0,
        soft_max=0.05,
        precision=4,
        unit='LENGTH',
    )

    # --- 型紙オブジェクト(M8) ---
    pattern_object: bpy.props.PointerProperty(
        name="Pattern Object",
        description=(
            "この布の型紙として使うメッシュ(頂点が布と1対1で対応するもの)。"
            "編集すると、走っている布(再生・Dress・Adjust)の寸法が変わる。"
            "オブジェクトの位置と回転は置き場所だけで、スケールは型紙に効く"
        ),
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )

    # --- ピン留め ---
    pin_vertex_group: bpy.props.StringProperty(
        name="Pin Vertex Group",
        description="固定する頂点を指定する頂点グループ名(空欄なら固定なし)",
        default="",
    )

    # --- 縫製(M3) ---
    seam_close_frames: bpy.props.IntProperty(
        name="Seam Close Frames",
        description="縫い目が完全に閉じるまでのフレーム数(0で即座に閉じる)",
        default=30,
        min=0,
        soft_max=240,
    )
    seam_compliance: bpy.props.FloatProperty(
        name="Seam Compliance",
        description="縫製制約のコンプライアンス(0で強固に縫い合わせる)",
        default=0.0,
        min=0.0,
        soft_max=0.01,
        precision=6,
    )

    # --- キャッシュ ---
    use_cache: bpy.props.BoolProperty(
        name="Cache Frames",
        description="計算済みフレームをメモリに保持し、巻き戻し・スクラブを高速化する",
        default=True,
    )



_classes = (
    MUSLIN_PG_vertex_index,
    MUSLIN_PG_seam,
    MUSLIN_PG_elastic,
    MUSLIN_PG_tools,
    MUSLIN_PG_cloth,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    # シミュレーション設定は布ごとの性質なのでオブジェクトに持たせる。
    # シーンに残すのはツールと表示の設定だけ。
    bpy.types.Object.muslin = bpy.props.PointerProperty(type=MUSLIN_PG_cloth)
    bpy.types.Scene.muslin_tools = bpy.props.PointerProperty(type=MUSLIN_PG_tools)
    # シームは「どのメッシュに属するか」が本質なのでオブジェクトに持たせる
    bpy.types.Object.muslin_seams = bpy.props.CollectionProperty(type=MUSLIN_PG_seam)
    bpy.types.Object.muslin_seam_active = bpy.props.IntProperty(default=0)
    bpy.types.Object.muslin_elastics = bpy.props.CollectionProperty(type=MUSLIN_PG_elastic)
    bpy.types.Object.muslin_elastic_active = bpy.props.IntProperty(default=0)


def unregister():
    del bpy.types.Object.muslin_elastic_active
    del bpy.types.Object.muslin_elastics
    del bpy.types.Object.muslin_seam_active
    del bpy.types.Object.muslin_seams
    del bpy.types.Scene.muslin_tools
    del bpy.types.Object.muslin
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
