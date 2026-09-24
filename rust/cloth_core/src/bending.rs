//! 曲げ制約。平均曲率ベクトルの二次形式を使う。
//!
//! # なぜこの形なのか
//!
//! 2つの案を実装して計測し、どちらも退けた末にこれに落ち着いた。
//!
//! **1. 距離制約(Provot 方式)** — 隣接2面の対角頂点どうしを距離で結ぶ。
//! 曲げ角 θ に対する長さ変化が `1 - cos(θ/2) ≈ θ²/8` と**二次**で、角度への感度が
//! 低すぎる。完全剛体(compliance=0)にしても 22cm の片持ち試料が 21.85cm 垂れた
//! (制約なしでは 22.18cm)。制約自体は平均 0.1% の精度で満たされているのに、
//! その 0.1% が頂点あたり 5 度に相当し、44 頂点で 225 度になるため。
//! **剛性の制御として機能しない。**
//!
//! **2. 二面角(acos ベース)** — 角度を直接扱う教科書的な形。勾配に `1/sin φ` が
//! 入るため、**平らな付近(布が普段いる状態)で数値的に破綻する**。実測では
//! compliance 1e3 未満で発散し(伸び誤差が 10^10 に達した)、平ら付近を除外して
//! 安定させると今度は曲げ剛性が消えた。必要な情報が使えない領域にだけある。
//!
//! **3. 二次形式(これ)** — 静止状態で平面になる4頂点には、
//! `Σ K_i = 0` かつ `Σ K_i x_i = 0` を満たす重み `K` が(定数倍を除いて)一意に定まる。
//! 曲がると `v = Σ K_i x_i` が 0 から離れるので、これを平均曲率ベクトルとみなして
//! **`C = |v|`** を制約にする。三角関数も 1/sin も無い。
//!
//! `C = ½|v|²` にすると、分母 `Σ w|∇C|² = |v|² Σ wK²` が `|v|²` に比例するため、
//! 曲がりが小さいところで `α̃` が支配的になって compliance の制御が効かなくなる
//! (実測: 1e-6 以上の値がすべて同じ結果になった)。一次にすると分母が
//! `Σ wK²` の定数になり、compliance と対等に効く。

use crate::math::Vec3;

/// 曲げ制約。共有辺の2点とその両側の対角頂点、あわせて4頂点で1つの折り目。
#[derive(Clone, Copy, Debug)]
pub struct BendingConstraint {
    /// 共有辺の端点
    pub p1: usize,
    /// 共有辺のもう一方の端点
    pub p2: usize,
    /// 共有辺の片側の対角頂点
    pub p3: usize,
    /// 共有辺のもう一方の側の対角頂点
    pub p4: usize,
    /// 静止形状から決まる重み。`Σ K_i = 0` なので平行移動に不変。
    pub k: [f64; 4],
    /// XPBD コンプライアンス。0 で完全剛体(折れ曲がらない)
    pub compliance: f64,
}

const EPS: f64 = 1e-12;

impl BendingConstraint {
    /// 静止状態の4頂点から重みを求めて作る。
    ///
    /// 4点が平面上にあるとき、`p4` は三角形 `(p1,p2,p3)` の重心座標
    /// `(α, β, γ)` で表せる。`K = (α, β, γ, -1)` とすれば
    /// `Σ K_i = 0` かつ `Σ K_i x_i = 0` を満たす。
    ///
    /// 三角形が縮退している場合は None。
    pub fn new(
        positions: &[Vec3],
        p1: usize,
        p2: usize,
        p3: usize,
        p4: usize,
        compliance: f64,
    ) -> Option<Self> {
        let a = positions[p1];
        let b = positions[p2];
        let c = positions[p3];
        let d = positions[p4];

        let (alpha, beta, gamma) = barycentric(d, a, b, c)?;
        let mut k = [alpha, beta, gamma, -1.0];

        // 大きさを揃えておくと、compliance の意味がメッシュ解像度に依りにくくなる
        let norm = (k.iter().map(|v| v * v).sum::<f64>()).sqrt();
        if norm < EPS {
            return None;
        }
        for v in k.iter_mut() {
            *v /= norm;
        }

        Some(BendingConstraint {
            p1,
            p2,
            p3,
            p4,
            k,
            compliance,
        })
    }

