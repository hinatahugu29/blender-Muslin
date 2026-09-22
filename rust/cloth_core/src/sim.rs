//! XPBDベースのクロスシミュレーションコア(Python非依存)。
//!
//! 設計方針:
//! - `pyo3` に一切依存しないので `cargo test` で単体検証できる。
//! - 制約は「距離制約」を基本形とし、伸び(stretch)・曲げ(bending)・縫製(seam)を
//!   同じソルバーで扱う。縫製だけは rest_length をアニメーションさせる点が異なる。
//! - XPBD の λ(ラグランジュ乗数)はサブステップごとにリセットし、反復内で累積する
//!   (これにより compliance が実際の物性値として反復回数に依存しなくなる)。

use crate::bending::{solve_bending, BendingConstraint};
use crate::collision::{SpatialHash, TriangleBvh};
use crate::math::Vec3;

/// つまんだ頂点を目標位置へ引く制約。`C = |x - target|`。
#[derive(Clone, Debug)]
struct Grab {
    index: usize,
    target: Vec3,
    compliance: f64,
    lambda: f64,
}

impl Grab {
    fn solve(&mut self, positions: &mut [Vec3], inv_mass: &[f64], inv_dt2: f64) {
        let w = inv_mass[self.index];
        if w == 0.0 {
            return; // ピン留めされた頂点は動かさない
        }
        let d = positions[self.index].sub(self.target);
        let c = d.length();
        if c < 1e-12 {
            return;
        }
        let alpha = self.compliance * inv_dt2;
        let dl = (-c - alpha * self.lambda) / (w + alpha);
        self.lambda += dl;
        positions[self.index] = positions[self.index].add(d.scale(w * dl / c));
    }
}

/// 2頂点間の距離制約。伸び・曲げ・縫製すべてこの形で表現する。
#[derive(Clone, Debug)]
pub struct DistanceConstraint {
    pub i0: usize,
    pub i1: usize,
    /// 目標長。縫製制約では時間とともに 0 に近づける。
    pub rest_length: f64,
    /// 生成時の初期長。縫製の閉じ具合の補間に使う。
    pub initial_length: f64,
    /// XPBD コンプライアンス(剛性の逆数)。0 = 完全剛体。
    pub compliance: f64,
}

impl DistanceConstraint {
    pub fn new(i0: usize, i1: usize, rest_length: f64, compliance: f64) -> Self {
        DistanceConstraint {
            i0,
            i1,
            rest_length,
            initial_length: rest_length,
            compliance,
        }
    }
}

/// 1ステップ分のシミュレーションパラメータ。
#[derive(Clone, Copy, Debug)]
pub struct SimParams {
    /// 重力加速度ベクトル(m/s^2)。通常 (0, 0, -9.81)。
    pub gravity: Vec3,
    /// 風圧(N/m^2)。加速度ではなく「面に働く力」として扱うため、
    /// 同じ風でも密度の高い生地ほどはためきにくくなる。
    pub wind: Vec3,
    /// 速度減衰係数(0 = 減衰なし, 1 に近いほど強い)。1秒あたりの減衰率として扱う。
    pub damping: f64,
    /// XPBD 制約解決の反復回数。
    pub iterations: u32,
    /// サブステップ数。
    pub substeps: u32,
    /// 床面衝突を有効にするか(M2 の本格的なコリジョンまでの暫定機能)。
    pub floor_enabled: bool,
    /// 床面の高さ(ワールドZ)。
    pub floor_z: f64,
    /// 床面の摩擦係数(0 = 滑る, 1 = 完全に止まる)。
    pub friction: f64,

    // --- コリジョンオブジェクト (M2) ---
    /// コリジョンオブジェクトとの衝突を有効にするか。
    pub collision_enabled: bool,
    /// 布の厚み。コライダー表面からこの距離を保つ。
    pub collision_thickness: f64,
    /// コライダー表面の摩擦(0 = 滑る, 1 = 貼り付く)。
    pub collision_friction: f64,

    // --- 自己衝突 (M2) ---
    /// 自己衝突を有効にするか(重いので既定は無効)。
    pub self_collision_enabled: bool,
    /// 自己衝突時に保つ頂点間距離。
    pub self_collision_thickness: f64,

    // --- 衝突後の補正 ---
    /// コリジョンオブジェクトの探索をフレーム先頭で1回だけ行う。
    ///
    /// 衝突解決はサブステップ毎に走るため、サブステップを増やすと BVH 探索が
    /// 比例して増える。候補をフレーム先頭で作って使い回せば、サブステップを
    /// 増やしても探索コストは増えない。1フレーム内の移動量を見込んだ
    /// 半径で拾うので、途中で近づいた接触も取りこぼさない。
    ///
    /// 自己衝突には適用しない。計測の結果、自己衝突ではハッシュ再構築が
    /// 全体の11%に過ぎず、支配的なのは近傍の反復そのものだった。加えて
    /// 移動見込みで探索範囲を広げると候補が実接触の1000倍以上に膨れる
    /// (接触1,000に対し候補1,174,163)。毎サブステップ作り直すほうが速い。
    pub cache_broadphase: bool,
    /// Chebyshev 加速に使うスペクトル半径の見積もり(0 で無効)。
    ///
    /// 反復そのものを加速する手法なので、収束先は変わらない。つまり
    /// **材質を変えずに収束を速められる**。1 に近いほど強く加速するが、
    /// 大きすぎると振動して発散する。
    pub chebyshev_radius: f64,
    /// 衝突解決の「後」に伸び制約を解き直す回数。
    ///
    /// 衝突の押し出しはサブステップ内の制約ループの外側で走るため、
    /// 押し広げられた分を伸び制約が回収する機会がない。
    /// ここで数回だけ解き直すと、押し出しを保ったまま伸びを戻せる。
    /// 0 で従来どおり(補正なし)。
    pub post_collision_iterations: u32,
    /// 布どうしの自己衝突を、前後の位置を結ぶ線分で判定する(掃過判定)。
    ///
    /// 位置だけを見る判定は、1サブステップの移動量が厚みを超えると抜ける
    /// (抜けない上限速度 = 厚み x fps x substeps)。線分で見れば、飛び越えた
    /// 事実そのものを捕まえられる。
    pub continuous_self_collision: bool,
}

impl Default for SimParams {
    fn default() -> Self {
        SimParams {
            gravity: Vec3::new(0.0, 0.0, -9.81),
            wind: Vec3::zero(),
            damping: 0.01,
            iterations: 10,
            substeps: 4,
            floor_enabled: false,
            floor_z: 0.0,
            friction: 0.8,
            collision_enabled: true,
            collision_thickness: 0.005,
            collision_friction: 0.8,
            self_collision_enabled: false,
            self_collision_thickness: 0.01,
            post_collision_iterations: 2,
            cache_broadphase: false,
            chebyshev_radius: 0.0,
            continuous_self_collision: false,
        }
    }
}

/// 移動見積もりに掛ける安全係数。
///
/// 衝突の瞬間は制約力で頂点が速度予測以上に動くため、素の見積もりだと
/// その1フレームだけ食い込むことがある(実測で最大 0.0149)。
pub const MARGIN_SAFETY: f64 = 1.5;

/// 自己衝突の近傍列挙を並列に回すときの、1タスクが受け持つ頂点数。
///
/// 自分で区切ることで結合順を固定し、結果を決定的に保つ。
const SELF_COLLISION_CHUNK: usize = 512;

/// Chebyshev 加速を始めるまでに素の反復を回す回数(Wang 2015 の S)。
///
/// 反復の初期は残差が大きく、そのまま増幅すると跳ねる。
///
/// **2 では壊れる組み合わせがあった。** ω の列は「最後まで回しきる」前提の
/// もので、途中で打ち切ると特定の長さで誤差を増幅する。substeps 32 で測ると、
/// ρ=0.95 かつ反復 5〜6 のときだけ片持ちの先端が**固定端より上に反り返った**
/// (垂れ比 -0.28)。反復 4 でも 7 以上でも無事という、帯状の不安定だった。
/// 発散判定には掛からないので、黙って誤った布が出る。
///
/// 5 にすると、反復が 5 以下なら加速が一度も走らない = 素の反復と同じになり、
/// **構造的に安全**。6 以上でも反り返りは出なくなった。
///
/// **ただしこの確認は曲げ(垂れ比)しか見ておらず、伸びを見落としていた。**
/// 伸び制約は曲げよりはるかに硬いので、曲げに合う ρ でも伸びには強すぎる。
/// 反復を上げると伸びが壊れる別の穴が残っていた(`CHEBYSHEV_RESTART` を参照)。
///
/// 遅延を伸ばしたぶん効きは落ちるが、ρ を上げれば取り戻せる。
/// 反復10・ρ0.98 で垂れ比 0.4078 と、危険だった頃の設定(遅延2・ρ0.95 で
/// 0.3986)とほぼ同じところまで戻る。
/// 掃過判定で経路上を拾う最大の刻み数。
///
/// 刻みは厚み間隔なので、速い頂点ほど増える。上限を置かないと、異常に速い
/// 頂点1つのために走査が膨らむ。32 刻みは厚みの 32倍の移動に相当し、
/// substeps 4・60fps・厚み 0.016 なら 123 m/s まで覆う。
const MAX_SWEPT_SAMPLES: usize = 32;

const CHEBYSHEV_DELAY: u32 = 5;

/// Chebyshev 加速をリスタートする周期(反復数)。
///
/// **これが無いと反復数を上げたときに布が吹き飛ぶ。** 外挿は前回位置との
/// 差を ω 倍する操作で、ω は反復が進むほど固定点(ρ=0.98 で 1.67)に近づく。
/// PBD は非線形なので、この外挿を延々と積み上げると誤差が発散する。
///
/// 0.4m 角の布を一辺で吊るし、伸び誤差と広がりを測った(リスタート無し):
///
/// | 反復 | ρ=0     | ρ=0.95  | ρ=0.98        |
/// |------|---------|---------|---------------|
/// | 10   | 0.00428 | 0.00471 | 0.00331       |
/// | 20   | 0.00412 | 0.00185 | 0.08324       |
/// | 40   | 0.00253 | 0.02184 | 14.3(12.5m まで飛ぶ) |
///
/// `is_finite()` は全部 true を返すので、発散判定では捕まらない。
/// 周期的に ω を 1 に戻して基準位置を取り直せば、積み上がりが切れる。
const CHEBYSHEV_RESTART: u32 = 10;

/// 摩擦を較正する基準のサブステップ数。
///
/// 摩擦はサブステップごとに1回掛かるので、1フレームの接線速度の残存率は
/// `(1-f)^substeps` になる。ここを基準に正規化して、Substeps を変えても
/// 摩擦の強さが変わらないようにする。値は既定の Substeps に合わせてあるので、
/// 既定のシーンの結果は変わらない。
const REFERENCE_SUBSTEPS: u32 = 4;

/// これ以上の頂点数なら並列化する。
///
/// 下回るとスレッド起動のコストが上回る(計測で決めた値)。
pub const PARALLEL_MIN_VERTICES: usize = 4000;

/// `Instant` からの経過をミリ秒で返す。
fn ms_since(t: std::time::Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1000.0
}

/// 1回の `step` に費やした時間の内訳(ミリ秒)。
///
/// ボトルネックがどこにあるかを推測ではなく実測で決めるために持つ。
///
/// 呼び出し回数は頂点数に依らず一定(既定設定で 1フレームあたり 240回程度)。
/// A/B 計測では 4,225頂点で 0.9% 以内。14,641頂点では実行間のばらつき(同一
/// バイナリで 34% の幅があった)に埋もれて、オーバーヘッドは検出できなかった。
#[derive(Default, Debug, Clone, Copy, PartialEq)]
pub struct StepTimings {
    /// 外力の適用と予測位置の更新(semi-implicit Euler)
    pub integrate: f64,
    /// 伸び制約の求解
    pub stretch: f64,
    /// 曲げ制約の求解
    pub bending: f64,
    /// 縫製制約の求解
    pub seam: f64,
    /// 床面との衝突
    pub floor: f64,
    /// コリジョンオブジェクトとの衝突(BVH 探索を含む)
    pub object_collision: f64,
    /// 自己衝突(空間ハッシュの再構築を含む)
    pub self_collision: f64,
    /// 空間ハッシュの再構築のみ(self_collision の内数)
    pub hash_rebuild: f64,
    /// 掃過判定で面まで戻した頂点の数(時間ではなく件数)。
    /// 0 でないなら、位置だけの判定では抜けていた頂点があったということ。
    pub self_tunneling: f64,
    /// 衝突後の伸び補正
    pub post_collision: f64,
    /// 位置差からの速度更新と摩擦
    pub velocity: f64,
    /// step 全体
    pub total: f64,
}

/// クロスシミュレーション本体。
pub struct ClothSim {
    pub positions: Vec<Vec3>,
    pub velocities: Vec<Vec3>,
    /// 逆質量。0 は固定頂点(ピン留め)。
    pub inv_mass: Vec<f64>,
    /// ピン留めを適用する前の逆質量。ピンの付け外しで質量分布が壊れないよう保持する。
    base_inv_mass: Vec<f64>,
    /// 各頂点が受け持つ面積(m^2)。風圧を力に変換するのに使う。
    pub vertex_area: Vec<f64>,

    pub stretch_constraints: Vec<DistanceConstraint>,
    /// 二面角による曲げ制約。距離制約では曲げ剛性を制御できないため
    /// (詳しくは `bending` モジュールを参照)、角度で直接扱う。
    pub bending_constraints: Vec<BendingConstraint>,
    /// 縫製制約(M3)。`seam_closure` によって rest_length が変化する。
    pub seam_constraints: Vec<DistanceConstraint>,

    /// 縫製の閉じ具合 0.0(初期長のまま) 〜 1.0(完全に縫い合わさる)。
    seam_closure: f64,

    /// ビューポートでつまんでいる頂点(M7)。無ければ None。
    grab: Option<Grab>,

    // XPBD の λ 用スクラッチ領域(毎ステップ確保し直さないよう保持)
    lambda_stretch: Vec<f64>,
    lambda_bending: Vec<f64>,
    lambda_seam: Vec<f64>,

    /// サブステップ開始時の位置。掃過判定で「どこから来たか」に使う。
    substep_start: Vec<Vec3>,

    /// 制約で直接結ばれた頂点対。自己衝突の対象から除外するのに使う
    /// (伸び制約と自己衝突が綱引きして布が破裂するのを防ぐ)。
    /// 標準の HashSet(SipHash)では候補ペアごとの参照が重い。
    /// 自己衝突の内側ループで毎回引くので軽量ハッシャを使う。
    constrained_pairs: crate::hashing::FastSet<(u32, u32)>,
    /// 縫い目で結ばれた頂点対。これも自己衝突から除外する。
    /// 縫い目は閉じると距離が 0 になるので、除外しないと厚みの分だけ
    /// 押し返されて、縫製と自己衝突が綱引きになる。
    /// `set_seams` で差し替わるので `constrained_pairs` とは分けて持つ。
    seam_pairs: crate::hashing::FastSet<(u32, u32)>,
    /// コリジョンオブジェクト(BVH付き三角形メッシュ)
    colliders: Vec<TriangleBvh>,
    /// このフレームの終点となるコライダー頂点。サブステップごとに補間する。
    ///
    /// コライダーはフレーム頭で1回しか更新されないので、速く動くと**重なる
    /// 瞬間がサンプルされずに素通りする**。実測では、球(直径 0.30m)が
    /// 15 m/s(1フレーム 0.25m)で布を通ると、開始位置によって 8回中2回
    /// 素通りした。60 m/s では 8回中5回。
    ///
    /// サブステップごとに始点から終点へ補間すれば、実効的な標本化が
    /// substeps 倍細かくなる。連続衝突判定(CCD)そのものではないが、
    /// 支配的な取りこぼしはこれで塞がる。
    collider_targets: Vec<Option<Vec<Vec3>>>,
    /// 直近のサブステップでコライダーが動いた最大距離。
    ///
    /// 探索半径をこのぶん広げる。半径は既定 0.025 しかないので、**大きな
    /// コライダーの内部深くに入った頂点は表面が遠くて検出されない**。
    /// 球(半径 0.15)の中心から 0.06 の頂点は表面まで 0.09 あり、圏外だった。
    collider_motion: f64,
    /// 布自身の三角形。頂点-三角形の自己衝突に使う。
    ///
    /// 頂点同士の反発だけでは、**頂点の並びがずれた2枚が素通りする**。
    /// 格子間隔 h の2枚を半セルずらすと頂点間の面内距離が 0.707h になり、
    /// 推奨の厚み 0.4h では一度も反応しない(実測で 81頂点すべてが貫通した)。
    triangles: Vec<[usize; 3]>,
    /// 自己衝突用の空間ハッシュ(毎サブステップ再構築)
    hash: SpatialHash,
    /// 頂点-三角形用の空間ハッシュ
    tri_hash: crate::collision::TriangleHash,
    /// 頂点-三角形の衝突候補。列挙の結果をここに貯めてから逐次で解く。
    self_tri_pairs: Vec<(u32, u32)>,
    /// 自己衝突の近傍列挙に使う作業領域。毎サブステップ確保し直さず使い回す。
    neighbor_scratch: Vec<usize>,
    /// 自己衝突で押し離す頂点対。列挙の結果をここに貯めてから逐次で解く。
    self_pairs: Vec<(u32, u32)>,
    /// 直近サブステップで接触した頂点数(デバッグ表示用)
    last_collision_count: usize,
    /// 直近フレームで作った衝突候補の数(キャッシュの効き具合を見るため)
    last_candidate_counts: (usize, usize),
    /// 直近 `step` の時間内訳
    timings: StepTimings,

    // --- Chebyshev 加速の作業領域 ---
    /// 2反復前の位置 x^(k-1)
    cheb_prev: Vec<Vec3>,
    /// 1反復前の位置 x^(k)
    cheb_cur: Vec<Vec3>,

    // --- 摩擦を頂点ごとにまとめる作業領域(毎サブステップ確保し直さない) ---
    /// その頂点の接触法線の和。正規化して代表の法線にする
    friction_normal: Vec<Vec3>,
    /// その頂点に掛ける摩擦係数(接触のうち最大)
    friction_coeff: Vec<f64>,
    /// 今回の呼び出しで触れたかを示す世代印。配列全体を毎回ゼロ埋めしない
    friction_stamp: Vec<u32>,
    /// 今回触れた頂点(ここだけを走査して速度を更新する)
    friction_touched: Vec<u32>,
    /// 世代番号
    friction_epoch: u32,

    // --- 広域探索のキャッシュ(cache_broadphase 用) ---
    /// (頂点, コライダー番号, 三角形番号)。フレーム先頭で作る。
    object_candidates: Vec<(u32, u32, u32)>,
}

