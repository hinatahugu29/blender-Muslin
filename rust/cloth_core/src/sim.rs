//! XPBDベースのクロスシミュレーションコア(Python非依存)。
//!
//! 設計方針:
//! - `pyo3` に一切依存しないので `cargo test` で単体検証できる。
//! - 制約は「距離制約」を基本形とし、伸び(stretch)・曲げ(bending)・縫製(seam)を
//!   同じソルバーで扱う。縫製だけは rest_length をアニメーションさせる点が異なる。
//! - XPBD の λ(ラグランジュ乗数)はサブステップごとにリセットし、反復内で累積する
//!   (これにより compliance が実際の物性値として反復回数に依存しなくなる)。

use crate::collision::{SpatialHash, TriangleBvh};
use crate::math::Vec3;

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
    /// 衝突解決の「後」に伸び制約を解き直す回数。
    ///
    /// 衝突の押し出しはサブステップ内の制約ループの外側で走るため、
    /// 押し広げられた分を伸び制約が回収する機会がない。
    /// ここで数回だけ解き直すと、押し出しを保ったまま伸びを戻せる。
    /// 0 で従来どおり(補正なし)。
    pub post_collision_iterations: u32,
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
            friction: 0.3,
            collision_enabled: true,
            collision_thickness: 0.005,
            collision_friction: 0.3,
            self_collision_enabled: false,
            self_collision_thickness: 0.01,
            post_collision_iterations: 2,
        }
    }
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
    pub bending_constraints: Vec<DistanceConstraint>,
    /// 縫製制約(M3)。`seam_closure` によって rest_length が変化する。
    pub seam_constraints: Vec<DistanceConstraint>,

    /// 縫製の閉じ具合 0.0(初期長のまま) 〜 1.0(完全に縫い合わさる)。
    seam_closure: f64,

    // XPBD の λ 用スクラッチ領域(毎ステップ確保し直さないよう保持)
    lambda_stretch: Vec<f64>,
    lambda_bending: Vec<f64>,
    lambda_seam: Vec<f64>,

    /// 制約で直接結ばれた頂点対。自己衝突の対象から除外するのに使う
    /// (伸び制約と自己衝突が綱引きして布が破裂するのを防ぐ)。
    constrained_pairs: std::collections::HashSet<(u32, u32)>,
    /// コリジョンオブジェクト(BVH付き三角形メッシュ)
    colliders: Vec<TriangleBvh>,
    /// 自己衝突用の空間ハッシュ(毎サブステップ再構築)
    hash: SpatialHash,
    /// 直近サブステップで接触した頂点数(デバッグ表示用)
    last_collision_count: usize,
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
        bending_pairs: &[(usize, usize)],
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
        let bending_constraints = build(bending_pairs, bending_compliance);

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

        let mut constrained_pairs = std::collections::HashSet::new();
        for c in stretch_constraints.iter().chain(bending_constraints.iter()) {
            let (a, b) = (c.i0.min(c.i1) as u32, c.i0.max(c.i1) as u32);
            constrained_pairs.insert((a, b));
        }

        let mut sim = ClothSim {
            velocities: vec![Vec3::zero(); n],
            positions,
            inv_mass,
            base_inv_mass,
            vertex_area,
            constrained_pairs,
            colliders: Vec::new(),
            hash: SpatialHash::new(0.01),
            last_collision_count: 0,
            lambda_stretch: vec![0.0; stretch_constraints.len()],
            lambda_bending: vec![0.0; bending_constraints.len()],
            lambda_seam: Vec::new(),
            stretch_constraints,
            bending_constraints,
            seam_constraints: Vec::new(),
            seam_closure: 0.0,
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
        self.colliders.len() - 1
    }

    /// アニメーションするコライダーの頂点を更新する(BVHは再フィット)。
    pub fn update_collider(&mut self, index: usize, vertices: Vec<Vec3>) -> Result<(), String> {
        let collider = self
            .colliders
            .get_mut(index)
            .ok_or_else(|| format!("collider index {index} out of range"))?;
        if collider.vertices.len() != vertices.len() {
            return Err(format!(
                "collider vertex count mismatch: expected {}, got {}",
                collider.vertices.len(),
                vertices.len()
            ));
        }
        collider.refit(vertices);
        Ok(())
    }

    pub fn clear_colliders(&mut self) {
        self.colliders.clear();
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
        self.lambda_seam = vec![0.0; self.seam_constraints.len()];
        self.apply_seam_closure();
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
        let substeps = params.substeps.max(1);
        let sub_dt = dt / substeps as f64;
        for _ in 0..substeps {
            self.substep(sub_dt, params);
        }
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

        let prev_positions = self.positions.clone();

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

        // λ をサブステップ先頭でリセット(XPBD)
        self.lambda_stretch.iter_mut().for_each(|l| *l = 0.0);
        self.lambda_bending.iter_mut().for_each(|l| *l = 0.0);
        self.lambda_seam.iter_mut().for_each(|l| *l = 0.0);

        let inv_dt2 = 1.0 / (dt * dt);
        for _ in 0..params.iterations.max(1) {
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
                &self.bending_constraints,
                &mut self.lambda_bending,
                inv_dt2,
            );
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.seam_constraints,
                &mut self.lambda_seam,
                inv_dt2,
            );
        }

        // 衝突解決(位置の押し出し)。接触した頂点と法線・摩擦を記録する。
        let contacts = self.resolve_collisions(params);

        // 押し出しで壊れた伸びを同じサブステップ内で回収する。
        // λ はリセットせず継続させる(サブステップ内での XPBD の一貫性を保つ)。
        for _ in 0..params.post_collision_iterations {
            solve_distance(
                &mut self.positions,
                &self.inv_mass,
                &self.stretch_constraints,
                &mut self.lambda_stretch,
                inv_dt2,
            );
        }

        // 位置差から速度を更新(押し出し分も速度に反映される)
        for i in 0..n {
            if self.inv_mass[i] == 0.0 {
                self.velocities[i] = Vec3::zero();
                continue;
            }
            self.velocities[i] = self.positions[i].sub(prev_positions[i]).scale(1.0 / dt);
        }

        // 接触点の摩擦: 面へ食い込む法線速度を除去し、接線速度を摩擦分だけ落とす
        for &(i, normal, friction) in &contacts {
            let v = self.velocities[i];
            let vn = v.dot(normal);
            let v_tangent = v.sub(normal.scale(vn));
            self.velocities[i] = normal
                .scale(vn.max(0.0))
                .add(v_tangent.scale(1.0 - friction.clamp(0.0, 1.0)));
        }

        self.last_collision_count = contacts.len();
    }

    /// 床面・コリジョンオブジェクト・自己衝突をまとめて解決し、接触情報を返す。
    /// 戻り値: (頂点インデックス, 接触法線, 摩擦係数)
    fn resolve_collisions(&mut self, params: &SimParams) -> Vec<(usize, Vec3, f64)> {
        let mut contacts = Vec::new();

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

        if params.collision_enabled && !self.colliders.is_empty() {
            self.resolve_object_collisions(params, &mut contacts);
        }

        if params.self_collision_enabled {
            self.resolve_self_collisions(params, &mut contacts);
        }

        contacts
    }

    /// コリジョンオブジェクト表面から布を厚み分だけ押し出す。
    fn resolve_object_collisions(
        &mut self,
        params: &SimParams,
        contacts: &mut Vec<(usize, Vec3, f64)>,
    ) {
        let thickness = params.collision_thickness.max(0.0);
        // 深く潜り込んだ頂点も拾えるよう、探索半径は厚みより大きめに取る
        let search_radius = (thickness * 4.0).max(thickness + 0.02);

        for i in 0..self.positions.len() {
            if self.inv_mass[i] == 0.0 {
                continue;
            }
            let p = self.positions[i];

            for collider in &self.colliders {
                let Some((closest, tri, dist)) = collider.closest_point(p, search_radius) else {
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
                    self.positions[i] = closest.add(outward.scale(thickness));
                    contacts.push((i, outward, params.collision_friction));
                }
            }
        }
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

        self.hash = SpatialHash::new(thickness);
        self.hash.rebuild(&self.positions);

        let n = self.positions.len();
        for i in 0..n {
            let mut neighbors = Vec::new();
            self.hash.for_each_neighbor(self.positions[i], |j| {
                if j > i {
                    neighbors.push(j);
                }
            });

            for j in neighbors {
                // 制約で直接結ばれている頂点対は自己衝突から除外する
                if self.constrained_pairs.contains(&(i as u32, j as u32)) {
                    continue;
                }

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
                let dir = delta
                    .normalized()
                    .unwrap_or(Vec3::new(0.0, 0.0, 1.0));
                let correction = (thickness - dist) / w_sum;

                self.positions[i] = self.positions[i].add(dir.scale(correction * w0));
                self.positions[j] = self.positions[j].sub(dir.scale(correction * w1));

                contacts.push((i, dir, params.collision_friction));
                contacts.push((j, dir.scale(-1.0), params.collision_friction));
            }
        }
    }

    /// 位置を強制設定し、速度をリセットする(巻き戻し用)。
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

    /// 衝突後の伸び補正は、貫通を増やさずに伸び誤差を減らす
    #[test]
    fn post_collision_pass_reduces_stretch_without_penetration() {
        let drape = |post: u32| {
            let (positions, edges, bending, tris, _) = build_grid(21, 21, 0.05);
            let mut sim = ClothSim::new(positions, &edges, &bending, &tris, &[], 0.2, 0.0, 1e-4);
            let (sphere_pos, sphere_tris) = build_sphere(Vec3::new(0.5, 0.5, -0.35), 0.3, 16, 12);
            sim.add_collider(sphere_pos, sphere_tris);
            let params = SimParams {
                collision_enabled: true,
                collision_thickness: 0.01,
                self_collision_enabled: true,
                self_collision_thickness: 0.02,
                post_collision_iterations: post,
                ..Default::default()
            };
            for _ in 0..150 {
                sim.step(1.0 / 60.0, &params);
            }
            let deepest = sim
                .positions
                .iter()
                .map(|p| p.sub(Vec3::new(0.5, 0.5, -0.35)).length())
                .fold(f64::INFINITY, f64::min);
            (sim.average_stretch_error(), deepest, sim.is_finite())
        };

        let (err_off, deepest_off, finite_off) = drape(0);
        let (err_on, deepest_on, finite_on) = drape(4);

        assert!(finite_off && finite_on, "発散した");
        assert!(
            err_on < err_off,
            "伸び補正が効いていない: {err_off} -> {err_on}"
        );
        // 補正を入れたことで球に潜り込んでいないこと(半径 0.3 を割らない)
        assert!(
            deepest_on > 0.3 - 1e-3,
            "補正で貫通した: 中心からの最小距離 {deepest_on} (補正なしでは {deepest_off})"
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
    fn build_grid(
        nx: usize,
        ny: usize,
        spacing: f64,
    ) -> (
        Vec<Vec3>,
        Vec<(usize, usize)>,
        Vec<(usize, usize)>,
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
        let mut bending = Vec::new();
        let mut tris = Vec::new();
        for y in 0..ny {
            for x in 0..nx {
                if x + 1 < nx {
                    edges.push((idx(x, y), idx(x + 1, y)));
                }
                if y + 1 < ny {
                    edges.push((idx(x, y), idx(x, y + 1)));
                }
                if x + 2 < nx {
                    bending.push((idx(x, y), idx(x + 2, y)));
                }
                if y + 2 < ny {
                    bending.push((idx(x, y), idx(x, y + 2)));
                }
                if x + 1 < nx && y + 1 < ny {
                    tris.push((idx(x, y), idx(x + 1, y), idx(x + 1, y + 1)));
                    tris.push((idx(x, y), idx(x + 1, y + 1), idx(x, y + 1)));
                }
            }
        }

        let pinned = (0..nx).map(|x| idx(x, ny - 1)).collect();
        (positions, edges, bending, tris, pinned)
    }
}
