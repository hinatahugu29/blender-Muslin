//! cloth_core: Blenderアドオン `cloth_md` 用の物理コア。
//!
//! 物理本体は [`sim`] にあり pyo3 に依存しない。このファイルは Python 束縛のみ。
//! `cargo test --no-default-features` で Python 抜きにコアだけを検証できる。

pub mod collision;
pub mod math;
pub mod sim;

#[cfg(feature = "python")]
mod bindings {
    use pyo3::prelude::*;

    use crate::math::Vec3;
    use crate::sim::{ClothSim as CoreSim, SimParams};

    /// M0 動作確認用: 固定文字列を返す
    #[pyfunction]
    fn hello() -> PyResult<String> {
        Ok("Hello from Rust (cloth_core)!".to_string())
    }

    /// M0 動作確認用: 数値演算でデータ受け渡しを確認する
    #[pyfunction]
    fn add(a: f64, b: f64) -> PyResult<f64> {
        Ok(a + b)
    }

    /// ビルドされたコアのバージョン。アドオン側で .pyd の更新漏れを検出するのに使う。
    #[pyfunction]
    fn core_version() -> &'static str {
        env!("CARGO_PKG_VERSION")
    }

    fn to_vec3s(flat: &[f64]) -> PyResult<Vec<Vec3>> {
        if flat.len() % 3 != 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "positions length must be a multiple of 3",
            ));
        }
        Ok(flat
            .chunks_exact(3)
            .map(|c| Vec3::new(c[0], c[1], c[2]))
            .collect())
    }

    fn to_flat(points: &[Vec3]) -> Vec<f64> {
        let mut out = Vec::with_capacity(points.len() * 3);
        for p in points {
            out.push(p.x);
            out.push(p.y);
            out.push(p.z);
        }
        out
    }

    fn as_usize_pairs(pairs: &[(u32, u32)]) -> Vec<(usize, usize)> {
        pairs.iter().map(|&(a, b)| (a as usize, b as usize)).collect()
    }

    /// XPBD クロスシミュレーション。
    #[pyclass]
    pub struct ClothSim {
        inner: CoreSim,
    }

    #[pymethods]
    impl ClothSim {
        /// positions: [x0,y0,z0, ...] のフラット配列(ワールド座標)
        /// edges: 伸び制約の頂点インデックスペア
        /// bending_pairs: 曲げ制約のペア(隣接面の対角頂点同士)
        /// triangles: 三角形インデックス。質量を面積×密度から算出するのに使う
        /// pinned: 固定頂点インデックス
        /// density: 面密度 kg/m^2。0 以下または triangles が空なら一様質量
        #[new]
        #[pyo3(signature = (
            positions,
            edges,
            bending_pairs,
            triangles = vec![],
            pinned = vec![],
            density = 0.2,
            stretch_compliance = 0.0,
            bending_compliance = 1e-4,
        ))]
        #[allow(clippy::too_many_arguments)]
        fn new(
            positions: Vec<f64>,
            edges: Vec<(u32, u32)>,
            bending_pairs: Vec<(u32, u32)>,
            triangles: Vec<(u32, u32, u32)>,
            pinned: Vec<u32>,
            density: f64,
            stretch_compliance: f64,
            bending_compliance: f64,
        ) -> PyResult<Self> {
            let pos = to_vec3s(&positions)?;
            let tris: Vec<(usize, usize, usize)> = triangles
                .iter()
                .map(|&(a, b, c)| (a as usize, b as usize, c as usize))
                .collect();
            let pinned: Vec<usize> = pinned.iter().map(|&i| i as usize).collect();

            Ok(ClothSim {
                inner: CoreSim::new(
                    pos,
                    &as_usize_pairs(&edges),
                    &as_usize_pairs(&bending_pairs),
                    &tris,
                    &pinned,
                    density,
                    stretch_compliance,
                    bending_compliance,
                ),
            })
        }

        /// dt 秒進める。
        #[pyo3(signature = (
            dt,
            gravity = -9.81,
            iterations = 10,
            substeps = 4,
            damping = 0.01,
            wind = (0.0, 0.0, 0.0),
            floor_enabled = false,
            floor_z = 0.0,
            friction = 0.3,
            collision_enabled = true,
            collision_thickness = 0.005,
            collision_friction = 0.3,
            self_collision_enabled = false,
            self_collision_thickness = 0.01,
            post_collision_iterations = 2,
        ))]
        #[allow(clippy::too_many_arguments)]
        fn step(
            &mut self,
            dt: f64,
            gravity: f64,
            iterations: u32,
            substeps: u32,
            damping: f64,
            wind: (f64, f64, f64),
            floor_enabled: bool,
            floor_z: f64,
            friction: f64,
            collision_enabled: bool,
            collision_thickness: f64,
            collision_friction: f64,
            self_collision_enabled: bool,
            self_collision_thickness: f64,
            post_collision_iterations: u32,
        ) {
            let params = SimParams {
                gravity: Vec3::new(0.0, 0.0, gravity),
                wind: Vec3::new(wind.0, wind.1, wind.2),
                damping,
                iterations,
                substeps,
                floor_enabled,
                floor_z,
                friction,
                collision_enabled,
                collision_thickness,
                collision_friction,
                self_collision_enabled,
                self_collision_thickness,
                post_collision_iterations,
            };
            self.inner.step(dt, &params);
        }

        /// 現在の頂点座標をフラット配列で取得
        fn get_positions(&self) -> Vec<f64> {
            to_flat(&self.inner.positions)
        }

        /// 頂点座標を強制設定し速度をリセットする(巻き戻し用)
        fn set_positions(&mut self, positions: Vec<f64>) -> PyResult<()> {
            let pos = to_vec3s(&positions)?;
            self.inner
                .set_positions(pos)
                .map_err(pyo3::exceptions::PyValueError::new_err)
        }

        /// ピン留めを再設定する(質量分布は保持される)
        fn set_pinned(&mut self, pinned: Vec<u32>) {
            let pinned: Vec<usize> = pinned.iter().map(|&i| i as usize).collect();
            self.inner.set_pinned(&pinned);
        }

        /// 縫製制約(シーム)を設定する
        #[pyo3(signature = (pairs, compliance = 0.0))]
        fn set_seams(&mut self, pairs: Vec<(u32, u32)>, compliance: f64) {
            self.inner.set_seams(&as_usize_pairs(&pairs), compliance);
        }

        /// 縫い合わせ進行度 0.0〜1.0
        fn set_seam_closure(&mut self, closure: f64) {
            self.inner.set_seam_closure(closure);
        }

        #[getter]
        fn seam_closure(&self) -> f64 {
            self.inner.seam_closure()
        }

        #[getter]
        fn vertex_count(&self) -> usize {
            self.inner.vertex_count()
        }

        // ------------------------------------------------------ コリジョン (M2)

        /// コリジョンオブジェクトを追加する(ワールド座標の三角形メッシュ)。
        /// 戻り値: そのコライダーのインデックス(後で update_collider に使う)
        fn add_collider(
            &mut self,
            positions: Vec<f64>,
            triangles: Vec<(u32, u32, u32)>,
        ) -> PyResult<usize> {
            let verts = to_vec3s(&positions)?;
            let tris: Vec<[usize; 3]> = triangles
                .iter()
                .map(|&(a, b, c)| [a as usize, b as usize, c as usize])
                .collect();
            Ok(self.inner.add_collider(verts, tris))
        }

        /// アニメーションするコライダーの頂点座標を更新する(BVHは再フィットのみ)。
        fn update_collider(&mut self, index: usize, positions: Vec<f64>) -> PyResult<()> {
            let verts = to_vec3s(&positions)?;
            self.inner
                .update_collider(index, verts)
                .map_err(pyo3::exceptions::PyValueError::new_err)
        }

        fn clear_colliders(&mut self) {
            self.inner.clear_colliders();
        }

        #[getter]
        fn collider_count(&self) -> usize {
            self.inner.collider_count()
        }

        #[getter]
        fn collider_triangle_count(&self) -> usize {
            self.inner.collider_triangle_count()
        }

        /// 直近ステップでコリジョン応答が発生した頂点数(デバッグ用)
        #[getter]
        fn last_collision_count(&self) -> usize {
            self.inner.last_collision_count()
        }

        /// 平均伸び誤差(品質チェック用)
        fn average_stretch_error(&self) -> f64 {
            self.inner.average_stretch_error()
        }

        /// 数値発散していないか
        fn is_finite(&self) -> bool {
            self.inner.is_finite()
        }
    }

    #[pymodule]
    fn cloth_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(hello, m)?)?;
        m.add_function(wrap_pyfunction!(add, m)?)?;
        m.add_function(wrap_pyfunction!(core_version, m)?)?;
        m.add_class::<ClothSim>()?;
        Ok(())
    }
}