impl ClothSim {
    /// 頂点位置とトポロジから初期化する。
    ///
    /// - `triangles` が空でなければ、三角形面積 × `density` から頂点質量を算出する
    ///   (面積の 1/3 を各頂点に配分)。空の場合は全頂点質量 1.0。
    /// - `pinned` の頂点は inv_mass = 0 になる。
    pub fn new(
        positions: Vec<Vec3>,
        edges: &[(usize, usize)],
        bending_quads: &[(usize, usize, usize, usize)],
        triangles: &[(usize, usize, usize)],
        pinned: &[usize],
        density: f64,
        stretch_compliance: f64,
        bending_compliance: f64,
    ) -> Self {
        let n = positions.len();

        let (inv_mass, vertex_area) =
            Self::compute_mass_distribution(&positions, triangles, density, pinned);

        let build = |pairs: &[(usize, usize)], compliance: f64| -> Vec<DistanceConstraint> {
            pairs
                .iter()
                .filter(|&&(a, b)| a < n && b < n && a != b)
                .map(|&(a, b)| {
                    let rest = positions[a].sub(positions[b]).length();
                    DistanceConstraint::new(a, b, rest, compliance)
                })
                .collect()
        };

        let stretch_constraints = build(edges, stretch_compliance);
        let bending_constraints: Vec<BendingConstraint> = bending_quads
            .iter()
            .filter_map(|&(a, b, c, d)| {
                let n = positions.len();
                if a >= n || b >= n || c >= n || d >= n {
                    return None;
                }
                BendingConstraint::new(&positions, a, b, c, d, bending_compliance)
            })
            .collect();
        // ピン留め前の逆質量(ピンの解除時に元の質量分布へ戻せるようにする)
        let base_inv_mass: Vec<f64> = inv_mass
            .iter()
            .zip(vertex_area.iter())
            .map(|(w, a)| {
                if *w > 0.0 {
                    *w
                } else if triangles.is_empty() || density <= 0.0 {
                    1.0
                } else {
                    1.0 / (a * density).max(1e-12)
                }
            })
            .collect();

        let mut constrained_pairs = crate::hashing::FastSet::default();
        for c in stretch_constraints.iter() {
            let (a, b) = (c.i0.min(c.i1) as u32, c.i0.max(c.i1) as u32);
            constrained_pairs.insert((a, b));
        }
        // 曲げ制約でつながる対角の頂点対も自己衝突から除く。
        // 距離制約だった頃と同じ扱い(近すぎる隣どうしが押し合うのを防ぐ)。
        for c in bending_constraints.iter() {
            let (a, b) = (c.p3.min(c.p4) as u32, c.p3.max(c.p4) as u32);
            constrained_pairs.insert((a, b));
        }

        let mut sim = ClothSim {
            velocities: vec![Vec3::zero(); n],
            positions,
            inv_mass,
            base_inv_mass,
            vertex_area,
            constrained_pairs,
            seam_pairs: Default::default(),
            colliders: Vec::new(),
            collider_targets: Vec::new(),
            collider_motion: 0.0,
            triangles: triangles
                .iter()
                .filter(|&&(a, b, c)| a < n && b < n && c < n && a != b && b != c && a != c)
                .map(|&(a, b, c)| [a, b, c])
                .collect(),
            hash: SpatialHash::new(0.01),
            tri_hash: crate::collision::TriangleHash::new(),
            self_tri_pairs: Vec::new(),
            neighbor_scratch: Vec::new(),
            self_pairs: Vec::new(),
            last_collision_count: 0,
            last_candidate_counts: (0, 0),
            timings: StepTimings::default(),
            cheb_prev: vec![Vec3::zero(); n],
            cheb_cur: vec![Vec3::zero(); n],
            friction_normal: vec![Vec3::zero(); n],
            friction_coeff: vec![0.0; n],
            friction_stamp: vec![0; n],
            friction_touched: Vec::new(),
            friction_epoch: 0,
            object_candidates: Vec::new(),
            lambda_stretch: vec![0.0; stretch_constraints.len()],
            lambda_bending: vec![0.0; bending_constraints.len()],
            lambda_seam: Vec::new(),
            substep_start: vec![Vec3::zero(); n],
            stretch_constraints,
            bending_constraints,
            seam_constraints: Vec::new(),
            seam_closure: 0.0,
            grab: None,
        };
        sim.lambda_seam.clear();
        sim
    }

    /// 三角形面積と密度から、各頂点の逆質量と受け持ち面積を求める。
    /// 面が無い、または密度が 0 以下の場合は一様質量 1.0 / 面積 1.0 とする。
    fn compute_mass_distribution(
        positions: &[Vec3],
        triangles: &[(usize, usize, usize)],
        density: f64,
        pinned: &[usize],
    ) -> (Vec<f64>, Vec<f64>) {
        let n = positions.len();
        let mut area = vec![0.0_f64; n];

        for &(a, b, c) in triangles {
            if a >= n || b >= n || c >= n {
                continue;
            }
            let tri_area = positions[b]
                .sub(positions[a])
                .cross(positions[c].sub(positions[a]))
                .length()
                * 0.5;
            let share = tri_area / 3.0;
            area[a] += share;
            area[b] += share;
            area[c] += share;
        }

        // 面に属さない頂点は平均面積で埋める(孤立頂点対策)
        let positive: Vec<f64> = area.iter().copied().filter(|a| *a > 0.0).collect();
        let fallback_area = if positive.is_empty() {
            1.0
        } else {
            positive.iter().sum::<f64>() / positive.len() as f64
        };
        for a in area.iter_mut() {
            if *a <= 0.0 {
                *a = fallback_area;
            }
        }

        let mass: Vec<f64> = if triangles.is_empty() || density <= 0.0 {
            vec![1.0; n]
        } else {
            area.iter().map(|a| a * density).collect()
        };

        let mut inv_mass: Vec<f64> = mass
            .iter()
            .map(|m| if *m > 0.0 { 1.0 / *m } else { 0.0 })
            .collect();
        for &idx in pinned {
            if idx < inv_mass.len() {
                inv_mass[idx] = 0.0;
            }
        }
        (inv_mass, area)
    }

    pub fn vertex_count(&self) -> usize {
        self.positions.len()
    }

    /// 生地の面密度を差し替える。位置・速度・静止長には触らない。
    ///
    /// 生地を選び直したときに、走らせたまま反映できるようにするためのもの。
    /// 組み立て直すと布が初期姿勢に戻ってしまい、見比べられない。
    /// ピン留めした頂点(inv_mass == 0)はピンのまま残す。
    pub fn set_density(&mut self, density: f64) {
        let uniform = density <= 0.0;
        for i in 0..self.positions.len() {
            let mass = if uniform {
                1.0
            } else {
                (self.vertex_area[i] * density).max(1e-12)
            };
            let inv = 1.0 / mass;
            self.base_inv_mass[i] = inv;
            // ピン留めされている頂点はそのまま(質量無限)にしておく
            if self.inv_mass[i] != 0.0 {
                self.inv_mass[i] = inv;
            }
        }
    }

    /// 伸び・曲げのコンプライアンスを差し替える。静止長・静止角は保持する。
    pub fn set_compliances(&mut self, stretch: f64, bending: f64) {
        for c in self.stretch_constraints.iter_mut() {
            c.compliance = stretch;
        }
        for c in self.bending_constraints.iter_mut() {
            c.compliance = bending;
        }
    }

    /// ピン留めを再設定する。面積から算出した質量分布は保持される。
    pub fn set_pinned(&mut self, pinned: &[usize]) {
        self.inv_mass.copy_from_slice(&self.base_inv_mass);
        for &idx in pinned {
            if idx < self.inv_mass.len() {
                self.inv_mass[idx] = 0.0;
            }
        }
    }

    // ------------------------------------------------------ コリジョン (M2)

    /// コリジョンオブジェクトを追加し、そのインデックスを返す。
    pub fn add_collider(&mut self, vertices: Vec<Vec3>, triangles: Vec<[usize; 3]>) -> usize {
        self.colliders.push(TriangleBvh::new(vertices, triangles));
        self.collider_targets.push(None);
        self.colliders.len() - 1
    }

    /// コライダーの頂点を**その場で**更新する(BVHは再フィット)。
    ///
    /// 速く動くコライダーには [`Self::set_collider_target`] を使うこと。
    /// こちらはフレーム頭で1回だけ動かすので、1フレームの移動がコライダー
    /// 自身の大きさに近づくと布を素通りする。
    pub fn update_collider(&mut self, index: usize, vertices: Vec<Vec3>) -> Result<(), String> {
        self.check_collider_vertices(index, vertices.len())?;
        self.colliders[index].refit(vertices);
        self.collider_targets[index] = None;
        Ok(())
    }

    /// このフレームの終点を指定する。サブステップごとに補間して動かす。
    ///
    /// 現在位置を始点、`vertices` を終点として、各サブステップで線形補間する。
    /// 実効的な標本化が substeps 倍細かくなるので、速いコライダーの
    /// 取りこぼしが減る。`step` を1回呼ぶと終点に到達し、指定は消える。
    pub fn set_collider_target(
        &mut self,
        index: usize,
        vertices: Vec<Vec3>,
    ) -> Result<(), String> {
        self.check_collider_vertices(index, vertices.len())?;
        self.collider_targets[index] = Some(vertices);
        Ok(())
    }

    fn check_collider_vertices(&self, index: usize, len: usize) -> Result<(), String> {
        let collider = self
            .colliders
            .get(index)
            .ok_or_else(|| format!("collider index {index} out of range"))?;
        if collider.vertices.len() != len {
            return Err(format!(
                "collider vertex count mismatch: expected {}, got {}",
                collider.vertices.len(),
                len
            ));
        }
        Ok(())
    }

    /// サブステップ `step_index`(1 始まり)の位置までコライダーを進める。
    fn advance_colliders(&mut self, step_index: u32, substeps: u32) {
        if self.collider_targets.iter().all(|t| t.is_none()) {
            self.collider_motion = 0.0;
            return;
        }
        // 始点を別に持たずに正確な線形補間をする。
        // いま from + (to-from)*(s-1)/n にいるので、残り (n-s+1) 回で
        // 終点に着くよう「残り距離の 1/(n-s+1)」だけ進めればよい。
        // s = n では係数 1 になり、終点そのものになる(丸め誤差が残らない)。
        let remaining = substeps.saturating_sub(step_index) + 1;
        let beta = 1.0 / remaining as f64;

        for (i, target) in self.collider_targets.iter().enumerate() {
            let Some(target) = target else { continue };
            let collider = &mut self.colliders[i];
            let moved: Vec<Vec3> = if remaining <= 1 {
                target.clone()
            } else {
                collider
                    .vertices
                    .iter()
                    .zip(target.iter())
                    .map(|(cur, to)| cur.add(to.sub(*cur).scale(beta)))
                    .collect()
            };
            let mut motion: f64 = 0.0;
            for (from, to) in collider.vertices.iter().zip(moved.iter()) {
                motion = motion.max(to.sub(*from).length());
            }
            self.collider_motion = self.collider_motion.max(motion);
            collider.refit(moved);
        }
    }

    pub fn clear_colliders(&mut self) {
        self.colliders.clear();
        self.collider_targets.clear();
    }

    pub fn collider_count(&self) -> usize {
        self.colliders.len()
    }

    pub fn collider_triangle_count(&self) -> usize {
        self.colliders.iter().map(|c| c.triangle_count()).sum()
    }

    pub fn last_collision_count(&self) -> usize {
        self.last_collision_count
    }

    /// 縫製制約を設定する(M3)。頂点ペアの現在距離を initial_length として記録する。
    pub fn set_seams(&mut self, pairs: &[(usize, usize)], compliance: f64) {
        let n = self.positions.len();
        self.seam_constraints = pairs
            .iter()
            .filter(|&&(a, b)| a < n && b < n && a != b)
            .map(|&(a, b)| {
                let len = self.positions[a].sub(self.positions[b]).length();
                DistanceConstraint::new(a, b, len, compliance)
            })
            .collect();
        self.seam_pairs = self
            .seam_constraints
            .iter()
            .map(|c| (c.i0.min(c.i1) as u32, c.i0.max(c.i1) as u32))
            .collect();
        self.lambda_seam = vec![0.0; self.seam_constraints.len()];
        self.apply_seam_closure();
    }

    /// 頂点 `index` をつまんで `target` へ引く(ビューポートで布を動かす操作)。
    ///
    /// ピン留めと違い、コンプライアンスを持つ XPBD の制約として解く。
    /// 反復ごとに目標へ寄せる方式(先行事例 Taremin Cloth のソフトピン)だと、
    /// 効き方が反復数で変わってしまう。ピン留めされた頂点はつまめない。
    pub fn set_grab(&mut self, index: usize, target: Vec3, compliance: f64) -> Result<(), String> {
        if index >= self.positions.len() {
            return Err(format!("grab index {index} out of range"));
        }
        let lambda = match &self.grab {
            Some(g) if g.index == index => g.lambda,
            _ => 0.0,
        };
        self.grab = Some(Grab {
            index,
            target,
            compliance: compliance.max(0.0),
            lambda,
        });
        Ok(())
    }

    /// つまむのをやめる。
    pub fn clear_grab(&mut self) {
        self.grab = None;
    }

    /// つまんでいる頂点。
    pub fn grabbed_vertex(&self) -> Option<usize> {
        self.grab.as_ref().map(|g| g.index)
    }

    /// 縫い合わせ進行度を 0.0〜1.0 で設定する。1.0 で rest_length が 0(完全に縫合)。
    pub fn set_seam_closure(&mut self, closure: f64) {
        self.seam_closure = closure.clamp(0.0, 1.0);
        self.apply_seam_closure();
    }

    pub fn seam_closure(&self) -> f64 {
        self.seam_closure
    }

    fn apply_seam_closure(&mut self) {
        let t = self.seam_closure;
        for c in self.seam_constraints.iter_mut() {
            c.rest_length = c.initial_length * (1.0 - t);
        }
    }

    /// dt 秒だけ進める。内部で substeps 回に分割する。
    pub fn step(&mut self, dt: f64, params: &SimParams) {
        if dt <= 0.0 || !dt.is_finite() {
            return;
        }
        let step_start = std::time::Instant::now();
        self.timings = StepTimings::default();

        if params.cache_broadphase {
            self.refresh_broadphase(dt, params);
        }

        let substeps = params.substeps.max(1);
        let sub_dt = dt / substeps as f64;
        for s in 0..substeps {
            // コライダーを先に進めてから布を解く。フレーム頭で1回だけ動かすと
            // 速いコライダーが布を素通りする
            self.advance_colliders(s + 1, substeps);
            self.substep(sub_dt, params);
        }
        for target in self.collider_targets.iter_mut() {
            *target = None;
        }

        self.timings.total = step_start.elapsed().as_secs_f64() * 1000.0;
    }

    /// 直近 `step` の時間内訳(ミリ秒)。
    pub fn timings(&self) -> StepTimings {
        self.timings
    }

    /// 直近フレームで作った衝突候補の数 (オブジェクト, 自己衝突ペア)。
    pub fn last_candidate_counts(&self) -> (usize, usize) {
        self.last_candidate_counts
    }

    fn substep(&mut self, dt: f64, params: &SimParams) {
        let n = self.positions.len();
        let has_wind = params.wind.length() > 0.0;

        // 減衰: 1秒あたり damping の割合を失う想定で dt 補正
        let damp = if params.damping > 0.0 {
            (1.0 - params.damping.clamp(0.0, 1.0)).powf(dt.max(0.0))
        } else {
            1.0
        };

        let t_integrate = std::time::Instant::now();
        let prev_positions = self.positions.clone();
        if params.continuous_self_collision {
            self.substep_start.clear();
            self.substep_start.extend_from_slice(&self.positions);
        }

        // 予測位置(semi-implicit Euler)
        for i in 0..n {
            if self.inv_mass[i] == 0.0 {
                self.velocities[i] = Vec3::zero();
                continue;
            }
            // 重力は加速度、風は「面積に働く力」なので質量で割って加速度に変換する
            let mut accel = params.gravity;
            if has_wind {
                accel = accel.add(params.wind.scale(self.vertex_area[i] * self.inv_mass[i]));
            }
            self.velocities[i] = self.velocities[i].scale(damp).add(accel.scale(dt));
            self.positions[i] = self.positions[i].add(self.velocities[i].scale(dt));
        }

        self.timings.integrate += ms_since(t_integrate);

        // λ をサブステップ先頭でリセット(XPBD)
        self.lambda_stretch.iter_mut().for_each(|l| *l = 0.0);
        self.lambda_bending.iter_mut().for_each(|l| *l = 0.0);
        self.lambda_seam.iter_mut().for_each(|l| *l = 0.0);
        if let Some(g) = self.grab.as_mut() {
            g.lambda = 0.0;
        }

        let inv_dt2 = 1.0 / (dt * dt);

        // Chebyshev 加速(Wang 2015)。反復そのものを加速するので、収束先は
        // 変わらない = 材質を変えない。制約を足す方向(広い間隔の曲げ制約)は
        // エネルギーを足してしまいドレープを潰したので、こちらを試す。
        //
        //   x^(k+1) = ω(x̃^(k+1) - x^(k-1)) + x^(k-1)
        //   ω は 1 → 2/(2-ρ²) → 4/(4-ρ²ω) と更新する
        //
        // ω=1 のときは素の反復と一致する。固定頂点は x̃ = x^k = x^(k-1) なので
        // この式でも動かない。
        let cheb = params.chebyshev_radius > 0.0;
        let rho2 = params.chebyshev_radius * params.chebyshev_radius;
        let mut omega = 1.0_f64;
        if cheb {
            self.cheb_prev.copy_from_slice(&self.positions);
        }

        for k in 0..params.iterations.max(1) {
            if cheb {
                self.cheb_cur.copy_from_slice(&self.positions);
            }

            let t = std::time::Instant::now();
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.stretch_constraints,
                &mut self.lambda_stretch,
                inv_dt2,
            );
            self.timings.stretch += ms_since(t);

            let t = std::time::Instant::now();
            solve_bending(
                &mut self.positions,
                &self.inv_mass,
                &self.bending_constraints,
                &mut self.lambda_bending,
                inv_dt2,
            );
            self.timings.bending += ms_since(t);

            let t = std::time::Instant::now();
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.seam_constraints,
                &mut self.lambda_seam,
                inv_dt2,
            );
            self.timings.seam += ms_since(t);

            if let Some(g) = self.grab.as_mut() {
                g.solve(&mut self.positions, &self.inv_mass, inv_dt2);
            }