    /// 現在の平均曲率ベクトル `v = Σ K_i x_i`。静止形状では 0。
    pub fn curvature(&self, positions: &[Vec3]) -> Vec3 {
        positions[self.p1]
            .scale(self.k[0])
            .add(positions[self.p2].scale(self.k[1]))
            .add(positions[self.p3].scale(self.k[2]))
            .add(positions[self.p4].scale(self.k[3]))
    }
}

/// 点 `p` を三角形 `(a,b,c)` の重心座標で表す。縮退していれば None。
fn barycentric(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> Option<(f64, f64, f64)> {
    let v0 = b.sub(a);
    let v1 = c.sub(a);
    let v2 = p.sub(a);

    let d00 = v0.dot(v0);
    let d01 = v0.dot(v1);
    let d11 = v1.dot(v1);
    let d20 = v2.dot(v0);
    let d21 = v2.dot(v1);

    let denom = d00 * d11 - d01 * d01;
    if denom.abs() < EPS {
        return None;
    }

    let beta = (d11 * d20 - d01 * d21) / denom;
    let gamma = (d00 * d21 - d01 * d20) / denom;
    Some((1.0 - beta - gamma, beta, gamma))
}

/// 曲げ制約を1掃引ぶん解く。
pub fn solve_bending(
    positions: &mut [Vec3],
    inv_mass: &[f64],
    constraints: &[BendingConstraint],
    lambdas: &mut [f64],
    inv_dt2: f64,
) {
    for (ci, c) in constraints.iter().enumerate() {
        let idx = [c.p1, c.p2, c.p3, c.p4];
        let w = [
            inv_mass[c.p1],
            inv_mass[c.p2],
            inv_mass[c.p3],
            inv_mass[c.p4],
        ];

        // Σ w_i K_i^2。0 なら全頂点が固定されている
        let weight: f64 = (0..4).map(|i| w[i] * c.k[i] * c.k[i]).sum();
        if weight < EPS {
            continue;
        }

        let v = positions[idx[0]]
            .scale(c.k[0])
            .add(positions[idx[1]].scale(c.k[1]))
            .add(positions[idx[2]].scale(c.k[2]))
            .add(positions[idx[3]].scale(c.k[3]));

        let len = v.length();
        // ちょうど静止形状のときは補正するものが無い(方向も決められない)
        if len < 1e-10 {
            continue;
        }
        let dir = v.scale(1.0 / len);

        // C = |v| なので ∇C_i = K_i·v/|v|、Σ w|∇C|² = Σ w K²(定数)
        let alpha_tilde = c.compliance * inv_dt2;
        let lambda = lambdas[ci];
        let d_lambda = (-len - alpha_tilde * lambda) / (weight + alpha_tilde);
        lambdas[ci] = lambda + d_lambda;

        for i in 0..4 {
            if w[i] == 0.0 {
                continue;
            }
            positions[idx[i]] = positions[idx[i]].add(dir.scale(w[i] * c.k[i] * d_lambda));
        }
    }
}

/// 色分けした曲げ制約を解く(M9)。`constraints` は色の順に並んでいること。
///
/// 1つの制約の計算は `solve_bending` と同じ。同じ色のブロックは頂点を共有しない
/// ので並列にしてよい。λ は制約ごとに1つなので、書き込みはぶつからない。
pub(crate) fn solve_bending_colored(
    positions: &mut [Vec3],
    inv_mass: &[f64],
    constraints: &[BendingConstraint],
    lambdas: &mut [f64],
    inv_dt2: f64,
    colors: &[Vec<std::ops::Range<usize>>],
    parallel: bool,
) {
    use crate::coloring::{for_each_colored, Shared};
    let pos = Shared::new(positions);
    let lam = Shared::new(lambdas);
    for_each_colored(colors, parallel, |ci| {
        let c = &constraints[ci];
        let idx = [c.p1, c.p2, c.p3, c.p4];
        let w = [inv_mass[c.p1], inv_mass[c.p2], inv_mass[c.p3], inv_mass[c.p4]];
        let weight: f64 = (0..4).map(|i| w[i] * c.k[i] * c.k[i]).sum();
        if weight < EPS {
            return;
        }
        // SAFETY: 同じ色のブロックは頂点を共有しない(色分けの保証)。λ は制約ごと
        unsafe {
            let x = [pos.get(idx[0]), pos.get(idx[1]), pos.get(idx[2]), pos.get(idx[3])];
            let v = x[0]
                .scale(c.k[0])
                .add(x[1].scale(c.k[1]))
                .add(x[2].scale(c.k[2]))
                .add(x[3].scale(c.k[3]));
            let len = v.length();
            if len < 1e-10 {
                return;
            }
            let dir = v.scale(1.0 / len);
            let alpha_tilde = c.compliance * inv_dt2;
            let lambda = lam.get(ci);
            let d_lambda = (-len - alpha_tilde * lambda) / (weight + alpha_tilde);
            lam.set(ci, lambda + d_lambda);
            for i in 0..4 {
                if w[i] == 0.0 {
                    continue;
                }
                pos.set(idx[i], x[i].add(dir.scale(w[i] * c.k[i] * d_lambda)));
            }
        }
    });
}

/// 三角形の集合から、曲げ制約の4頂点を導く。
///
/// ちょうど2つの三角形に共有される辺それぞれについて
/// `(辺の端点2つ, 両側の対角頂点2つ)` を作る。境界の辺(1つの三角形にしか
/// 属さない)は曲げようがないので飛ばす。
///
/// アドオン側は Blender のメッシュから直接辺を取れるのでこれを使わないが、
/// テストや検証ハーネスのように三角形しか持っていない場合に使う。
pub fn quads_from_triangles(
    triangles: &[(usize, usize, usize)],
) -> Vec<(usize, usize, usize, usize)> {
    use std::collections::HashMap;

    let mut opposite: HashMap<(usize, usize), Vec<usize>> = HashMap::new();
    for &(a, b, c) in triangles {
        for (u, v, w) in [(a, b, c), (b, c, a), (c, a, b)] {
            let key = (u.min(v), u.max(v));
            opposite.entry(key).or_default().push(w);
        }
    }

    let mut quads: Vec<(usize, usize, usize, usize)> = opposite
        .into_iter()
        .filter(|(_, opp)| opp.len() == 2 && opp[0] != opp[1])
        .map(|((u, v), opp)| (u, v, opp[0], opp[1]))
        .collect();

    // HashMap の走査順は不定なので、決定的な順序に並べ直す
    quads.sort_unstable();
    quads
}

#[cfg(test)]
mod tests {
    use super::*;

    /// p1-p2 が共有辺、p3 と p4 がその両側。平らな配置
    fn flat_quad() -> Vec<Vec3> {
        vec![
            Vec3::new(0.0, 0.0, 0.0),  // p1 (共有辺)
            Vec3::new(1.0, 0.0, 0.0),  // p2 (共有辺)
            Vec3::new(0.0, 1.0, 0.0),  // p3 (対角)
            Vec3::new(0.0, -1.0, 0.0), // p4 (対角)
        ]
    }

    fn make(compliance: f64) -> (Vec<Vec3>, BendingConstraint) {
        let p = flat_quad();
        let c = BendingConstraint::new(&p, 0, 1, 2, 3, compliance).expect("作れるはず");
        (p, c)
    }

    #[test]
    fn rest_shape_has_zero_curvature() {
        let (p, c) = make(0.0);
        assert!(
            c.curvature(&p).length() < 1e-12,
            "静止形状では 0 のはず: {:?}",
            c.curvature(&p)
        );
    }

    /// 全体を回転・平行移動しても 0 のまま(剛体運動に反応しない)
    #[test]
    fn rigid_motion_does_not_bend() {
        let (p, c) = make(0.0);
        let (s, co) = (0.3f64.sin(), 0.3f64.cos());
        let moved: Vec<Vec3> = p
            .iter()
            .map(|v| {
                Vec3::new(v.x * co - v.y * s, v.x * s + v.y * co, v.z)
                    .add(Vec3::new(5.0, -3.0, 2.0))
            })
            .collect();
        assert!(
            c.curvature(&moved).length() < 1e-12,
            "剛体運動で曲げが生じている: {:?}",
            c.curvature(&moved)
        );
    }

    #[test]
    fn folding_produces_curvature() {
        let (p, c) = make(0.0);
        let mut folded = p.clone();
        folded[3] = Vec3::new(0.0, -0.7071, 0.7071);
        assert!(
            c.curvature(&folded).length() > 0.1,
            "折ったのに曲げが検出されない"
        );
    }

    /// 折れた配置を解くと静止形状へ戻る
    #[test]
    fn solver_flattens_a_fold() {
        let (flat, c) = make(0.0);
        let mut p = flat.clone();
        p[3] = Vec3::new(0.0, -0.7071, 0.7071);
        let inv_mass = vec![0.0, 0.0, 1.0, 1.0]; // 共有辺を固定して見やすくする
        let mut lambdas = vec![0.0];

        let before = c.curvature(&p).length();
        for _ in 0..80 {
            lambdas[0] = 0.0;
            solve_bending(&mut p, &inv_mass, &[c], &mut lambdas, 1.0);
        }
        let after = c.curvature(&p).length();
        assert!(
            after < before * 0.05,
            "平らに戻っていない: {before} -> {after}"
        );
    }

    #[test]
    fn compliance_controls_stiffness() {
        let fold = |compliance: f64| {
            let (flat, c) = make(compliance);
            let mut p = flat.clone();
            p[3] = Vec3::new(0.0, -0.7071, 0.7071);
            let inv_mass = vec![0.0, 0.0, 1.0, 1.0];
            let mut lambdas = vec![0.0];
            for _ in 0..80 {
                solve_bending(&mut p, &inv_mass, &[c], &mut lambdas, 1.0);
            }
            c.curvature(&p).length()
        };

        let stiff = fold(0.0);
        let soft = fold(100.0);
        assert!(
            soft > stiff * 5.0,
            "compliance が効いていない: 硬い {stiff} / 柔らかい {soft}"
        );
    }

    /// 勾配が数値微分と一致する
    #[test]
    fn gradient_matches_finite_difference() {
        let p = vec![
            Vec3::new(0.0, 0.0, 0.0),
            Vec3::new(1.0, 0.0, 0.0),
            Vec3::new(0.1, 0.9, 0.0),
            Vec3::new(-0.1, -0.8, 0.0),
        ];
        let c = BendingConstraint::new(&p, 0, 1, 2, 3, 0.0).expect("作れるはず");

        let mut bent = p.clone();
        bent[2].z = 0.2;
        bent[3].z = -0.1;

        let energy = |q: &[Vec3]| c.curvature(q).length();

        let h = 1e-6;
        for i in 0..4 {
            for axis in 0..3 {
                let mut plus = bent.clone();
                let mut minus = bent.clone();
                match axis {
                    0 => {
                        plus[i].x += h;
                        minus[i].x -= h;
                    }
                    1 => {
                        plus[i].y += h;
                        minus[i].y -= h;
                    }
                    _ => {
                        plus[i].z += h;
                        minus[i].z -= h;
                    }
                }
                let numeric = (energy(&plus) - energy(&minus)) / (2.0 * h);
                let curv = c.curvature(&bent);
                let grad = curv.scale(c.k[i] / curv.length());
                let got = match axis {
                    0 => grad.x,
                    1 => grad.y,
                    _ => grad.z,
                };
                assert!(
                    (got - numeric).abs() < 1e-6,
                    "頂点{i} 軸{axis}: 解析 {got} / 数値 {numeric}"
                );
            }
        }
    }

    #[test]
    fn degenerate_configuration_is_rejected() {
        let p = vec![Vec3::zero(); 4];
        assert!(BendingConstraint::new(&p, 0, 1, 2, 3, 0.0).is_none());
    }

    #[test]
    fn quads_are_derived_from_shared_edges() {
        // 2枚の三角形が1辺を共有する最小の構成
        let tris = vec![(0, 1, 2), (1, 0, 3)];
        let quads = quads_from_triangles(&tris);
        assert_eq!(quads.len(), 1, "共有辺は1本のはず: {quads:?}");
        let (a, b, c, d) = quads[0];
        assert_eq!((a, b), (0, 1), "共有辺が違う");
        assert!(
            (c == 2 && d == 3) || (c == 3 && d == 2),
            "対角頂点が違う: {c}, {d}"
        );
    }
}