            if cheb {
                // 一定周期で加速をリスタートする。外挿の積み上がりを切らないと
                // 反復数を上げたときに発散する(CHEBYSHEV_RESTART の表を参照)。
                let phase = k % CHEBYSHEV_RESTART;

                // 最初の数回は加速しない(Wang 2015 の S)。初期の大きな残差を
                // そのまま増幅すると跳ねるため
                omega = if phase < CHEBYSHEV_DELAY {
                    1.0
                } else if phase == CHEBYSHEV_DELAY {
                    2.0 / (2.0 - rho2)
                } else {
                    4.0 / (4.0 - rho2 * omega)
                };

                if omega != 1.0 {
                    for i in 0..n {
                        let prev = self.cheb_prev[i];
                        self.positions[i] =
                            prev.add(self.positions[i].sub(prev).scale(omega));
                    }
                }
                // prev = x^k。cheb_cur は次の反復の頭で上書きするので、
                // コピーせず入れ替えるだけでよい(毎反復 n*24 バイトの節約)
                std::mem::swap(&mut self.cheb_prev, &mut self.cheb_cur);
            }
        }

        // 衝突解決(位置の押し出し)。接触した頂点と法線・摩擦を記録する。
        let contacts = self.resolve_collisions(params);

        // 押し出しで壊れた伸びを同じサブステップ内で回収する。
        // λ はリセットせず継続させる(サブステップ内での XPBD の一貫性を保つ)。
        //
        // 縫製も一緒に解く。伸びだけを戻すと、辺の長さを保とうとする力で
        // 縫い目の頂点が引き離され、閉じたはずの縫い目に隙間が残る。
        let t_post = std::time::Instant::now();
        for _ in 0..params.post_collision_iterations {
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.stretch_constraints,
                &mut self.lambda_stretch,
                inv_dt2,
            );
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.seam_constraints,
                &mut self.lambda_seam,
                inv_dt2,
            );
            // つまんでいる頂点も同じ理由で解く。伸びだけを戻すと、つまんだ頂点が
            // 目標から引き離される(反復 10 で 1.75cm 離れていた)
            if let Some(g) = self.grab.as_mut() {
                g.solve(&mut self.positions, &self.inv_mass, inv_dt2);
            }
        }
        self.timings.post_collision += ms_since(t_post);

        // 位置差から速度を更新(押し出し分も速度に反映される)
        let t_velocity = std::time::Instant::now();
        for i in 0..n {
            if self.inv_mass[i] == 0.0 {
                self.velocities[i] = Vec3::zero();
                continue;
            }
            self.velocities[i] = self.positions[i].sub(prev_positions[i]).scale(1.0 / dt);
        }

        // 接触点の摩擦: 面へ食い込む法線速度を除去し、接線速度を摩擦分だけ落とす。
        //
        // **頂点ごとに1回だけ掛ける。** 接触ごとに掛けると、接線速度に
        // (1 - friction) がその頂点の接触数ぶん累乗で掛かってしまう。
        // 自己衝突では1頂点が多数の対に現れるので影響が大きい:
        // friction 0.3 で 8対に含まれる頂点は、本来 70% 残るはずの接線速度が
        // 5.8% まで落ちていた(実測値が (1-f)^K に一致することを確認済み)。
        // 摩擦の強さが「たまたま何対に含まれたか」= メッシュ密度と局所配置で
        // 決まってしまい、折り重なった布が不自然に固着する。
        self.apply_contact_friction(&contacts, params.substeps.max(1));

        self.timings.velocity += ms_since(t_velocity);
        self.last_collision_count = contacts.len();
    }

    /// 接触の摩擦を**頂点ごとに1回だけ**適用する。
    ///
    /// 面へ食い込む法線速度の除去は接触ごとに行う(どの接触面にも入り込まない
    /// ようにするため。同じ法線に対しては冪等なので重複しても害がない)。
    /// 接線の減衰だけを1回にまとめる。ここを接触ごとに掛けると
    /// (1 - friction) が接触数ぶん累乗になり、摩擦の強さが接触対の個数、
    /// つまりメッシュ密度と局所配置で決まってしまう。
    ///
    /// 代表の法線は接触法線の和を正規化したもの。接触が1つなら従来と同じ。
    /// 上下から挟まれて法線が打ち消し合う場合は向きを決められないので、
    /// 速度全体を摩擦分だけ落とす。
    fn apply_contact_friction(&mut self, contacts: &[(usize, Vec3, f64)], substeps: u32) {
        if contacts.is_empty() {
            return;
        }

        self.friction_epoch = self.friction_epoch.wrapping_add(1);
        // wrapping で 0 に戻ると前回の印と衝突するので、その一巡だけ作り直す
        if self.friction_epoch == 0 {
            self.friction_stamp.iter_mut().for_each(|s| *s = 0);
            self.friction_epoch = 1;
        }
        let epoch = self.friction_epoch;
        self.friction_touched.clear();

        for &(i, normal, friction) in contacts {
            if self.friction_stamp[i] != epoch {
                self.friction_stamp[i] = epoch;
                self.friction_normal[i] = Vec3::zero();
                self.friction_coeff[i] = 0.0;
                self.friction_touched.push(i as u32);
            }
            self.friction_normal[i] = self.friction_normal[i].add(normal);
            if friction > self.friction_coeff[i] {
                self.friction_coeff[i] = friction;
            }

            // 食い込む向きの法線速度はこの場で落とす
            let v = self.velocities[i];
            let vn = v.dot(normal);
            if vn < 0.0 {
                self.velocities[i] = v.sub(normal.scale(vn));
            }
        }

        // サブステップ数で正規化する。摩擦はサブステップごとに1回掛かるので、
        // そのままだと1フレームの残存率が (1-f)^substeps になり、**摩擦の
        // 強さが Substeps で変わってしまう**。既定の 0.3 は substeps 4 で
        // 0.24、substeps 16 で 0.0033 と 70倍違い、球に落とした布が
        // substeps 4 では滑り落ちるのに 16 では留まる、という状態だった。
        //
        // 基準を既定の Substeps (= 4) に置くので、**既定のシーンの結果は
        // 変わらない**(指数がちょうど 1 になる)。他のサブステップ数が
        // 既定と同じ摩擦になる。
        let exponent = REFERENCE_SUBSTEPS as f64 / substeps as f64;

        for &idx in self.friction_touched.iter() {
            let i = idx as usize;
            let friction = self.friction_coeff[i].clamp(0.0, 1.0);
            if friction <= 0.0 {
                continue;
            }
            let keep = (1.0 - friction).powf(exponent);
            let v = self.velocities[i];
            match self.friction_normal[i].normalized() {
                Some(n) => {
                    let vn = v.dot(n);
                    let v_tangent = v.sub(n.scale(vn));
                    self.velocities[i] = n.scale(vn.max(0.0)).add(v_tangent.scale(keep));
                }
                // 法線が打ち消し合った = 挟まれている。接線を決められない
                None => self.velocities[i] = v.scale(keep),
            }
        }
    }

    /// 床面・コリジョンオブジェクト・自己衝突をまとめて解決し、接触情報を返す。
    /// 戻り値: (頂点インデックス, 接触法線, 摩擦係数)
    fn resolve_collisions(&mut self, params: &SimParams) -> Vec<(usize, Vec3, f64)> {
        let mut contacts = Vec::new();

        let t_floor = std::time::Instant::now();
        if params.floor_enabled {
            for i in 0..self.positions.len() {
                if self.inv_mass[i] == 0.0 {
                    continue;
                }
                if self.positions[i].z < params.floor_z {
                    self.positions[i].z = params.floor_z;
                    contacts.push((i, Vec3::new(0.0, 0.0, 1.0), params.friction));
                }
            }
        }

        self.timings.floor += ms_since(t_floor);

        let t_object = std::time::Instant::now();
        if params.collision_enabled && !self.colliders.is_empty() {
            if params.cache_broadphase {
                self.resolve_object_collisions_cached(params, &mut contacts);
            } else {
                self.resolve_object_collisions(params, &mut contacts);
            }
        }
        self.timings.object_collision += ms_since(t_object);

        // 自己衝突はキャッシュしない(理由は SimParams::cache_broadphase を参照)
        let t_self = std::time::Instant::now();
        if params.self_collision_enabled {
            self.resolve_self_collisions(params, &mut contacts);
        }
        self.timings.self_collision += ms_since(t_self);

        contacts
    }

    /// 頂点 `i` が1フレームで動きうる距離の見積もり。
    ///
    /// 全頂点の最大速度を一律に使うと、速い頂点が1つあるだけで全頂点の
    /// 探索半径が膨らみ、候補数が爆発する(実測でキャッシュが2〜3倍遅くなった)。
    /// 頂点ごとの速度で見積もる。
    ///
    /// 速度だけでは、フレーム先頭で止まっていて途中で加速する頂点を
    /// 取りこぼす(実測で 0.0107 の食い込みが出た)。外力による増分も足す。
    fn motion_margin(&self, i: usize, dt: f64, params: &SimParams) -> f64 {
        let accel = params.gravity.length() + params.wind.length();
        MARGIN_SAFETY * (self.velocities[i].length() * dt + 0.5 * accel * dt * dt)
    }

    /// 衝突の候補をフレーム先頭で1回だけ作る。
    ///
    /// 以降のサブステップは候補に対する距離判定と押し出しだけを行うので、
    /// BVH 探索と空間ハッシュ再構築がサブステップ数に比例しなくなる。
    fn refresh_broadphase(&mut self, dt: f64, params: &SimParams) {
        self.object_candidates.clear();
        if params.collision_enabled && !self.colliders.is_empty() {
            let thickness = params.collision_thickness.max(0.0);
            let base_radius = (thickness * 4.0).max(thickness + 0.02);
            for i in 0..self.positions.len() {
                if self.inv_mass[i] == 0.0 {
                    continue;
                }
                let radius = base_radius + self.motion_margin(i, dt, params);
                let p = self.positions[i];
                for (ci, collider) in self.colliders.iter().enumerate() {
                    // 最近傍1個ではフレーム途中で入れ替わったときに足りない。
                    // 半径内の三角形を全て候補にする。
                    collider.for_each_triangle_within(p, radius, |tri| {
                        self.object_candidates.push((i as u32, ci as u32, tri as u32));
                    });
                }
            }
        }

        self.last_candidate_counts = (self.object_candidates.len(), 0);
    }

    /// キャッシュ済み候補を使ったコリジョン解決(BVH 探索をやり直さない)。
    fn resolve_object_collisions_cached(
        &mut self,
        params: &SimParams,
        contacts: &mut Vec<(usize, Vec3, f64)>,
    ) {
        let thickness = params.collision_thickness.max(0.0);

        // 候補は頂点ごとにまとまっているので、同じ頂点の区間を一気に処理して
        // その中の最近傍を選び直す(探索し直さずに従来と同じ結果を狙う)。
        let mut idx = 0;
        while idx < self.object_candidates.len() {
            let (vi, ci, _) = self.object_candidates[idx];
            let mut end = idx;
            while end < self.object_candidates.len()
                && self.object_candidates[end].0 == vi
                && self.object_candidates[end].1 == ci
            {
                end += 1;
            }

            let vi_us = vi as usize;
            let collider = &self.colliders[ci as usize];
            let p = self.positions[vi_us];

            let mut best: Option<(Vec3, usize, f64)> = None;
            for entry in &self.object_candidates[idx..end] {
                let tri = entry.2 as usize;
                let Some((a, b, c)) = collider.triangle(tri) else {
                    continue;
                };
                let q = crate::collision::closest_point_on_triangle(p, a, b, c);
                let d2 = q.sub(p).dot(q.sub(p));
                if best.is_none() || d2 < best.unwrap().2 {
                    best = Some((q, tri, d2));
                }
            }
            idx = end;

            let Some((closest, tri, _)) = best else {
                continue;
            };
            let Some(face_normal) = collider.triangle_normal(tri) else {
                continue;
            };

            let offset = p.sub(closest);
            let dist = offset.length();
            let signed = offset.dot(face_normal);

            let outward = if signed < 0.0 {
                face_normal
            } else if dist > 1e-9 {
                offset.scale(1.0 / dist)
            } else {
                face_normal
            };
            let penetration = if signed < 0.0 {
                thickness + dist
            } else {
                thickness - dist
            };

            if penetration > 0.0 {
                self.positions[vi_us] = closest.add(outward.scale(thickness));
                contacts.push((vi_us, outward, params.collision_friction));
            }
        }
    }

    /// コリジョンオブジェクト表面から布を厚み分だけ押し出す。
    ///
    /// **頂点ごとに独立**(自分の位置しか書かず、コライダーは読むだけ)なので
    /// 並列化しても結果が変わらない。距離制約と違って収束の性質にも影響しない。
    fn resolve_object_collisions(
        &mut self,
        params: &SimParams,
        contacts: &mut Vec<(usize, Vec3, f64)>,
    ) {
        use rayon::prelude::*;

        let thickness = params.collision_thickness.max(0.0);
        // 深く潜り込んだ頂点も拾えるよう、探索半径は厚みより大きめに取る。
        // 動くコライダーでは、そのサブステップの移動量ぶんさらに広げる。
        // 広げないと、通り抜ける途中で内部深くに入った頂点が圏外になる
        let search_radius =
            (thickness * 4.0).max(thickness + 0.02) + self.collider_motion;
        let friction = params.collision_friction;
        let colliders = &self.colliders;
        let inv_mass = &self.inv_mass;

        // 小さいメッシュでは並列化のオーバーヘッドが上回るので、閾値を設ける
        let parallel = self.positions.len() >= PARALLEL_MIN_VERTICES;

        let solve = |i: usize, p: &mut Vec3| -> Option<(usize, Vec3, f64)> {
            if inv_mass[i] == 0.0 {
                return None;
            }
            let mut hit = None;
            for collider in colliders.iter() {
                let Some((closest, tri, dist)) = collider.closest_point(*p, search_radius)
                else {
                    continue;
                };
                let Some(face_normal) = collider.triangle_normal(tri) else {
                    continue;
                };

                // 表面のどちら側にいるかは面法線との符号で判定する
                let offset = p.sub(closest);
                let signed = offset.dot(face_normal);
                let outward = if signed < 0.0 {
                    // 内部に潜り込んでいる → 面法線方向へ押し出す
                    face_normal
                } else if dist > 1e-9 {
                    // 外側にいる → 現在の離れる方向を維持する
                    offset.scale(1.0 / dist)
                } else {
                    face_normal
                };

                let penetration = if signed < 0.0 {
                    thickness + dist
                } else {
                    thickness - dist
                };

                if penetration > 0.0 {
                    *p = closest.add(outward.scale(thickness));
                    hit = Some((i, outward, friction));
                }
            }
            hit
        };

        let hits: Vec<(usize, Vec3, f64)> = if parallel {
            self.positions
                .par_iter_mut()
                .enumerate()
                .filter_map(|(i, p)| solve(i, p))
                .collect()
        } else {
            self.positions
                .iter_mut()
                .enumerate()
                .filter_map(|(i, p)| solve(i, p))
                .collect()
        };
        contacts.extend(hits);
    }

    /// 自己衝突: 空間ハッシュで近接頂点対を見つけ、厚み分だけ押し離す。
    ///
    /// 注意: 頂点同士の反発のみを扱う近似で、頂点-三角形の貫通は完全には防げない。
    /// 布の解像度に対して厚みを十分に取れば実用上は破綻しにくい。
    fn resolve_self_collisions(
        &mut self,
        params: &SimParams,
        contacts: &mut Vec<(usize, Vec3, f64)>,
    ) {
        let thickness = params.self_collision_thickness;
        if thickness <= 0.0 {
            return;
        }

        use rayon::prelude::*;

        // hash と作業領域を一時的に取り出す。self.positions を書き換えながら
        // 使うため、借用を分ける必要がある。
        let mut hash = std::mem::take(&mut self.hash);
        let mut neighbors = std::mem::take(&mut self.neighbor_scratch);
        let mut pairs = std::mem::take(&mut self.self_pairs);

        let t_hash = std::time::Instant::now();
        hash.rebuild_with(thickness, &self.positions);
        self.timings.hash_rebuild += ms_since(t_hash);

        let n = self.positions.len();

        // --- 近傍の列挙。読み取りだけなので並列にできる ---
        //
        // 従来は押し出しながら距離を見ていた(Gauss-Seidel)。分けたことで
        // 距離判定はこのフェーズ開始時の位置で行われる。押し出しの結果として
        // 新たに近づいた対は次のサブステップで拾う。
        let positions = &self.positions;
        let inv_mass = &self.inv_mass;
        let constrained = &self.constrained_pairs;
        let seam_pairs = &self.seam_pairs;

        let collect_for = |i: usize, out: &mut Vec<(u32, u32)>, buf: &mut Vec<usize>| {
            buf.clear();
            hash.for_each_neighbor(positions[i], |j| {
                if j > i {
                    buf.push(j);
                }
            });
            for &j in buf.iter() {
                // 制約で直接結ばれている頂点対は自己衝突から除外する
                let key = (i as u32, j as u32);
                if constrained.contains(&key) || seam_pairs.contains(&key) {
                    continue;
                }
                if inv_mass[i] + inv_mass[j] == 0.0 {
                    continue;
                }
                if positions[i].sub(positions[j]).length() < thickness {
                    out.push((i as u32, j as u32));
                }
            }
        };

        pairs.clear();
        if n >= PARALLEL_MIN_VERTICES {
            // 区切り方を自分で決めて順序を固定する。
            // rayon の fold/reduce に任せると分割位置が実行時の事情で変わりうるため、
            // 結合順(= Gauss-Seidel の順序)が揺れて結果が非決定的になりかねない。
            // ベイクとスクラブは決定性が前提なので、そこは譲れない。
            let starts: Vec<usize> = (0..n).step_by(SELF_COLLISION_CHUNK).collect();
            let chunks: Vec<Vec<(u32, u32)>> = starts
                .into_par_iter()
                .map(|start| {
                    let end = (start + SELF_COLLISION_CHUNK).min(n);
                    let mut out = Vec::new();
                    let mut buf = Vec::new();
                    for i in start..end {
                        collect_for(i, &mut out, &mut buf);
                    }
                    out
                })
                .collect();
            for chunk in chunks {
                pairs.extend(chunk);
            }
        } else {
            for i in 0..n {
                collect_for(i, &mut pairs, &mut neighbors);
            }
        }

        // --- 押し出し。頂点対が互いに書き込むので逐次で解く ---
        for idx in 0..pairs.len() {
            let (i, j) = pairs[idx];
            let (i, j) = (i as usize, j as usize);

            let w0 = self.inv_mass[i];
            let w1 = self.inv_mass[j];
            let w_sum = w0 + w1;
            if w_sum == 0.0 {
                continue;
            }

            let delta = self.positions[i].sub(self.positions[j]);
            let dist = delta.length();
            if dist >= thickness {
                continue;
            }

            // 完全に重なっている場合は決定的な方向へずらす
            let dir = delta.normalized().unwrap_or(Vec3::new(0.0, 0.0, 1.0));
            let correction = (thickness - dist) / w_sum;

            self.positions[i] = self.positions[i].add(dir.scale(correction * w0));
            self.positions[j] = self.positions[j].sub(dir.scale(correction * w1));

            contacts.push((i, dir, params.collision_friction));
            contacts.push((j, dir.scale(-1.0), params.collision_friction));
        }

        self.hash = hash;
        self.neighbor_scratch = neighbors;
        self.self_pairs = pairs;

        self.resolve_self_vertex_triangle(params, contacts);
    }

    /// 頂点-三角形の自己衝突。
    ///
    /// 頂点同士の反発だけでは、頂点の並びがずれた2枚が素通りする(格子間隔 h の
    /// 2枚を半セルずらすと頂点間距離が 0.707h になり、推奨の厚み 0.4h では
    /// 一度も反応しない)。面との距離を直接見て押し離す。
    ///
    /// 自分を含む三角形は除く。それ以外の近傍三角形は、厚みが推奨どおり
    /// エッジ長の 0.4倍以下なら誤検出しない(平らな格子で最も近い非隣接面まで
    /// 0.707h あるため)。
    fn resolve_self_vertex_triangle(
        &mut self,
        params: &SimParams,
        contacts: &mut Vec<(usize, Vec3, f64)>,
    ) {
        if self.triangles.is_empty() {
            return;
        }
        let thickness = params.self_collision_thickness;

        use rayon::prelude::*;

        let mut hash = std::mem::take(&mut self.tri_hash);
        let mut pairs = std::mem::take(&mut self.self_tri_pairs);

        let t_hash = std::time::Instant::now();
        hash.rebuild(&self.positions, &self.triangles, thickness);
        self.timings.hash_rebuild += ms_since(t_hash);

        let n = self.positions.len();

        // --- 掃過判定: 飛び越えた頂点を、通り抜けた面まで戻す ---
        //
        // 位置だけを見る判定は「飛び越えた先が本当に遠い」ので捕まえられない。
        // ここで面まで引き戻しておけば、あとは通常の押し出しがいつもどおり
        // 処理できる。押し出しの仕組みには手を入れない。
        if params.continuous_self_collision && self.substep_start.len() == n {
            let mut fixes: Vec<(u32, Vec3)> = Vec::new();
            {
                let positions = &self.positions;
                let inv_mass = &self.inv_mass;
                let triangles = &self.triangles;
                let start = &self.substep_start;
                let hash = &hash;

                // 頂点ごとに独立(読むだけ)なので並列にできる。通常の
                // 近傍列挙と同じく、区切り方を自分で決めて順序を固定する。
                let swept_for = |i: usize, out: &mut Vec<(u32, Vec3)>| {
                    if inv_mass[i] == 0.0 {
                        return;
                    }
                    let p0 = start[i];
                    let p1 = positions[i];
                    let motion = p1.sub(p0).length();
                    // 厚みの範囲しか動いていないなら位置だけの判定で足りる
                    if motion <= thickness {
                        return;
                    }
                    // 経路上を厚み間隔で拾う。セルの取りこぼしを防ぐため
                    let steps = ((motion / thickness).ceil() as usize)
                        .clamp(1, MAX_SWEPT_SAMPLES);
                    let mut best: Option<(f64, usize)> = None;
                    for sstep in 0..=steps {
                        let sp = p0.add(p1.sub(p0).scale(sstep as f64 / steps as f64));
                        hash.for_each_triangle(sp, |ti| {
                            let t = triangles[ti];
                            if t[0] == i || t[1] == i || t[2] == i {
                                return;
                            }
                            if let Some(tt) = crate::collision::segment_triangle_hit(
                                p0,
                                p1,
                                positions[t[0]],
                                positions[t[1]],
                                positions[t[2]],
                            ) {
                                // 最初に当たった面を採る
                                if best.map_or(true, |(bt, _)| tt < bt) {
                                    best = Some((tt, ti));
                                }
                            }
                        });
                    }

                    if let Some((tt, ti)) = best {
                        let t = triangles[ti];
                        let (a, b, c) =
                            (positions[t[0]], positions[t[1]], positions[t[2]]);
                        let hit = p0.add(p1.sub(p0).scale(tt));
                        if let Some(nrm) = b.sub(a).cross(c.sub(a)).normalized() {
                            // 入ってきた側へ厚みぶん戻す
                            let side = if p0.sub(hit).dot(nrm) >= 0.0 { 1.0 } else { -1.0 };
                            out.push((i as u32, hit.add(nrm.scale(side * thickness))));
                        }
                    }
                };

                if n >= PARALLEL_MIN_VERTICES {
                    let starts: Vec<usize> = (0..n).step_by(SELF_COLLISION_CHUNK).collect();
                    let chunks: Vec<Vec<(u32, Vec3)>> = starts
                        .into_par_iter()
                        .map(|st| {
                            let end = (st + SELF_COLLISION_CHUNK).min(n);
                            let mut out = Vec::new();
                            for i in st..end {
                                swept_for(i, &mut out);
                            }
                            out
                        })
                        .collect();
                    for chunk in chunks {
                        fixes.extend(chunk);
                    }
                } else {
                    for i in 0..n {
                        swept_for(i, &mut fixes);
                    }
                }
            }
            self.timings.self_tunneling = fixes.len() as f64;
            for (i, pos) in fixes {
                self.positions[i as usize] = pos;
            }
        }

        let positions = &self.positions;
        let inv_mass = &self.inv_mass;
        let triangles = &self.triangles;

        let collect_for = |i: usize, out: &mut Vec<(u32, u32)>| {
            if inv_mass[i] == 0.0 {
                return;
            }
            let p = positions[i];
            hash.for_each_triangle(p, |ti| {
                let t = triangles[ti];
                // 自分を含む面は対象外
                if t[0] == i || t[1] == i || t[2] == i {
                    return;
                }
                if inv_mass[i] + inv_mass[t[0]] + inv_mass[t[1]] + inv_mass[t[2]] == 0.0 {
                    return;
                }
                let q = crate::collision::closest_point_on_triangle(
                    p,
                    positions[t[0]],
                    positions[t[1]],
                    positions[t[2]],
                );
                if p.sub(q).length() < thickness {
                    out.push((i as u32, ti as u32));
                }
            });
        };

        pairs.clear();
        if n >= PARALLEL_MIN_VERTICES {
            // 頂点同士のときと同じ理由で、区切り方を自分で決めて順序を固定する
            let starts: Vec<usize> = (0..n).step_by(SELF_COLLISION_CHUNK).collect();
            let chunks: Vec<Vec<(u32, u32)>> = starts
                .into_par_iter()
                .map(|start| {
                    let end = (start + SELF_COLLISION_CHUNK).min(n);
                    let mut out = Vec::new();
                    for i in start..end {
                        collect_for(i, &mut out);
                    }
                    out
                })
                .collect();
            for chunk in chunks {
                pairs.extend(chunk);
            }
        } else {
            for i in 0..n {
                collect_for(i, &mut pairs);
            }
        }

        // --- 押し出し。互いに書き込むので逐次で解く ---
        for idx in 0..pairs.len() {
            let (vi, ti) = pairs[idx];
            let (vi, ti) = (vi as usize, ti as usize);
            let t = self.triangles[ti];

            let p = self.positions[vi];
            let (a, b, c) = (self.positions[t[0]], self.positions[t[1]], self.positions[t[2]]);
            let q = crate::collision::closest_point_on_triangle(p, a, b, c);

            let delta = p.sub(q);
            let dist = delta.length();
            if dist >= thickness {
                continue;
            }

            let (u, v, w) = crate::collision::barycentric_on_triangle(q, a, b, c);
            let wp = self.inv_mass[vi];
            let (wa, wb, wc) = (
                self.inv_mass[t[0]],
                self.inv_mass[t[1]],
                self.inv_mass[t[2]],
            );
            // 三角形側の実効的な逆質量。重心座標の重みで配分する
            let wt = u * u * wa + v * v * wb + w * w * wc;
            let denom = wp + wt;
            if denom <= 1e-12 {
                continue;
            }

            // 完全に面上にある場合は面法線へ逃がす
            let dir = match delta.normalized() {
                Some(d) => d,
                None => match b.sub(a).cross(c.sub(a)).normalized() {
                    Some(nrm) => nrm,
                    None => continue,
                },
            };

            let correction = (thickness - dist) / denom;
            self.positions[vi] = self.positions[vi].add(dir.scale(correction * wp));
            self.positions[t[0]] = self.positions[t[0]].sub(dir.scale(correction * u * wa));
            self.positions[t[1]] = self.positions[t[1]].sub(dir.scale(correction * v * wb));
            self.positions[t[2]] = self.positions[t[2]].sub(dir.scale(correction * w * wc));

            let back = dir.scale(-1.0);
            contacts.push((vi, dir, params.collision_friction));
            contacts.push((t[0], back, params.collision_friction));
            contacts.push((t[1], back, params.collision_friction));
            contacts.push((t[2], back, params.collision_friction));
        }

        self.tri_hash = hash;
        self.self_tri_pairs = pairs;
    }

    /// 位置を強制設定し、速度をリセットする(巻き戻し用)。
    /// 開始時点で食い込んでいる頂点を、**速度を発生させずに**押し離す。
    ///
    /// 速度はサブステップの中で「位置の差 ÷ dt」から作る。そのため、最初から
    /// 食い込んだ状態で走らせると、押し出した距離がそのまま速度になって布が
    /// 吹き飛ぶ(分離速度 = 食い込みの深さ ÷ サブステップの dt。厚み 0.02 に
    /// 0.016 食い込ませると 3.84 m/s)。パターンを重ねて置いてから縫う、
    /// 取り込んだ衣装が最初から交差している、といった場面で実際に起きる。
    ///
    /// ここでは `step` を通さずに位置だけを直すので、押し出しが速度に化けない。
    ///
    /// 分離速度そのものを一律に殺す手もあるが、それだと**動くコライダーが布を
    /// 押し出せなくなる**(アバターの動きに布が付いてこない)。食い込みは開始
    /// 時点に限った問題なので、開始前に片付ける形にしてある。
    ///
    /// 戻り値: 最後の反復で残っていた接触の数(0 なら解消しきった)
    pub fn untangle(&mut self, params: &SimParams, iterations: u32) -> usize {
        // 広域探索のキャッシュはフレーム先頭で作る前提なので、ここでは使わない
        let params = SimParams {
            cache_broadphase: false,
            ..*params
        };

        let mut remaining = 0;
        for _ in 0..iterations.max(1) {
            remaining = self.resolve_collisions(&params).len();
            if remaining == 0 {
                break;
            }
        }

        // 位置だけを動かした。速度は作らない
        self.velocities.iter_mut().for_each(|v| *v = Vec3::zero());
        remaining
    }

    pub fn set_positions(&mut self, positions: Vec<Vec3>) -> Result<(), String> {
        if positions.len() != self.positions.len() {
            return Err(format!(
                "positions length mismatch: expected {}, got {}",
                self.positions.len(),
                positions.len()
            ));
        }
        self.positions = positions;
        self.velocities.iter_mut().for_each(|v| *v = Vec3::zero());
        Ok(())
    }

    /// 全制約の平均相対誤差 |len - rest| / rest。品質チェック・回帰テスト用。
    pub fn average_stretch_error(&self) -> f64 {
        let mut sum = 0.0;
        let mut count = 0usize;
        for c in &self.stretch_constraints {
            if c.rest_length <= 1e-12 {
                continue;
            }
            let len = self.positions[c.i0].sub(self.positions[c.i1]).length();
            sum += (len - c.rest_length).abs() / c.rest_length;
            count += 1;
        }
        if count == 0 {
            0.0
        } else {
            sum / count as f64
        }
    }

    /// 全頂点座標が有限値かどうか(発散検知)。
    pub fn is_finite(&self) -> bool {
        self.positions
            .iter()
            .all(|p| p.x.is_finite() && p.y.is_finite() && p.z.is_finite())
    }
}

/// XPBD の距離制約ソルバー。λ を反復間で累積する。
fn solve_distance(
    positions: &mut [Vec3],
    inv_mass: &[f64],
    constraints: &[DistanceConstraint],
    lambdas: &mut [f64],
    inv_dt2: f64,
) {
    for (ci, c) in constraints.iter().enumerate() {
        let w0 = inv_mass[c.i0];
        let w1 = inv_mass[c.i1];
        let w_sum = w0 + w1;
        if w_sum == 0.0 {
            continue;
        }

        let delta = positions[c.i0].sub(positions[c.i1]);
        let dir = match delta.normalized() {
            Some(d) => d,
            None => continue,
        };
        let c_val = delta.length() - c.rest_length;

        let alpha_tilde = c.compliance * inv_dt2;
        let lambda = lambdas[ci];
        let d_lambda = (-c_val - alpha_tilde * lambda) / (w_sum + alpha_tilde);
        lambdas[ci] = lambda + d_lambda;

        positions[c.i0] = positions[c.i0].add(dir.scale(d_lambda * w0));
        positions[c.i1] = positions[c.i1].add(dir.scale(-d_lambda * w1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 生地を走らせたまま差し替えられること。
    ///
    /// density / compliance は組み立て時に焼き込まれるので、以前は
    /// 走行中に生地を選び直しても何も起きなかった。組み立て直すと布が
    /// 初期姿勢に戻ってしまうため、姿勢を保ったまま差し替える。
    #[test]
    fn density_and_compliance_can_change_while_running() {
        let (positions, edges, bending, tris, pinned) = build_grid(11, 11, 0.1);
        let mut sim =
            ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 0.0, 1e-4);
        let params = SimParams::default();
        for _ in 0..30 {
            sim.step(1.0 / 60.0, &params);
        }

        // 差し替えても姿勢はそのまま
        let before = sim.positions.clone();
        sim.set_density(0.8);
        sim.set_compliances(1e-3, 0.3);
        for (a, b) in sim.positions.iter().zip(before.iter()) {
            assert!(a.sub(*b).length() < 1e-12, "差し替えで位置が動いた");
        }

        // ピン留めは維持される(質量無限のまま)
        for &i in pinned.iter() {
            assert_eq!(sim.inv_mass[i], 0.0, "ピン留めが外れた");
        }
        // ピン以外は新しい密度を反映している
        let free = (0..sim.positions.len())
            .find(|i| !pinned.contains(i))
            .expect("自由な頂点が無い");
        let expect = 1.0 / (sim.vertex_area[free] * 0.8);
        assert!(
            (sim.inv_mass[free] - expect).abs() < 1e-9,
            "密度が反映されていない: {} vs {}",
            sim.inv_mass[free],
            expect
        );

        // コンプライアンスは全制約に行き渡る
        assert!(sim.stretch_constraints.iter().all(|c| c.compliance == 1e-3));
        assert!(sim.bending_constraints.iter().all(|c| c.compliance == 0.3));

        // 差し替えた後も発散しない
        for _ in 0..30 {
            sim.step(1.0 / 60.0, &params);
        }
        assert!(sim.is_finite(), "差し替え後に発散した");
    }

    /// 差し替えたコンプライアンスが、その後の運動に実際に効くこと。
    ///
    /// 「全制約に値が行き渡った」ことは上のテストで見ている。ここでは
    /// solver が毎ステップその値を読んでいる、という繋がりの方を確かめる。
    ///
    /// 硬さと垂れの向きを見るなら片持ち(bending_compliance_controls_cantilever_droop)
    /// が正しい測り方で、吊り下げでは逆転する。硬い布は平面を保って下まで
    /// 伸びるので、柔らかい布より低い位置に届く。ここでは向きを問わず、
    /// 同じ初期状態から分岐することだけを見る。
    #[test]
    fn swapped_compliance_changes_later_motion() {
        let run = |swap_to: Option<f64>| {
            let (positions, edges, quads, tris, pinned) = build_grid(9, 9, 0.1);
            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.3);
            let params = SimParams::default();
            for _ in 0..10 {
                sim.step(1.0 / 60.0, &params);
            }
            if let Some(bending) = swap_to {
                sim.set_compliances(0.0, bending);
            }
            for _ in 0..30 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.positions.clone()
        };

        let untouched = run(None);
        let swapped = run(Some(0.0));
        let moved = untouched
            .iter()
            .zip(swapped.iter())
            .map(|(a, b)| a.sub(*b).length())
            .fold(0.0_f64, f64::max);
        assert!(
            moved > 1e-4,
            "差し替えても運動が変わらない: 最大差 {moved:.2e} m"
        );

        // 同じ値に差し替えたときは、何もしなかった場合と一致する
        let same = run(Some(0.3));
        let drift = untouched
            .iter()
            .zip(same.iter())
            .map(|(a, b)| a.sub(*b).length())
            .fold(0.0_f64, f64::max);
        assert!(
            drift < 1e-12,
            "同じ値への差し替えで結果が動いた: {drift:.2e} m"
        );
    }

    /// 掃過判定が、位置だけでは抜けていた布どうしの高速な交差を止めること。
    ///
    /// 位置だけを見る判定は、1サブステップの移動量が厚みを超えると抜ける:
    ///
    ///     抜けない上限速度 = Self Thickness x fps x Substeps
    ///
    /// 厚み 0.016 / 60fps / substeps 4 なら 3.84 m/s。実測でも 3.13 m/s は
    /// 無事、4.43 m/s で 225頂点**全部**が下の布を素通りしていた。
    /// 掃過判定を入れると 12 m/s まで 0 になる。
    #[test]
    fn swept_test_stops_cloth_passing_through_cloth() {
        const N: usize = 15;
        const E: f64 = 0.04;
        const TH: f64 = 0.016;
        const G: f64 = 9.81;

        // 下の布(固定) + 上の布(半セルずらして落とす)
        let sheet = |z: f64, shift: f64| {
            let mut pos = Vec::new();
            for y in 0..N {
                for x in 0..N {
                    pos.push(Vec3::new(x as f64 * E + shift, y as f64 * E + shift, z));
                }
            }
            let mut edges = Vec::new();
            let mut tris = Vec::new();
            for y in 0..N {
                for x in 0..N {
                    let i = y * N + x;
                    if x + 1 < N {
                        edges.push((i, i + 1));
                    }
                    if y + 1 < N {
                        edges.push((i, i + N));
                    }
                    if x + 1 < N && y + 1 < N {
                        tris.push((i, i + 1, i + N + 1));
                        tris.push((i, i + N + 1, i + N));
                    }
                }
            }
            (pos, edges, tris)
        };

        let through = |speed: f64, swept: bool| -> usize {
            let height = speed * speed / (2.0 * G);
            let (lower_pos, lower_edges, lower_tris) = sheet(0.0, 0.0);
            let (upper_pos, upper_edges, upper_tris) = sheet(height, E * 0.5);
            let off = lower_pos.len();

            let mut positions = lower_pos;
            positions.extend(upper_pos);
            let mut edges = lower_edges;
            edges.extend(upper_edges.iter().map(|&(a, b)| (a + off, b + off)));
            let mut tris = lower_tris;
            tris.extend(
                upper_tris
                    .iter()
                    .map(|&(a, b, c)| (a + off, b + off, c + off)),
            );
            let pinned: Vec<usize> = (0..off).collect();
            let quads = crate::bending::quads_from_triangles(&tris);

            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.01);
            let params = SimParams {
                iterations: 10,
                substeps: 4,
                damping: 0.0,
                collision_enabled: false,
                self_collision_enabled: true,
                self_collision_thickness: TH,
                continuous_self_collision: swept,
                ..Default::default()
            };
            let frames = (speed / G * 60.0) as usize + 25;
            for _ in 0..frames {
                sim.step(1.0 / 60.0, &params);
            }
            assert!(sim.is_finite(), "発散した (速度 {speed}, 掃過 {swept})");
            (off..sim.positions.len())
                .filter(|&i| sim.positions[i].z < -TH)
                .count()
        };

        let total = N * N;

        // 予測上限(3.84 m/s)より下なら、掃過判定が無くても抜けない
        assert_eq!(through(3.13, false), 0, "遅くても抜けた。前提が崩れている");

        // 上限を超えると、位置だけの判定では全部抜ける
        let naive = through(4.43, false);
        assert_eq!(naive, total, "素通りが再現できていない: {naive}/{total}");

        // 掃過判定を入れると止まる
        for speed in [4.43, 6.26, 12.0] {
            let swept = through(speed, true);
            assert_eq!(swept, 0, "掃過判定でも抜けた: {speed} m/s で {swept}/{total}");
        }
    }

    /// 自己衝突が「離す」ついでに布を膨らませないこと。
    ///
    /// **既存の自己衝突テストはこの向きを見ていない。** どれも「離れたか」と
    /// `is_finite()` しか確かめないので、押しすぎて布が伸びても通ってしまう。
    /// 加速の件(`CHEBYSHEV_RESTART`)と同じ形の穴。
    ///
    /// 厚みが辺長に近づくと、制約で結ばれていない頂点対(2つ隣)が押し合って
    /// 布が自分で膨らむ。0.30m 角・辺長 0.01m を床に堆積させて測ると:
    ///
    /// | 厚み/辺長 | 最長辺/静止長 | 広がり |
    /// |---|---|---|
    /// | 0.2〜0.7 | 1.000 | 0.300m(変化なし) |
    /// | 0.8      | 1.000 | 0.341m |
    /// | 1.0      | 1.053 | 0.370m |
    /// | 2.0      | 2.677 | 0.418m |
    /// | 4.0      | 8.107 | 0.333m |
    ///
    /// アドオンは `suggest_thickness` で辺長の 0.4倍を薦め、0.6倍を超えると
    /// 警告する(`check_thickness`)。この測定はその閾値に余裕があることの裏付け。
    /// ここでは薦めている 0.4倍で膨らまないことを固定する。
    #[test]
    fn self_collision_does_not_inflate_cloth_at_recommended_thickness() {
        let (nx, ny, edge) = (25usize, 25usize, 0.01);
        let (positions, edges, quads, tris, _) = build_grid(nx, ny, edge);
        let rest_span = (nx - 1) as f64 * edge;

        let mut sim = ClothSim::new(positions, &edges, &quads, &tris, &[], 0.2, 0.0, 0.01);
        let params = SimParams {
            iterations: 10,
            substeps: 8,
            damping: 0.02,
            floor_enabled: true,
            floor_z: -0.05,
            collision_enabled: false,
            self_collision_enabled: true,
            // suggest_thickness が薦める値 (辺長 x 0.4)
            self_collision_thickness: edge * 0.4,
            ..Default::default()
        };
        for _ in 0..150 {
            sim.step(1.0 / 60.0, &params);
        }
        assert!(sim.is_finite(), "発散した");

        // 最も伸びた辺。静止長を大きく超えていたら押し合っている
        let longest = edges
            .iter()
            .map(|&(a, b)| sim.positions[a].sub(sim.positions[b]).length())
            .fold(0.0_f64, f64::max);
        assert!(
            longest < edge * 1.15,
            "自己衝突が辺を引き伸ばした: 最長 {:.5}m (静止長 {:.5}m)",
            longest,
            edge
        );

        // 平面内の広がり。膨らむと静止時より横に広がる
        let width = {
            let xs: Vec<f64> = sim.positions.iter().map(|p| p.x).collect();
            let ys: Vec<f64> = sim.positions.iter().map(|p| p.y).collect();
            let sx = xs.iter().cloned().fold(f64::MIN, f64::max)
                - xs.iter().cloned().fold(f64::MAX, f64::min);
            let sy = ys.iter().cloned().fold(f64::MIN, f64::max)
                - ys.iter().cloned().fold(f64::MAX, f64::min);
            sx.max(sy)
        };
        assert!(
            width < rest_span * 1.1,
            "自己衝突で布が膨らんだ: 広がり {width:.4}m (静止時 {rest_span:.4}m)"
        );

        assert!(
            sim.average_stretch_error() < 0.01,
            "伸びが壊れた: {:.5}",
            sim.average_stretch_error()
        );
    }

    /// Chebyshev 加速が、反復数を上げても布を吹き飛ばさないこと。
    ///
    /// **この穴で一度出荷している。** 以前は垂れ比(曲げ)だけを見て安全だと
    /// 判断していたが、伸びは見ていなかった。伸び制約は曲げよりはるかに硬いので
    /// 曲げに合う ρ でも伸びには強すぎる。リスタート導入前は 0.4m 角の布が
    /// 反復40・ρ0.98 で 12m まで広がっていた。
    ///
    /// `is_finite()` は true のままなので、発散判定では捕まらない。
    /// 大きさと伸び誤差の両方で縛る。
    #[test]
    fn chebyshev_does_not_blow_up_at_high_iteration_counts() {
        let (nx, ny, edge) = (31usize, 31usize, 0.01);
        let span_rest = (nx - 1) as f64 * edge; // 0.30m

        let measure = |iterations: u32, substeps: u32, rho: f64| -> (f64, f64) {
            let (positions, edges, quads, tris, _) = build_grid(nx, ny, edge);
            // 上辺一列を固定して自重で垂らす。伸びに最も負荷がかかる形
            let pinned: Vec<usize> = (0..nx).map(|x| (ny - 1) * nx + x).collect();
            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.0);
            let params = SimParams {
                iterations,
                substeps,
                damping: 0.01,
                collision_enabled: false,
                chebyshev_radius: rho,
                ..Default::default()
            };
            for _ in 0..60 {
                sim.step(1.0 / 60.0, &params);
            }
            let span = sim
                .positions
                .iter()
                .map(|p| p.x.abs().max(p.y.abs()).max(p.z.abs()))
                .fold(0.0_f64, f64::max);
            (sim.average_stretch_error(), span)
        };

        for &substeps in &[4u32, 8, 32] {
            for &iterations in &[10u32, 20, 40] {
                for &rho in &[0.95_f64, 0.98] {
                    let (err, span) = measure(iterations, substeps, rho);
                    assert!(
                        span < span_rest * 3.0,
                        "布が吹き飛んだ: 反復{iterations} substeps{substeps} ρ{rho}                          で広がり {span:.3}m (静止時 {span_rest:.2}m)"
                    );
                    assert!(
                        err < 0.05,
                        "伸びが壊れた: 反復{iterations} substeps{substeps} ρ{rho}                          で平均伸び誤差 {err:.5}"
                    );
                }
            }
        }
    }

    /// 加速を強めるほど、また反復を増やすほど、伸び誤差は改善するはず。
    ///
    /// リスタート導入前はここが逆転していた(反復20以降で悪化)。
    #[test]
    fn chebyshev_improves_monotonically_with_iterations() {
        let (nx, ny, edge) = (31usize, 31usize, 0.01);
        let err = |iterations: u32, rho: f64| -> f64 {
            let (positions, edges, quads, tris, _) = build_grid(nx, ny, edge);
            let pinned: Vec<usize> = (0..nx).map(|x| (ny - 1) * nx + x).collect();
            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.0);
            let params = SimParams {
                iterations,
                substeps: 8,
                damping: 0.01,
                collision_enabled: false,
                chebyshev_radius: rho,
                ..Default::default()
            };
            for _ in 0..60 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.average_stretch_error()
        };

        let e20 = err(20, 0.98);
        let e40 = err(40, 0.98);
        assert!(
            e40 <= e20,
            "反復を増やしたのに悪化した: 20回 {e20:.5} -> 40回 {e40:.5}"
        );

        let plain = err(40, 0.0);
        let boosted = err(40, 0.98);
        assert!(
            boosted <= plain,
            "加速したのに悪化した: 加速なし {plain:.5} -> ρ0.98 {boosted:.5}"
        );
    }

    /// 曲げのコンプライアンスが剛性として機能するかを、カンチレバー法で確かめる。
    ///
    /// 短冊の片端を固定して水平に突き出し、自重でどれだけ垂れるかを見る。
    /// 繊維の分野で曲げ剛性を測る標準的な方法。
    ///
    /// **solver の設定について**: 曲げ剛性は PBD の反復数に依存する。
    /// 同じ compliance=0(完全剛体)でも、突き出し長に対する垂れ比は
    ///   iter 20 / subs 8  -> 0.94   (ほぼ垂れる)
    ///   iter 200 / subs 8 -> 0.76
    ///   iter 20 / subs 32 -> 0.65
    ///   iter 200 / subs 32 -> 0.11  (ほぼ真っ直ぐ)
    /// と変わる。硬い生地を表現したいなら Substeps を上げる必要がある。
    /// ここでは差が見える 20 x 32 で確かめる。
    #[test]
    fn bending_compliance_controls_cantilever_droop() {
        let (nx, ny, edge) = (51usize, 9usize, 0.005);
        let clamp_cols = 7usize;
        let overhang = (nx - clamp_cols) as f64 * edge;

        let droop = |compliance: f64| -> Option<f64> {
            let (positions, edges, quads, tris, _) = build_grid(nx, ny, edge);
            let idx = |x: usize, y: usize| y * nx + x;
            let pinned: Vec<usize> = (0..ny)
                .flat_map(|y| (0..clamp_cols).map(move |x| idx(x, y)))
                .collect();
            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.15, 0.0, compliance);
            let params = SimParams {
                iterations: 20,
                substeps: 32,
                damping: 0.6,
                collision_enabled: false,
                ..Default::default()
            };
            for _ in 0..400 {
                sim.step(1.0 / 60.0, &params);
            }
            if !sim.is_finite() || sim.average_stretch_error() > 0.01 {
                return None;
            }
            let tip: f64 =
                (0..ny).map(|y| sim.positions[idx(nx - 1, y)].z).sum::<f64>() / ny as f64;
            Some(-tip / overhang)
        };

        let stiff = droop(0.0).expect("硬い設定で発散した");
        let medium = droop(1e-2).expect("中間の設定で発散した");
        let soft = droop(10.0).expect("柔らかい設定で発散した");

        assert!(
            stiff < 0.8,
            "完全剛体なのに垂れすぎ: 突き出し長の {:.0}%",
            stiff * 100.0
        );
        assert!(
            soft > stiff + 0.15 && medium > stiff,
            "compliance が曲げ剛性として効いていない: 硬い {stiff:.3} / 中 {medium:.3} / 柔 {soft:.3}"
        );
    }

    /// 制約なしの単一頂点は自由落下する: z = -0.5*g*t^2
    #[test]
    fn free_fall_matches_analytic_solution() {
        let mut sim = ClothSim::new(
            vec![Vec3::zero()],
            &[],
            &[],
            &[],
            &[],
            0.0,
            0.0,
            0.0,
        );
        let params = SimParams {
            damping: 0.0,
            substeps: 1,
            ..SimParams::default()
        };

        let dt = 1.0 / 240.0;
        let steps = 240; // 1秒
        for _ in 0..steps {
            sim.step(dt, &params);
        }

        let expected = -0.5 * 9.81 * 1.0;
        let actual = sim.positions[0].z;
        // semi-implicit Euler の誤差は O(dt) なので数%以内に収まればよい
        assert!(
            (actual - expected).abs() < 0.05,
            "free fall z = {actual}, expected ~{expected}"
        );
    }

    /// ピン留めされた頂点は永久に動かない
    #[test]
    fn pinned_vertex_never_moves() {
        let mut sim = ClothSim::new(
            vec![Vec3::new(1.0, 2.0, 3.0), Vec3::new(1.1, 2.0, 3.0)],
            &[(0, 1)],
            &[],
            &[],
            &[0],
            0.0,
            0.0,
            0.0,
        );
        let params = SimParams::default();
        for _ in 0..120 {
            sim.step(1.0 / 60.0, &params);
        }
        assert_eq!(sim.positions[0], Vec3::new(1.0, 2.0, 3.0));
        assert!(sim.positions[1].z < 3.0, "自由端は落ちるはず");
    }

    /// compliance = 0 の距離制約は rest_length をほぼ厳密に保つ(=伸びない布)
    #[test]
    fn rigid_distance_constraint_preserves_length() {
        let rest = 0.1;
        let mut sim = ClothSim::new(
            vec![Vec3::zero(), Vec3::new(rest, 0.0, 0.0)],
            &[(0, 1)],
            &[],
            &[],
            &[0],
            0.0,
            0.0,
            0.0,
        );
        let params = SimParams::default();
        for _ in 0..300 {
            sim.step(1.0 / 60.0, &params);
        }
        let len = sim.positions[0].sub(sim.positions[1]).length();
        assert!(
            (len - rest).abs() / rest < 0.01,
            "length drifted to {len} (rest {rest})"
        );
    }

    /// 布グリッドを吊るしても発散せず、伸び誤差が小さいままであること
    #[test]
    fn hanging_grid_stays_stable() {
        let (positions, edges, bending, tris, pinned) = build_grid(11, 11, 0.1);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 0.0, 1e-4);
        let params = SimParams::default();

        for _ in 0..180 {
            sim.step(1.0 / 60.0, &params);
        }

        assert!(sim.is_finite(), "シミュレーションが発散した");
        let err = sim.average_stretch_error();
        assert!(err < 0.05, "平均伸び誤差が大きすぎる: {err}");
    }

    /// 縫製制約: closure を 1 まで上げると 2 頂点が引き寄せられる(M3の基礎)
    #[test]
    fn seam_closure_pulls_pieces_together() {
        let positions = vec![
            Vec3::new(-0.5, 0.0, 0.0),
            Vec3::new(-0.4, 0.0, 0.0),
            Vec3::new(0.4, 0.0, 0.0),
            Vec3::new(0.5, 0.0, 0.0),
        ];
        let mut sim = ClothSim::new(
            positions,
            &[(0, 1), (2, 3)],
            &[],
            &[],
            &[],
            0.0,
            0.0,
            0.0,
        );
        sim.set_seams(&[(1, 2)], 0.0);

        let params = SimParams {
            gravity: Vec3::zero(),
            damping: 0.1,
            ..SimParams::default()
        };

        let initial_gap = sim.positions[1].sub(sim.positions[2]).length();
        for i in 0..120 {
            sim.set_seam_closure(i as f64 / 119.0);
            sim.step(1.0 / 60.0, &params);
        }
        let final_gap = sim.positions[1].sub(sim.positions[2]).length();

        assert!(
            final_gap < initial_gap * 0.1,
            "縫い目が閉じていない: {initial_gap} -> {final_gap}"
        );
        assert!(sim.is_finite());
    }

    /// 胴(円柱)の前後に布を置いて両脇を縫う。戻り値の縫い目はピン留めした
    /// 上端付近を含まない(前後 32cm 離して固定しているので、閉じようがない)。
    fn build_sewn_tube(
        n: usize,
        spacing: f64,
        radius: f64,
    ) -> (ClothSim, Vec<(usize, usize)>) {
        let half = spacing * (n - 1) as f64 / 2.0;
        let mut positions = Vec::new();
        let (mut edges, mut tris) = (Vec::new(), Vec::new());
        for (panel, y) in [(0usize, -0.16), (1, 0.16)] {
            let base = panel * n * n;
            let idx = |x: usize, z: usize| base + z * n + x;
            for z in 0..n {
                for x in 0..n {
                    positions.push(Vec3::new(-half + x as f64 * spacing, y, z as f64 * spacing));
                }
            }
            for z in 0..n {
                for x in 0..n {
                    if x + 1 < n {
                        edges.push((idx(x, z), idx(x + 1, z)));
                    }
                    if z + 1 < n {
                        edges.push((idx(x, z), idx(x, z + 1)));
                    }
                    if x + 1 < n && z + 1 < n {
                        tris.push((idx(x, z), idx(x + 1, z), idx(x + 1, z + 1)));
                        tris.push((idx(x, z), idx(x + 1, z + 1), idx(x, z + 1)));
                    }
                }
            }
        }
        let pinned: Vec<usize> = (0..n)
            .flat_map(|x| [(n - 1) * n + x, n * n + (n - 1) * n + x])
            .collect();
        let seams: Vec<(usize, usize)> = (0..n - 5)
            .flat_map(|z| [0, n - 1].map(|x| (z * n + x, n * n + z * n + x)))
            .collect();

        let quads = crate::bending::quads_from_triangles(&tris);
        let mut sim = ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.15, 0.0, 2e-2);
        sim.set_seams(&seams, 0.0);

        let seg = 32;
        let mut cv = Vec::new();
        for k in 0..seg {
            let a = k as f64 / seg as f64 * std::f64::consts::TAU;
            cv.push(Vec3::new(radius * a.cos(), radius * a.sin(), -0.3));
            cv.push(Vec3::new(radius * a.cos(), radius * a.sin(), 0.7));
        }
        let mut ct = Vec::new();
        for k in 0..seg {
            let (a0, a1) = (2 * k, 2 * k + 1);
            let (b0, b1) = (2 * ((k + 1) % seg), 2 * ((k + 1) % seg) + 1);
            ct.push([a0, b0, b1]);
            ct.push([a0, b1, a1]);
        }
        sim.add_collider(cv, ct);
        (sim, seams)
    }

    /// 衝突の後で縫い目が開き直らない。
    ///
    /// 以前は衝突後の緩和で伸びだけを解いていたため、辺の長さを戻す力で
    /// 縫い目が引き離され、自己衝突 ON で平均 13.8mm(最大 60mm)の隙間が
    /// 残っていた(40cm 四方 × 2枚、辺長 2cm、胴の半径 10cm)。
    /// 先行事例 Taremin Cloth が同じ現象を約 10mm と報告している。
    #[test]
    fn seams_stay_closed_after_collisions() {
        let (mut sim, seams) = build_sewn_tube(21, 0.02, 0.10);
        let params = SimParams {
            iterations: 10,
            substeps: 8,
            chebyshev_radius: 0.98,
            damping: 0.05,
            self_collision_enabled: true,
            self_collision_thickness: 0.008,
            collision_thickness: 0.005,
            ..SimParams::default()
        };
        for f in 0..96 {
            sim.set_seam_closure((f as f64 / 47.0).min(1.0));
            sim.step(1.0 / 24.0, &params);
        }
        assert!(sim.is_finite());
        let worst = seams
            .iter()
            .map(|&(a, b)| sim.positions[a].sub(sim.positions[b]).length())
            .fold(0.0, f64::max);
        assert!(worst < 0.001, "縫い目が開いている: 最大 {:.2}mm", worst * 1e3);
    }

    /// 縫い目の頂点対は自己衝突の対象にならない(閉じると距離 0 になるため)。
    #[test]
    fn seam_pairs_are_excluded_from_self_collision() {
        let build = || {
            let positions = vec![
                Vec3::new(0.0, 0.0, 0.0),
                Vec3::new(0.1, 0.0, 0.0),
                Vec3::new(0.001, 0.0, 0.0),
                Vec3::new(0.101, 0.0, 0.0),
            ];
            ClothSim::new(positions, &[(0, 1), (2, 3)], &[], &[], &[], 0.0, 0.0, 0.0)
        };
        let params = SimParams {
            gravity: Vec3::zero(),
            self_collision_enabled: true,
            self_collision_thickness: 0.01,
            post_collision_iterations: 0,
            ..SimParams::default()
        };

        // 縫い目なし: 1mm しか離れていない 0-2 は押し離される
        let mut free = build();
        free.step(1.0 / 60.0, &params);
        assert!(free.positions[0].sub(free.positions[2]).length() > 0.002);

        // 縫い目あり(closure 0 = いまの距離を保つ): 押し離されない
        let mut sim = build();
        sim.set_seams(&[(0, 2), (1, 3)], 0.0);
        sim.step(1.0 / 60.0, &params);
        let gap = sim.positions[0].sub(sim.positions[2]).length();
        assert!(gap < 0.0015, "縫い目の頂点が自己衝突で押し離された: {gap}");
    }

    /// 着せた姿勢から始め直しても、型紙の寸法が変わらない(M7)。
    ///
    /// 型紙の形で組み立ててから `set_positions` で姿勢を置けば、伸び・曲げ・
    /// 質量の基準は型紙のまま、位置だけが着せた姿勢になる。
    /// 以前のように「今の形を元の形にし直す」と、収束しきらない伸び(0.3%)が
    /// し直すたびに寸法へ焼き込まれ、6回で 1.8% 大きくなっていた。
    #[test]
    fn restarting_from_dressed_pose_keeps_pattern_size() {
        let n = 31;
        let (pattern, edges, bending, tris, _) = build_grid(n, n, 0.02);
        let pinned: Vec<usize> = (0..n).map(|x| (n - 1) * n + x).collect();
        let total = |p: &[Vec3]| -> f64 {
            edges.iter().map(|&(a, b)| p[a].sub(p[b]).length()).sum()
        };
        let pattern_total = total(&pattern);
        let params = SimParams {
            // 格子は XY 平面なので、上端で吊るすように y 方向へ落とす
            gravity: Vec3::new(0.0, -9.81, 0.0),
            iterations: 10,
            substeps: 8,
            chebyshev_radius: 0.98,
            damping: 0.05,
            ..SimParams::default()
        };
        let build = || ClothSim::new(pattern.clone(), &edges, &bending, &tris, &pinned, 0.15, 0.0, 2e-2);

        let mut pose = pattern.clone();
        let mut ratios = Vec::new();
        for _ in 0..6 {
            let mut sim = build();
            sim.set_positions(pose.clone()).unwrap();
            for _ in 0..96 {
                sim.step(1.0 / 24.0, &params);
            }
            pose = sim.positions.clone();
            ratios.push(total(&pose) / pattern_total);
        }
        assert!(ratios[0] > 1.0005, "伸びが出ていない(計測の前提が崩れた): {ratios:?}");
        let drift = ratios.last().unwrap() - ratios[0];
        assert!(
            drift.abs() < 0.0005,
            "着せ直すたびに寸法が変わっている: {ratios:?}"
        );
    }

    /// つまむ制約はばね定数 1/α のばねとして釣り合い、反復数に依存しない。
    ///
    /// 質量 1 の1点を重力下で柔らかくつまむと、目標から m·g·α だけ下がって
    /// 止まるはず。反復ごとに目標へ寄せる方式だと反復数で効きが変わる。
    #[test]
    fn soft_grab_settles_at_spring_offset_regardless_of_iterations() {
        let alpha = 1e-3;
        let expected = 9.81 * alpha;
        for iterations in [1u32, 10, 40] {
            let mut sim = ClothSim::new(vec![Vec3::zero()], &[], &[], &[], &[], 0.0, 0.0, 0.0);
            sim.set_grab(0, Vec3::zero(), alpha).unwrap();
            let params = SimParams {
                iterations,
                damping: 0.9,
                ..SimParams::default()
            };
            for _ in 0..600 {
                sim.step(1.0 / 60.0, &params);
            }
            let sag = -sim.positions[0].z;
            assert!(
                (sag - expected).abs() < expected * 0.02,
                "iterations {iterations}: 下がり {sag:.5} / 期待 {expected:.5}"
            );
        }
    }

    /// 硬くつまむと頂点は目標へ行き、周りも付いてくる。離せば元どおり落ちる。
    #[test]
    fn hard_grab_moves_vertex_and_neighbors_then_releases() {
        let (positions, edges, bending, tris, pinned) = build_grid(9, 9, 0.05);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 0.0, 1e-3);
        let params = SimParams::default();
        let corner = 0; // ピン留めした上端の反対側の角
        let start = sim.positions[corner];
        // 上端のピンから布の長さ(0.4m)以内に置く。届かない位置だと伸びとの綱引きになる
        let target = start.add(Vec3::new(0.0, 0.1, 0.1));
        sim.set_grab(corner, target, 0.0).unwrap();
        assert_eq!(sim.grabbed_vertex(), Some(corner));
        for _ in 0..60 {
            sim.step(1.0 / 60.0, &params);
        }
        let off = sim.positions[corner].sub(target).length();
        assert!(off < 0.005, "目標へ行っていない: {off:.4}m");
        assert!(sim.positions[1].z > 0.05, "隣の頂点が付いてこない: {}", sim.positions[1].z);
        assert!(sim.average_stretch_error() < 0.05);

        sim.clear_grab();
        for _ in 0..60 {
            sim.step(1.0 / 60.0, &params);
        }
        assert!(sim.positions[corner].z < target.z - 0.05, "離しても落ちない");
    }

    /// ピン留めした頂点はつまんでも動かない。範囲外の番号は拒否する。
    #[test]
    fn grab_ignores_pinned_vertices_and_rejects_bad_index() {
        let (positions, edges, bending, tris, pinned) = build_grid(5, 5, 0.1);
        let pin = pinned[0];
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 0.0, 1e-3);
        let before = sim.positions[pin];
        sim.set_grab(pin, before.add(Vec3::new(0.0, 0.0, 1.0)), 0.0).unwrap();
        sim.step(1.0 / 60.0, &SimParams::default());
        assert!(sim.positions[pin].sub(before).length() < 1e-12);
        assert!(sim.set_grab(999, Vec3::zero(), 0.0).is_err());
    }

    /// 床面衝突: 布が床を突き抜けない
    #[test]
    fn floor_collision_stops_cloth() {
        let (positions, edges, bending, tris, _) = build_grid(6, 6, 0.1);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
        let params = SimParams {
            floor_enabled: true,
            floor_z: -1.0,
            ..SimParams::default()
        };
        for _ in 0..240 {
            sim.step(1.0 / 60.0, &params);
        }
        let lowest = sim.positions.iter().map(|p| p.z).fold(f64::MAX, f64::min);
        assert!(lowest >= -1.0 - 1e-6, "床を突き抜けた: {lowest}");
    }

    /// 同じ風でも、密度の高い(重い)生地ほどはためきにくい
    #[test]
    fn wind_effect_scales_with_density() {
        let displacement = |density: f64| {
            let (positions, edges, bending, tris, pinned) = build_grid(9, 9, 0.1);
            let mut sim =
                ClothSim::new(positions, &edges, &bending, &tris, &pinned, density, 0.0, 1e-4);
            let params = SimParams {
                wind: Vec3::new(2.0, 0.0, 0.0),
                ..SimParams::default()
            };
            for _ in 0..120 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.positions.iter().map(|p| p.x).fold(f64::MIN, f64::max)
        };

        let light = displacement(0.05);
        let heavy = displacement(1.0);
        assert!(
            light > heavy + 1e-3,
            "軽い生地の方が風で流されるはず: light={light}, heavy={heavy}"
        );
    }

    /// 広域探索のキャッシュは、結果を変えずに衝突検出を減らす
    ///
    /// キャッシュはフレーム先頭で候補を作るので、フレーム途中で近づいた接触を
    /// 取りこぼす危険がある。候補を複数持ち、移動見込みに安全係数を掛けることで
    /// 結果が一致することを確認する。
    #[test]
    fn broadphase_cache_matches_direct_search() {
        let drape = |cache: bool| {
            let (positions, edges, bending, tris, _) = build_grid(21, 21, 0.05);
            let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
            let center = Vec3::new(0.5, 0.5, -0.35);
            let (sphere_pos, sphere_tris) = build_sphere(center, 0.3, 16, 12);
            sim.add_collider(sphere_pos, sphere_tris);
            let params = SimParams {
                collision_enabled: true,
                collision_thickness: 0.01,
                substeps: 8,
                iterations: 2,
                cache_broadphase: cache,
                ..Default::default()
            };
            let mut deepest = f64::INFINITY;
            for _ in 0..120 {
                sim.step(1.0 / 60.0, &params);
                for p in &sim.positions {
                    deepest = deepest.min(p.sub(center).length());
                }
            }
            (sim.average_stretch_error(), deepest, sim.positions.clone())
        };

        let (err_direct, deep_direct, pos_direct) = drape(false);
        let (err_cached, deep_cached, pos_cached) = drape(true);

        assert!(
            (err_direct - err_cached).abs() < 1e-6,
            "伸び誤差が食い違う: 直接 {err_direct} / キャッシュ {err_cached}"
        );
        assert!(
            deep_cached > deep_direct - 1e-3,
            "キャッシュのほうが食い込んでいる: 直接 {deep_direct} / キャッシュ {deep_cached}"
        );

        let max_diff = pos_direct
            .iter()
            .zip(pos_cached.iter())
            .map(|(a, b)| a.sub(*b).length())
            .fold(0.0_f64, f64::max);
        assert!(
            max_diff < 1e-6,
            "頂点位置が食い違う: 最大差 {max_diff} m"
        );
    }

    /// 自己衝突の並列列挙が決定的であること
    ///
    /// 近傍の列挙は並列に回すが、押し出しの順序(= Gauss-Seidel の順序)が
    /// 実行ごとに変わると結果がぶれる。ベイクとスクラブは決定性が前提なので、
    /// 区切り方を固定してある。それが効いているかを確かめる。
    #[test]
    fn parallel_self_collision_is_deterministic() {
        let side = 71; // 5,041頂点。PARALLEL_MIN_VERTICES と区切り幅の両方を超える
        assert!(side * side > PARALLEL_MIN_VERTICES);
        assert!(side * side > SELF_COLLISION_CHUNK * 2, "区切りが複数になる大きさで試す");

        let run = || {
            let (positions, edges, bending, tris, _) = build_grid(side, side, 0.02);
            let mut sim =
                ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 0.02);
            let params = SimParams {
                collision_enabled: false,
                self_collision_enabled: true,
                self_collision_thickness: 0.008,
                floor_enabled: true,
                floor_z: 0.0,
                wind: Vec3::new(0.4, 0.2, 0.0),
                ..Default::default()
            };
            for _ in 0..40 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.positions.clone()
        };

        let a = run();
        let b = run();
        let max_diff = a
            .iter()
            .zip(b.iter())
            .map(|(x, y)| x.sub(*y).length())
            .fold(0.0_f64, f64::max);
        assert_eq!(max_diff, 0.0, "自己衝突の並列化で結果がぶれている: 最大差 {max_diff}");
    }

    /// 並列化した衝突解決が決定的であること
    ///
    /// 頂点ごとに独立なので結果は変わらないはずだが、並列化は非決定性を
    /// 持ち込みやすい。ベイクとスクラブは決定性の上に成り立っているので、
    /// 並列の閾値を超える大きさで明示的に確かめる。
    #[test]
    fn parallel_collision_is_deterministic() {
        let side = 71; // 5,041頂点。PARALLEL_MIN_VERTICES(4,000)を超える
        assert!(side * side > PARALLEL_MIN_VERTICES, "閾値を超える大きさで試すこと");

        let run = || {
            let (positions, edges, bending, tris, _) = build_grid(side, side, 0.03);
            let mut sim =
                ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
            let center = Vec3::new(1.0, 1.0, -0.3);
            let (sphere_pos, sphere_tris) = build_sphere(center, 0.35, 24, 16);
            sim.add_collider(sphere_pos, sphere_tris);
            let params = SimParams {
                collision_enabled: true,
                collision_thickness: 0.01,
                ..Default::default()
            };
            for _ in 0..30 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.positions.clone()
        };

        let a = run();
        let b = run();
        let max_diff = a
            .iter()
            .zip(b.iter())
            .map(|(x, y)| x.sub(*y).length())
            .fold(0.0_f64, f64::max);
        assert_eq!(max_diff, 0.0, "並列化で結果がぶれている: 最大差 {max_diff}");
    }

    /// 衝突後の伸び補正は、貫通を増やさずに伸び誤差を減らす
    #[test]
    fn post_collision_pass_reduces_stretch_without_penetration() {
        let center = Vec3::new(0.5, 0.5, -0.35);
        let radius = 0.3;
        let floor_z = -0.8;

        let drape = |post: u32| {
            let (positions, edges, bending, tris, _) = build_grid(21, 21, 0.05);
            let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
            let (sphere_pos, sphere_tris) = build_sphere(center, radius, 16, 12);
            sim.add_collider(sphere_pos, sphere_tris);
            let params = SimParams {
                // 床が無いと布が球から滑り落ちて自由落下し、
                // 「遠いから貫通していない」という無意味な判定になる
                floor_enabled: true,
                floor_z,
                collision_enabled: true,
                collision_thickness: 0.01,
                self_collision_enabled: true,
                // 伸び補正は「厚みを大きく取ったとき」のための機能なので、
                // 効果が出る領域で測る。推奨の 0.4 x エッジ長(0.02)では
                // 押し出し自体が小さく、差が雑音に埋もれる
                self_collision_thickness: 0.05,
                post_collision_iterations: post,
                ..Default::default()
            };
            for _ in 0..220 {
                sim.step(1.0 / 60.0, &params);
            }
            let deepest = sim
                .positions
                .iter()
                .map(|p| p.sub(center).length())
                .fold(f64::INFINITY, f64::min);
            (sim.average_stretch_error(), deepest, sim.is_finite())
        };

        let (err_off, deepest_off, finite_off) = drape(0);
        let (err_on, deepest_on, finite_on) = drape(4);

        assert!(finite_off && finite_on, "発散した");
        assert!(
            err_on < err_off * 0.8,
            "伸び補正が効いていない: {err_off} -> {err_on}"
        );
        // 球に潜り込んでいないこと
        assert!(
            deepest_on > radius - 1e-3,
            "補正で貫通した: 中心からの最小距離 {deepest_on} (補正なしでは {deepest_off})"
        );
        // かつ、球の上に載っていること。飛んで行っても「遠いから貫通なし」に
        // なってしまうので、近いことも確かめる
        assert!(
            deepest_on < radius * 1.5,
            "布が球から離れている。試験が成立していない: {deepest_on}"
        );
    }

    /// 十分に減衰させた最終的なドレープ形状は substeps 数に依存しない
    /// (= compliance が実際の物性値として機能している)
    #[test]
    fn static_drape_is_substep_independent() {
        let drape = |substeps: u32| {
            let (positions, edges, bending, tris, pinned) = build_grid(9, 9, 0.1);
            let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 1e-6, 1e-4);
            let params = SimParams {
                damping: 0.9,
                substeps,
                ..SimParams::default()
            };
            for _ in 0..400 {
                sim.step(1.0 / 60.0, &params);
            }
            sim.positions.clone()
        };

        let a = drape(2);
        let b = drape(8);
        let max_diff = a
            .iter()
            .zip(b.iter())
            .map(|(p, q)| p.sub(*q).length())
            .fold(0.0, f64::max);
        assert!(
            max_diff < 0.01,
            "静止ドレープ形状が substeps に依存している: 最大差 {max_diff} m"
        );
    }

    /// 球コライダーの上に落とした布が球を貫通しない(M2 Exit条件の中核)
    #[test]
    fn cloth_drapes_over_sphere_without_penetration() {
        let (sphere_verts, sphere_tris) = build_sphere(Vec3::new(0.4, 0.4, -0.3), 0.25, 16, 12);

        let (positions, edges, bending, tris, _) = build_grid(15, 15, 0.06);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
        sim.add_collider(sphere_verts, sphere_tris);

        let thickness = 0.008;
        let params = SimParams {
            collision_enabled: true,
            collision_thickness: thickness,
            ..SimParams::default()
        };

        // 布は最終的に球から滑り落ちるのが正しい挙動なので、
        // 「一度でも接触したか」と「どの瞬間も貫通していないか」を毎フレーム検証する。
        let center = Vec3::new(0.4, 0.4, -0.3);
        let mut saw_contact = false;
        let mut worst_distance = f64::MAX;

        for frame in 0..240 {
            sim.step(1.0 / 60.0, &params);
            saw_contact |= sim.last_collision_count() > 0;

            for p in &sim.positions {
                let d = p.sub(center).length();
                worst_distance = worst_distance.min(d);
                assert!(
                    d > 0.25 - 0.01,
                    "frame {frame}: 球に食い込んだ (中心からの距離 {d}, 半径 0.25)"
                );
            }
        }

        assert!(sim.is_finite(), "発散した");
        assert!(saw_contact, "球に一度も接触していない");
        assert!(
            worst_distance < 0.25 + thickness + 0.03,
            "球の表面まで到達していない: 最小距離 {worst_distance}"
        );
    }

    /// コライダーを動かすと布が押される(refit が効いている)
    #[test]
    fn moving_collider_pushes_cloth() {
        let (verts, tris) = build_sphere(Vec3::new(0.5, 0.5, -1.0), 0.3, 12, 8);
        let (positions, edges, bending, grid_tris, pinned) = build_grid(11, 11, 0.1);
        let mut sim = ClothSim::new(positions, &edges, &bending, &grid_tris, &pinned, 0.2, 0.0, 1e-4);
        let collider = sim.add_collider(verts.clone(), tris);

        let params = SimParams {
            gravity: Vec3::zero(),
            collision_enabled: true,
            collision_thickness: 0.01,
            ..SimParams::default()
        };

        let before = sim.positions[5 * 11 + 5].z;

        // 球を布の下から突き上げる
        for k in 1..=60 {
            let dz = k as f64 * 0.015;
            let moved: Vec<Vec3> = verts.iter().map(|v| v.add(Vec3::new(0.0, 0.0, dz))).collect();
            sim.update_collider(collider, moved).unwrap();
            sim.step(1.0 / 60.0, &params);
        }

        let after = sim.positions[5 * 11 + 5].z;
        assert!(after > before + 0.05, "布が押し上げられていない: {before} -> {after}");
        assert!(sim.is_finite());

        // **押し上げすぎていないことも縛る。** 「動いたか」だけを見ると、
        // 球が布を吹き飛ばしても通ってしまう(Chebyshev の件と同じ片側の指標)。
        // 球の中心は -1.0 から 0.9 上がって -0.1、頂点は半径ぶん足して 0.2。
        // 布はその上に厚みぶん乗るので 0.21 前後に落ち着くはず。
        // 実測では布 0.242 / 頂点 0.210。差 0.032 が厚み 0.01 より大きいのは、
        // 球が 0.9 m/s で上がっていて布が慣性で行き過ぎるため。吹き飛びなら
        // 桁で外れるので、0.06 の余裕でも取り逃がさない。
        let apex = -1.0 + 60.0 * 0.015 + 0.3 + params.collision_thickness;
        assert!(
            after < apex + 0.06,
            "球より上へ押し出されている: 布 {after:.3} / 球の頂点 {apex:.3}"
        );

        // 押し上げのついでに布を引き伸ばしていないこと
        let stretch = sim.average_stretch_error();
        assert!(stretch < 0.01, "押し上げで布が伸びた: 平均伸び誤差 {stretch:.5}");
    }

    /// 自己衝突を有効にしても布が破裂しない
    #[test]
    fn self_collision_keeps_cloth_stable() {
        let (positions, edges, bending, tris, pinned) = build_grid(13, 13, 0.05);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.2, 0.0, 1e-4);

        let params = SimParams {
            self_collision_enabled: true,
            // 厚みはエッジ長(0.05)より十分小さくする
            self_collision_thickness: 0.02,
            ..SimParams::default()
        };

        for _ in 0..180 {
            sim.step(1.0 / 60.0, &params);
        }

        assert!(sim.is_finite(), "自己衝突で発散した");
        let err = sim.average_stretch_error();
        assert!(err < 0.05, "自己衝突で布が伸びすぎている: {err}");
    }

    /// 自己衝突が実際に頂点同士を押し離す
    #[test]
    /// 頂点の並びがずれた2枚が、互いを突き抜けないこと。
    ///
    /// 頂点同士の反発しか無かった頃は**素通りしていた**。格子間隔 h の2枚を
    /// 半セルずらすと、頂点間の面内距離が h√2/2 = 0.707h になる。推奨の厚み
    /// 0.4h はこれより小さいので、頂点同士は一度も反応しない。実測では上の層の
    /// 81頂点すべてが下の層を抜けて z = -53 まで落ちた。
    ///
    /// 実際に折り重なった布で頂点が揃うことはまずないので、ここが素通りすると
    /// 自己衝突は「効いているように見えて効いていない」状態になる。
    #[test]
    fn offset_layers_do_not_pass_through_each_other() {
        let n = 9usize;
        let h = 0.05;
        let thickness = h * 0.4; // 推奨どおり
        let gap = 0.06;

        let layer = |offset: f64, z: f64, base: usize| {
            let mut pos = Vec::new();
            let mut edges = Vec::new();
            let mut tris = Vec::new();
            for y in 0..n {
                for x in 0..n {
                    pos.push(Vec3::new(x as f64 * h + offset, y as f64 * h + offset, z));
                }
            }
            for y in 0..n {
                for x in 0..n {
                    let i = base + y * n + x;
                    if x + 1 < n {
                        edges.push((i, i + 1));
                    }
                    if y + 1 < n {
                        edges.push((i, i + n));
                    }
                    if x + 1 < n && y + 1 < n {
                        tris.push((i, i + 1, i + n + 1));
                        tris.push((i, i + n + 1, i + n));
                    }
                }
            }
            (pos, edges, tris)
        };

        // shift だけずらした2枚を落として、上が下を抜けるかを見る
        let run = |shift: f64| -> (usize, f64) {
            let (mut pos, mut edges, mut tris) = layer(0.0, 0.0, 0);
            let (p1, e1, t1) = layer(shift, gap, n * n);
            pos.extend(p1);
            edges.extend(e1);
            tris.extend(t1);
            let quads = crate::bending::quads_from_triangles(&tris);

            let half = n * n;
            // 下の層を固定し、上の層だけを落とす
            let pinned: Vec<usize> = (0..half).collect();
            let mut sim =
                ClothSim::new(pos, &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.02);
            let params = SimParams {
                substeps: 8,
                damping: 0.02,
                collision_enabled: false,
                self_collision_enabled: true,
                self_collision_thickness: thickness,
                ..SimParams::default()
            };
            sim.untangle(&params, 8);
            for _ in 0..200 {
                sim.step(1.0 / 60.0, &params);
            }
            assert!(sim.is_finite(), "発散した");

            let lowest = (0..half)
                .map(|i| sim.positions[i].z)
                .fold(f64::INFINITY, f64::min);
            let through = (half..2 * half)
                .filter(|&i| sim.positions[i].z < lowest - 1e-6)
                .count();
            let mean_upper =
                (half..2 * half).map(|i| sim.positions[i].z).sum::<f64>() / half as f64;
            (through, mean_upper)
        };

        let (aligned_through, aligned_z) = run(0.0);
        assert_eq!(aligned_through, 0, "頂点が揃っているのに抜けた");
        assert!(
            (aligned_z - thickness).abs() < thickness * 0.5,
            "厚みの位置で止まっていない: {aligned_z}"
        );

        let (offset_through, offset_z) = run(h / 2.0);
        assert_eq!(
            offset_through, 0,
            "半セルずらした層が突き抜けた: {offset_through} 頂点 (平均 z = {offset_z})"
        );
        assert!(
            (offset_z - thickness).abs() < thickness * 0.5,
            "厚みの位置で止まっていない: {offset_z}"
        );
    }

    /// 最初から食い込んでいる布が、開始と同時に吹き飛ばないこと。
    ///
    /// 速度は「位置の差 ÷ dt」から作るので、深い食い込みをそのまま押し出すと
    /// 分離がそのまま運動エネルギーになる(厚み 0.02 に 0.016 食い込むと
    /// 3.84 m/s、0.5秒で 1.9m 離れた)。`untangle` で開始前に位置だけを直す。
    #[test]
    fn untangle_resolves_initial_overlap_without_launching_cloth() {
        let thickness = 0.02;
        let gap = 0.004; // 厚みの 1/5。かなり深い食い込み

        // 2枚を gap だけ離して重ねる(互いに制約では繋がっていない)
        let (nx, ny, spacing) = (5usize, 5usize, 0.05);
        let mut positions = Vec::new();
        let mut edges = Vec::new();
        for layer in 0..2 {
            let base = layer * nx * ny;
            for y in 0..ny {
                for x in 0..nx {
                    positions.push(Vec3::new(
                        x as f64 * spacing,
                        y as f64 * spacing,
                        layer as f64 * gap,
                    ));
                }
            }
            for y in 0..ny {
                for x in 0..nx {
                    let i = base + y * nx + x;
                    if x + 1 < nx {
                        edges.push((i, i + 1));
                    }
                    if y + 1 < ny {
                        edges.push((i, i + nx));
                    }
                }
            }
        }

        let params = SimParams {
            gravity: Vec3::zero(),
            damping: 0.0,
            collision_enabled: false,
            self_collision_enabled: true,
            self_collision_thickness: thickness,
            ..SimParams::default()
        };

        let half = nx * ny;
        let separation = |sim: &ClothSim| -> f64 {
            sim.positions[0].sub(sim.positions[half]).length()
        };

        // --- untangle 無し: 押し出しが速度になって離れ続ける ---
        let mut runaway =
            ClothSim::new(positions.clone(), &edges, &[], &[], &[], 0.0, 0.0, 0.0);
        for _ in 0..30 {
            runaway.step(1.0 / 60.0, &params);
        }
        let flung = separation(&runaway);
        assert!(
            flung > thickness * 10.0,
            "吹き飛びが再現できていない。試験の前提が崩れている: {flung}"
        );

        // --- untangle 有り ---
        let mut fixed = ClothSim::new(positions, &edges, &[], &[], &[], 0.0, 0.0, 0.0);
        let remaining = fixed.untangle(&params, 8);
        assert_eq!(remaining, 0, "食い込みを解消しきれなかった");

        let after_untangle = separation(&fixed);
        assert!(
            after_untangle >= thickness,
            "厚みまで離れていない: {after_untangle}"
        );
        assert!(
            after_untangle < thickness * 1.5,
            "押し離しすぎ: {after_untangle}"
        );

        // 速度が残っていないので、回しても離れ続けない
        for _ in 0..30 {
            fixed.step(1.0 / 60.0, &params);
        }
        let settled = separation(&fixed);
        assert!(
            settled < thickness * 1.5,
            "untangle 後も離れ続けている: {after_untangle} -> {settled}"
        );
    }

    /// 食い込んでいない布に対して `untangle` は何もしないこと。
    ///
    /// 開始時に必ず通す処理なので、ここで位置が動くと**重なっていない既存の
    /// シーンの結果まで変わってしまう**。ベイクの互換性がこの性質に乗っている。
    #[test]
    fn untangle_does_nothing_when_nothing_overlaps() {
        let (positions, edges, quads, tris, pinned) = build_grid(11, 11, 0.1);
        let params = SimParams {
            collision_enabled: false,
            self_collision_enabled: true,
            // エッジ長 0.1 に対して十分小さい(斜めの 0.141 を超えない)
            self_collision_thickness: 0.03,
            ..SimParams::default()
        };

        let mut sim =
            ClothSim::new(positions.clone(), &edges, &quads, &tris, &pinned, 0.2, 0.0, 1e-4);
        let before = sim.positions.clone();
        let remaining = sim.untangle(&params, 8);

        assert_eq!(remaining, 0, "食い込んでいないのに接触が出た");
        for (a, b) in before.iter().zip(sim.positions.iter()) {
            assert_eq!(a, b, "untangle が位置を動かした");
        }
    }

    /// Chebyshev 加速が曲げの収束を速めること、かつ材質を変えないこと。
    ///
    /// 広い間隔の曲げ制約(梯子)はカンチレバーだけ見て良さそうに見えたが、
    /// ドレープで布が板になった。加速は「同じ解に速く近づける」手法なので、
    /// **硬い生地は硬く、柔らかい生地は柔らかいまま**でなければならない。
    /// 片側だけを見て判断しないよう、両方を確かめる。
    #[test]
    fn chebyshev_accelerates_without_changing_material() {
        let (nx, ny, edge, clamp) = (41usize, 5usize, 0.005, 5usize);
        let idx = |x: usize, y: usize| y * nx + x;
        let overhang = (nx - clamp) as f64 * edge;

        let droop = |compliance: f64, rho: f64| -> Option<f64> {
            let mut positions = Vec::new();
            for y in 0..ny {
                for x in 0..nx {
                    positions.push(Vec3::new(x as f64 * edge, y as f64 * edge, 0.0));
                }
            }
            let mut edges = Vec::new();
            let mut tris = Vec::new();
            for y in 0..ny {
                for x in 0..nx {
                    if x + 1 < nx {
                        edges.push((idx(x, y), idx(x + 1, y)));
                    }
                    if y + 1 < ny {
                        edges.push((idx(x, y), idx(x, y + 1)));
                    }
                    if x + 1 < nx && y + 1 < ny {
                        tris.push((idx(x, y), idx(x + 1, y), idx(x + 1, y + 1)));
                        tris.push((idx(x, y), idx(x + 1, y + 1), idx(x, y + 1)));
                    }
                }
            }
            let quads = crate::bending::quads_from_triangles(&tris);
            let pinned: Vec<usize> = (0..ny)
                .flat_map(|y| (0..clamp).map(move |x| idx(x, y)))
                .collect();
            let anchor = positions[pinned[0]];

            let mut sim =
                ClothSim::new(positions, &edges, &quads, &tris, &pinned, 0.15, 0.0, compliance);
            let params = SimParams {
                iterations: 10,
                // 加速は「サブステップを上げたとき」に特によく効く
                substeps: 32,
                damping: 0.6,
                collision_enabled: false,
                post_collision_iterations: 0,
                chebyshev_radius: rho,
                ..SimParams::default()
            };
            for _ in 0..200 {
                sim.step(1.0 / 60.0, &params);
            }
            if !sim.is_finite() {
                return None;
            }
            // 固定頂点が動いていないこと(加速の式が固定を壊さないか)
            assert_eq!(sim.positions[pinned[0]], anchor, "固定頂点が動いた");

            let tip: f64 = (0..ny).map(|y| sim.positions[idx(nx - 1, y)].z).sum::<f64>()
                / ny as f64;
            Some(-tip / overhang)
        };

        // 硬い生地(完全剛体): 加速すると目に見えて硬くなる
        let stiff_plain = droop(0.0, 0.0).expect("素の反復で発散した");
        let stiff_cheb = droop(0.0, 0.95).expect("加速して発散した");
        assert!(
            stiff_cheb < stiff_plain * 0.8,
            "加速しても硬さが出ていない: {stiff_plain:.4} -> {stiff_cheb:.4}"
        );

        // 柔らかい生地: 材質は変わらないので、ほとんど動かないこと。
        // ここが硬くなるようなら「加速」ではなく「剛性を足している」
        let soft_plain = droop(0.3, 0.0).expect("素の反復で発散した");
        let soft_cheb = droop(0.3, 0.95).expect("加速して発散した");
        assert!(
            (soft_cheb - soft_plain).abs() < 0.05,
            "柔らかい生地の垂れ方が変わった: {soft_plain:.4} -> {soft_cheb:.4}"
        );
    }

    /// Chebyshev 加速が、どの反復数でも布を反り返らせないこと。
    ///
    /// ω の列は最後まで回しきる前提のもので、途中で打ち切ると特定の長さで
    /// 誤差を増幅する。遅延が 2 だった頃は、ρ=0.95 かつ反復 5〜6 のときだけ
    /// **片持ちの先端が固定端より上に反り返った**(垂れ比 -0.28)。反復 4 でも
    /// 7 以上でも無事という帯状の不安定で、しかも発散判定には掛からないので
    /// 黙って誤った布が出ていた。
    ///
    /// 「速くなった」ばかり見て、壊れる組み合わせを探していなかった。
    #[test]
    fn chebyshev_never_curls_cloth_upward() {
        let (nx, ny, edge, clamp) = (31usize, 5usize, 0.005, 5usize);
        let idx = |x: usize, y: usize| y * nx + x;
        let overhang = (nx - clamp) as f64 * edge;

        let mut positions = Vec::new();
        for y in 0..ny {
            for x in 0..nx {
                positions.push(Vec3::new(x as f64 * edge, y as f64 * edge, 0.0));
            }
        }
        let mut edges = Vec::new();
        let mut tris = Vec::new();
        for y in 0..ny {
            for x in 0..nx {
                if x + 1 < nx {
                    edges.push((idx(x, y), idx(x + 1, y)));
                }
                if y + 1 < ny {
                    edges.push((idx(x, y), idx(x, y + 1)));
                }
                if x + 1 < nx && y + 1 < ny {
                    tris.push((idx(x, y), idx(x + 1, y), idx(x + 1, y + 1)));
                    tris.push((idx(x, y), idx(x + 1, y + 1), idx(x, y + 1)));
                }
            }
        }
        let quads = crate::bending::quads_from_triangles(&tris);
        let pinned: Vec<usize> = (0..ny)
            .flat_map(|y| (0..clamp).map(move |x| idx(x, y)))
            .collect();

        let droop = |iterations: u32, rho: f64| -> f64 {
            let mut sim = ClothSim::new(
                positions.clone(), &edges, &quads, &tris, &pinned, 0.15, 0.0, 0.0,
            );
            let params = SimParams {
                iterations,
                substeps: 16,
                damping: 0.6,
                collision_enabled: false,
                post_collision_iterations: 0,
                chebyshev_radius: rho,
                ..SimParams::default()
            };
            for _ in 0..150 {
                sim.step(1.0 / 60.0, &params);
            }
            assert!(sim.is_finite(), "iterations={iterations} rho={rho} で発散した");
            let tip: f64 =
                (0..ny).map(|y| sim.positions[idx(nx - 1, y)].z).sum::<f64>() / ny as f64;
            -tip / overhang
        };

        // 5 と 6 が、遅延 2 の頃に壊れていた帯
        for iterations in [4u32, 5, 6, 7, 8, 12] {
            for rho in [0.0, 0.9, 0.95, 0.98] {
                let d = droop(iterations, rho);
                assert!(
                    d > 0.0,
                    "反り返った: iterations={iterations} rho={rho} 垂れ比={d:.4}"
                );
                // 重力で垂れているのだから、上限も外れないこと
                assert!(
                    d < 1.5,
                    "垂れすぎ(発散しかけ): iterations={iterations} rho={rho} 垂れ比={d:.4}"
                );
            }
        }

        // 反復が遅延以下なら加速は一度も走らない = 素の反復と完全に一致する
        for iterations in [4u32, 5] {
            let plain = droop(iterations, 0.0);
            let boosted = droop(iterations, 0.98);
            assert_eq!(
                plain, boosted,
                "反復 {iterations} (遅延 {CHEBYSHEV_DELAY} 以下) で加速が走っている"
            );
        }
    }

    /// 摩擦の強さが Substeps に依らないこと。
    ///
    /// 摩擦はサブステップごとに1回掛かるので、正規化しないと1フレームでの
    /// 残存率が `(1-f)^substeps` になる。既定の f=0.3 では substeps 4 で
    /// 0.24、substeps 16 で 0.0033 と **70倍**違い、球に落とした布が
    /// substeps 4 では滑り落ちるのに 16 では留まる、という状態だった。
    /// 品質のための設定が摩擦という別物を動かしてしまっていた。
    #[test]
    fn friction_does_not_depend_on_substeps() {
        let thickness = 0.02;
        let friction = 0.3;
        let dt = 1.0 / 60.0;

        // 床の上を滑らせて、1フレームでどれだけ減速するかを見る
        let retention = |substeps: u32| -> f64 {
            let mut sim = ClothSim::new(
                vec![Vec3::new(0.0, 0.0, 0.0)],
                &[], &[], &[], &[], 0.0, 0.0, 0.0,
            );
            let base = SimParams {
                gravity: Vec3::zero(),
                damping: 0.0,
                iterations: 1,
                substeps,
                collision_enabled: false,
                post_collision_iterations: 0,
                floor_enabled: true,
                // 頂点をちょうど床の上に置き、接触し続ける状態にする
                floor_z: 0.0,
                friction,
                ..SimParams::default()
            };

            // 接線方向(+x)に速度を作る。床には触れたまま
            sim.step(dt, &SimParams { wind: Vec3::new(1.0 / dt, 0.0, 0.0), ..base });
            let before = sim.positions[0].x;
            sim.step(dt, &base);
            let during = sim.positions[0].x - before;

            // 摩擦を受けたあとの速度は、次のフレームの移動量に現れる
            let after = sim.positions[0].x;
            sim.step(dt, &SimParams { friction: 0.0, ..base });
            let moved = sim.positions[0].x - after;
            let _ = during;
            moved / dt
        };

        let reference = retention(REFERENCE_SUBSTEPS);
        for substeps in [1u32, 2, 8, 16, 32] {
            let got = retention(substeps);
            assert!(
                (got - reference).abs() < reference.abs() * 0.1 + 1e-6,
                "substeps {substeps} で摩擦が変わった: {reference:.4} -> {got:.4}"
            );
        }
    }

    /// 速く動くコライダーが布を素通りしないこと。
    ///
    /// コライダーはフレーム頭で1回しか更新されないので、1フレームの移動が
    /// コライダー自身の大きさに近づくと、重なる瞬間がサンプルされずに
    /// 素通りしていた。加えて探索半径が 0.025 しかなく、**大きなコライダーの
    /// 内部深くに入った頂点は表面が遠くて検出されなかった**。
    /// 実測では球(直径 0.30m)が 15 m/s で通ると 8回中2回、60 m/s では
    /// 8回中5回、布に触れずに通り抜けた。
    ///
    /// `set_collider_target` でサブステップごとに補間し、探索半径を
    /// 移動量ぶん広げることで、いずれも 0 回になった。
    #[test]
    fn fast_collider_does_not_pass_through_cloth() {
        let (n, e, radius) = (15usize, 0.05, 0.15);
        let cx = (n - 1) as f64 * e / 2.0;

        let mut positions = Vec::new();
        for z in 0..n {
            for x in 0..n {
                positions.push(Vec3::new(x as f64 * e, 0.0, z as f64 * e));
            }
        }
        let mut edges = Vec::new();
        let mut tris = Vec::new();
        for z in 0..n {
            for x in 0..n {
                let i = z * n + x;
                if x + 1 < n {
                    edges.push((i, i + 1));
                }
                if z + 1 < n {
                    edges.push((i, i + n));
                }
                if x + 1 < n && z + 1 < n {
                    tris.push((i, i + 1, i + n + 1));
                    tris.push((i, i + n + 1, i + n));
                }
            }
        }
        let quads = crate::bending::quads_from_triangles(&tris);
        let pinned = vec![0, n - 1, n * (n - 1), n * n - 1];

        // 球が y 方向に通過する
        let sphere = |y: f64| build_sphere(Vec3::new(cx, y, cx), radius, 16, 12);

        // interpolate=false は従来の update_collider(その場で動かす)
        let shaken = |speed: f64, start: f64, interpolate: bool| -> f64 {
            let mut sim = ClothSim::new(
                positions.clone(), &edges, &quads, &tris, &pinned, 0.2, 0.0, 0.02,
            );
            let (sp, st) = sphere(start);
            sim.add_collider(sp, st);

            let params = SimParams {
                gravity: Vec3::zero(),
                damping: 0.0,
                collision_enabled: true,
                collision_thickness: 0.005,
                ..SimParams::default()
            };
            let dt = 1.0 / 60.0;
            let mut y = start;
            let frames = ((start.abs() + 0.5) / (speed * dt)) as usize + 2;
            for _ in 0..frames {
                y += speed * dt;
                let (moved, _) = sphere(y);
                if interpolate {
                    sim.set_collider_target(0, moved).unwrap();
                } else {
                    sim.update_collider(0, moved).unwrap();
                }
                sim.step(dt, &params);
            }

            // 球を遠ざけて、残った揺れを測る。当たっていれば揺れている
            let (far, _) = sphere(100.0);
            sim.update_collider(0, far).unwrap();
            let mut shake: f64 = 0.0;
            for _ in 0..20 {
                sim.step(dt, &params);
                for p in sim.positions.iter() {
                    shake = shake.max(p.y.abs());
                }
            }
            assert!(sim.is_finite(), "発散した");
            shake
        };

        // 1フレームで球の直径(0.30m)以上動く速さ
        let speed = 30.0;
        let per_frame = speed / 60.0;

        // 開始位置(位相)をずらして、どれも取りこぼさないこと
        let mut missed = 0;
        for k in 0..4 {
            let start = -0.5 - k as f64 * (per_frame / 4.0);
            if shaken(speed, start, true) < 0.01 {
                missed += 1;
            }
        }
        assert_eq!(missed, 0, "補間しても素通りした位相がある: {missed}/4");
    }

    /// 摩擦の強さが接触対の個数に依らないこと。
    ///
    /// 接触ごとに掛けていた頃は、接線速度に (1 - friction) が接触数ぶん
    /// 累乗で掛かっていた(friction 0.3 / 8対で 70% のはずが 5.8%)。
    /// 摩擦がメッシュ密度と局所配置で変わってしまうので、頂点ごとに1回に
    /// まとめた。ここでは中心の頂点の真下に置く頂点の数を変えて、
    /// 接線速度の残り方が変わらないことを確かめる。
    #[test]
    fn friction_does_not_compound_with_contact_count() {
        let thickness = 0.02;
        let friction = 0.3;
        let dt = 1.0 / 60.0;

        // 自己衝突だけを効かせる設定
        let quiet = SimParams {
            gravity: Vec3::zero(),
            damping: 0.0,
            iterations: 1,
            substeps: 1,
            collision_enabled: false,
            post_collision_iterations: 0,
            ..SimParams::default()
        };

        let retention = |count: usize| -> f64 {
            let mut positions = vec![Vec3::zero()];
            for m in 0..count {
                let ang = std::f64::consts::TAU * m as f64 / count.max(1) as f64;
                positions.push(Vec3::new(
                    0.0008 * ang.cos(),
                    0.0008 * ang.sin(),
                    -0.9 * thickness,
                ));
            }
            let mut sim = ClothSim::new(positions, &[], &[], &[], &[], 0.0, 0.0, 0.0);

            // 中心に接線方向(+x)の速度を作る
            let push = SimParams {
                wind: Vec3::new(1.0 / dt, 0.0, 0.0),
                ..quiet
            };
            sim.step(dt, &push);

            // 自己衝突ありで1ステップ。末尾で摩擦が掛かる
            let touch = SimParams {
                self_collision_enabled: true,
                self_collision_thickness: thickness,
                collision_friction: friction,
                ..quiet
            };
            sim.step(dt, &touch);

            // 摩擦を受けた後の速度は、次のステップの移動量に現れる
            let before = sim.positions[0].x;
            sim.step(dt, &quiet);
            (sim.positions[0].x - before) / (1.0 * dt)
        };

        // 摩擦は Substeps で正規化してあるので、1フレームでの残存率は
        // サブステップ数に依らず (1-f)^REFERENCE_SUBSTEPS になる
        let expected = (1.0 - friction).powi(REFERENCE_SUBSTEPS as i32);
        let one = retention(1);
        assert!(
            (one - expected).abs() < 0.02,
            "接触1つのときの残存率が想定と違う: {one} (期待 {expected})"
        );
        for count in [2usize, 4, 8] {
            let many = retention(count);
            assert!(
                (many - one).abs() < 0.05,
                "接触が {count} 個で摩擦が変わった: {one:.4} -> {many:.4}"
            );
        }
    }

    /// 自己衝突が**実際に層を引き離している**こと。
    ///
    /// 既存の `self_collision_keeps_cloth_stable` は上端を固定した平らな
    /// グリッドを垂らすだけで、自己接触が一度も起きない(接触数 0 を確認済み)。
    /// つまり `resolve_self_collisions` が何もしなくても通ってしまう。
    /// ここでは重なった2枚を用意して、押し離すことを直接確かめる。
    #[test]
    fn self_collision_separates_stacked_layers() {
        let (nx, ny, spacing) = (5usize, 5usize, 0.05);
        let thickness = 0.02;
        // 食い込みは浅くする。深い食い込みは押し出しがそのまま速度になり
        // (v = 食い込み / サブステップの dt)、布が吹き飛ぶ。
        // 例: 厚み 0.02 に対し 0.016 食い込ませると 3.84 m/s で分離する。
        // ここで見たいのは「押し離せるか」なので、その影響が出ない量にする。
        let gap = 0.018;

        // 同じ形の2枚を gap だけ離して重ねる(互いに制約では繋がっていない)
        let mut positions = Vec::new();
        let mut edges = Vec::new();
        for layer in 0..2 {
            let base = layer * nx * ny;
            let z = layer as f64 * gap;
            for y in 0..ny {
                for x in 0..nx {
                    positions.push(Vec3::new(x as f64 * spacing, y as f64 * spacing, z));
                }
            }
            for y in 0..ny {
                for x in 0..nx {
                    let i = base + y * nx + x;
                    if x + 1 < nx {
                        edges.push((i, i + 1));
                    }
                    if y + 1 < ny {
                        edges.push((i, i + nx));
                    }
                }
            }
        }

        let closest = |sim: &ClothSim| -> f64 {
            let n = sim.positions.len();
            let half = n / 2;
            let mut worst = f64::MAX;
            for i in 0..half {
                for j in half..n {
                    worst = worst.min(sim.positions[i].sub(sim.positions[j]).length());
                }
            }
            worst
        };

        let run = |enabled: bool| -> f64 {
            let mut sim =
                ClothSim::new(positions.clone(), &edges, &[], &[], &[], 0.0, 0.0, 0.0);
            let params = SimParams {
                gravity: Vec3::zero(),
                damping: 0.0,
                collision_enabled: false,
                self_collision_enabled: enabled,
                self_collision_thickness: thickness,
                ..SimParams::default()
            };
            // 1ステップで押し離せることを見る。長く回すと、押し出しで得た
            // 速度のぶん(重力も減衰も無いので)いつまでも離れ続ける
            sim.step(1.0 / 60.0, &params);
            assert!(sim.is_finite());
            closest(&sim)
        };

        let without = run(false);
        assert!(
            (without - gap).abs() < 1e-6,
            "自己衝突なしなのに動いた: {without}"
        );

        let with = run(true);
        assert!(
            with >= thickness,
            "重なった2枚が厚みまで離れない: {with} (厚み {thickness})"
        );
        assert!(
            with < thickness * 2.0,
            "押し離しすぎ: {with} (厚み {thickness})"
        );
    }

    /// 崩れて折り重なった布で、自己衝突が層を保てているか。
    ///
    /// 重ねた2枚を離すだけの試験では、実際の使われ方(折り目が次々に増える)
    /// を再現できない。垂直な布を床に崩落させ、**制約で結ばれていない頂点対が
    /// 厚みより近くなっていないか**を総当たりで数える。
    ///
    /// 自己衝突なしでは布が完全に潰れて重なる(頂点が一致するところまでいく)。
    /// あるなら違反はごくわずかに収まるはず。
    #[test]
    fn self_collision_keeps_layers_apart_when_cloth_piles_up() {
        let (n, spacing) = (15usize, 0.04);
        let thickness = 0.016; // エッジ長の 0.4倍(推奨どおり)
        let floor_z = -0.30;

        let idx = |x: usize, y: usize| y * n + x;
        let mut positions = Vec::new();
        for y in 0..n {
            for x in 0..n {
                // xz 平面に立てた布。床のすぐ上から崩れ落ちる
                positions.push(Vec3::new(
                    x as f64 * spacing,
                    0.0,
                    floor_z + 0.02 + y as f64 * spacing,
                ));
            }
        }
        let mut edges = Vec::new();
        let mut tris = Vec::new();
        for y in 0..n {
            for x in 0..n {
                if x + 1 < n {
                    edges.push((idx(x, y), idx(x + 1, y)));
                }
                if y + 1 < n {
                    edges.push((idx(x, y), idx(x, y + 1)));
                }
                if x + 1 < n && y + 1 < n {
                    tris.push((idx(x, y), idx(x + 1, y), idx(x + 1, y + 1)));
                    tris.push((idx(x, y), idx(x + 1, y + 1), idx(x, y + 1)));
                }
            }
        }
        let quads = crate::bending::quads_from_triangles(&tris);

        // Rust 側の constrained_pairs と同じ集合(伸び + 曲げの対角)
        let mut constrained = std::collections::HashSet::new();
        for &(a, b) in &edges {
            constrained.insert((a.min(b), a.max(b)));
        }
        for &(_p1, _p2, p3, p4) in &quads {
            constrained.insert((p3.min(p4), p3.max(p4)));
        }

        let run = |enabled: bool| -> (usize, f64) {
            let mut sim =
                ClothSim::new(positions.clone(), &edges, &quads, &tris, &[], 0.2, 0.0, 0.02);
            let params = SimParams {
                damping: 0.02,
                floor_enabled: true,
                floor_z,
                collision_enabled: false,
                self_collision_enabled: enabled,
                self_collision_thickness: thickness,
                ..SimParams::default()
            };
            for _ in 0..240 {
                sim.step(1.0 / 60.0, &params);
            }
            assert!(sim.is_finite(), "崩落で発散した (自己衝突 {enabled})");

            let total = sim.positions.len();
            let mut count = 0usize;
            let mut worst = f64::MAX;
            for i in 0..total {
                for j in (i + 1)..total {
                    let d = sim.positions[i].sub(sim.positions[j]).length();
                    if d >= thickness || constrained.contains(&(i, j)) {
                        continue;
                    }
                    count += 1;
                    worst = worst.min(d);
                }
            }
            (count, if count == 0 { thickness } else { worst })
        };

        let (off_count, off_worst) = run(false);
        let (on_count, on_worst) = run(true);

        // 自己衝突なしでは大量に重なる(効果の基準線)
        assert!(
            off_count > 200,
            "自己衝突なしでも重ならない。試験が自己接触を起こせていない: {off_count} 対"
        );
        // 有効にしたら大きく減ること
        assert!(
            on_count * 4 < off_count,
            "自己衝突を入れても違反が減っていない: {off_count} -> {on_count} 対"
        );
        // 潰れ切った対が残っていないこと(厚みに対する最小距離)
        assert!(
            on_worst > off_worst,
            "最も潰れた対が改善していない: {off_worst:.5} -> {on_worst:.5}"
        );
    }

    #[test]
    fn self_collision_separates_overlapping_vertices() {
        // 制約で結ばれていない2頂点をほぼ同じ位置に置く
        let positions = vec![Vec3::new(0.0, 0.0, 0.0), Vec3::new(0.001, 0.0, 0.0)];
        let mut sim = ClothSim::new(positions, &[], &[], &[], &[], 0.0, 0.0, 0.0);

        let params = SimParams {
            gravity: Vec3::zero(),
            self_collision_enabled: true,
            self_collision_thickness: 0.02,
            ..SimParams::default()
        };
        sim.step(1.0 / 60.0, &params);

        let dist = sim.positions[0].sub(sim.positions[1]).length();
        assert!(dist > 0.019, "押し離されていない: {dist}");
    }

    /// ピン留めを付け外ししても面積由来の質量分布が壊れない
    #[test]
    fn set_pinned_preserves_mass_distribution() {
        let (positions, edges, bending, tris, pinned) = build_grid(7, 7, 0.1);
        let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &pinned, 0.5, 0.0, 1e-4);

        let free_vertex = 0; // 下端(ピン留めされていない)
        let before = sim.inv_mass[free_vertex];

        sim.set_pinned(&[1, 2]);
        sim.set_pinned(&pinned);

        assert!(
            (sim.inv_mass[free_vertex] - before).abs() < 1e-12,
            "質量分布が壊れた: {} -> {}",
            before,
            sim.inv_mass[free_vertex]
        );
        assert_eq!(sim.inv_mass[pinned[0]], 0.0, "ピンが効いていない");
    }

    /// テスト用の球メッシュ(UV球)を作る
    fn build_sphere(
        center: Vec3,
        radius: f64,
        segments: usize,
        rings: usize,
    ) -> (Vec<Vec3>, Vec<[usize; 3]>) {
        let mut verts = Vec::new();
        for r in 0..=rings {
            let phi = std::f64::consts::PI * r as f64 / rings as f64;
            for s in 0..segments {
                let theta = 2.0 * std::f64::consts::PI * s as f64 / segments as f64;
                verts.push(Vec3::new(
                    center.x + radius * phi.sin() * theta.cos(),
                    center.y + radius * phi.sin() * theta.sin(),
                    center.z + radius * phi.cos(),
                ));
            }
        }

        let mut tris = Vec::new();
        for r in 0..rings {
            for s in 0..segments {
                let s_next = (s + 1) % segments;
                let a = r * segments + s;
                let b = r * segments + s_next;
                let c = (r + 1) * segments + s_next;
                let d = (r + 1) * segments + s;
                // 外向き法線になる巻き順
                tris.push([a, d, c]);
                tris.push([a, c, b]);
            }
        }
        (verts, tris)
    }

    /// テスト用の平面グリッド生成。上端の行をピン留め対象として返す。
    /// テスト用の平面グリッド生成。
    ///
    /// 曲げ制約は三角形から導く(`bending::quads_from_triangles`)。
    /// 戻り値: (頂点, 伸び制約, 曲げの4頂点, 三角形, 上端の行)
    fn build_grid(
        nx: usize,
        ny: usize,
        spacing: f64,
    ) -> (
        Vec<Vec3>,
        Vec<(usize, usize)>,
        Vec<(usize, usize, usize, usize)>,
        Vec<(usize, usize, usize)>,
        Vec<usize>,
    ) {
        let idx = |x: usize, y: usize| y * nx + x;
        let mut positions = Vec::with_capacity(nx * ny);
        for y in 0..ny {
            for x in 0..nx {
                positions.push(Vec3::new(x as f64 * spacing, y as f64 * spacing, 0.0));
            }
        }

        let mut edges = Vec::new();
        let mut tris = Vec::new();
        for y in 0..ny {
            for x in 0..nx {
                if x + 1 < nx {
                    edges.push((idx(x, y), idx(x + 1, y)));
                }
                if y + 1 < ny {
                    edges.push((idx(x, y), idx(x, y + 1)));
                }

                if x + 1 < nx && y + 1 < ny {
                    tris.push((idx(x, y), idx(x + 1, y), idx(x + 1, y + 1)));
                    tris.push((idx(x, y), idx(x + 1, y + 1), idx(x, y + 1)));
                }
            }
        }

        let bending = crate::bending::quads_from_triangles(&tris);
        let pinned = (0..nx).map(|x| idx(x, ny - 1)).collect();
        (positions, edges, bending, tris, pinned)
    }
}
