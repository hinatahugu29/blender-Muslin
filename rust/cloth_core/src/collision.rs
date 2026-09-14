//! 衝突判定: 三角形メッシュ用 BVH と、自己衝突用の空間ハッシュ。
//!
//! いずれも Python 非依存なので `cargo test` で単体検証できる。

use crate::math::Vec3;

// ------------------------------------------------------------------ AABB

#[derive(Clone, Copy, Debug)]
pub struct Aabb {
    pub min: Vec3,
    pub max: Vec3,
}

impl Aabb {
    pub fn empty() -> Self {
        Aabb {
            min: Vec3::new(f64::MAX, f64::MAX, f64::MAX),
            max: Vec3::new(f64::MIN, f64::MIN, f64::MIN),
        }
    }

    pub fn expand_point(&mut self, p: Vec3) {
        self.min.x = self.min.x.min(p.x);
        self.min.y = self.min.y.min(p.y);
        self.min.z = self.min.z.min(p.z);
        self.max.x = self.max.x.max(p.x);
        self.max.y = self.max.y.max(p.y);
        self.max.z = self.max.z.max(p.z);
    }

    pub fn merge(&mut self, other: &Aabb) {
        self.expand_point(other.min);
        self.expand_point(other.max);
    }

    pub fn center(&self) -> Vec3 {
        self.min.add(self.max).scale(0.5)
    }

    /// 点から AABB までの距離の2乗(内部なら 0)
    pub fn distance_squared_to(&self, p: Vec3) -> f64 {
        let dx = (self.min.x - p.x).max(0.0).max(p.x - self.max.x);
        let dy = (self.min.y - p.y).max(0.0).max(p.y - self.max.y);
        let dz = (self.min.z - p.z).max(0.0).max(p.z - self.max.z);
        dx * dx + dy * dy + dz * dz
    }
}

// -------------------------------------------------------- 三角形の最近接点

/// 三角形 (a,b,c) 上で点 p に最も近い点を返す(Ericson, Real-Time Collision Detection)。
pub fn closest_point_on_triangle(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> Vec3 {
    let ab = b.sub(a);
    let ac = c.sub(a);
    let ap = p.sub(a);

    let d1 = ab.dot(ap);
    let d2 = ac.dot(ap);
    if d1 <= 0.0 && d2 <= 0.0 {
        return a;
    }

    let bp = p.sub(b);
    let d3 = ab.dot(bp);
    let d4 = ac.dot(bp);
    if d3 >= 0.0 && d4 <= d3 {
        return b;
    }

    let vc = d1 * d4 - d3 * d2;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        let v = d1 / (d1 - d3);
        return a.add(ab.scale(v));
    }

    let cp = p.sub(c);
    let d5 = ab.dot(cp);
    let d6 = ac.dot(cp);
    if d6 >= 0.0 && d5 <= d6 {
        return c;
    }

    let vb = d5 * d2 - d1 * d6;
    if vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0 {
        let w = d2 / (d2 - d6);
        return a.add(ac.scale(w));
    }

    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        let w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        return b.add(c.sub(b).scale(w));
    }

    let denom = 1.0 / (va + vb + vc);
    let v = vb * denom;
    let w = vc * denom;
    a.add(ab.scale(v)).add(ac.scale(w))
}

// ------------------------------------------------------------------- BVH

#[derive(Clone, Debug)]
struct BvhNode {
    bounds: Aabb,
    /// 葉なら三角形インデックス範囲、内部ノードなら (左の子, 使わない)
    left: usize,
    right: usize,
    start: usize,
    count: usize,
}

impl BvhNode {
    fn is_leaf(&self) -> bool {
        self.count > 0
    }
}

const BVH_LEAF_SIZE: usize = 4;

/// 静的(または頂点だけ動く)三角形メッシュに対する最近接クエリ用 BVH。
#[derive(Clone, Debug)]
pub struct TriangleBvh {
    pub vertices: Vec<Vec3>,
    pub triangles: Vec<[usize; 3]>,
    nodes: Vec<BvhNode>,
    /// nodes が参照する三角形インデックスの並び替え結果
    order: Vec<usize>,
}

impl TriangleBvh {
    pub fn new(vertices: Vec<Vec3>, triangles: Vec<[usize; 3]>) -> Self {
        let n = vertices.len();
        let triangles: Vec<[usize; 3]> = triangles
            .into_iter()
            .filter(|t| t[0] < n && t[1] < n && t[2] < n)
            .collect();

        let mut bvh = TriangleBvh {
            order: (0..triangles.len()).collect(),
            vertices,
            triangles,
            nodes: Vec::new(),
        };
        if !bvh.triangles.is_empty() {
            let count = bvh.order.len();
            bvh.build_node(0, count);
        }
        bvh
    }

    pub fn triangle_count(&self) -> usize {
        self.triangles.len()
    }

    fn tri_bounds(&self, tri: usize) -> Aabb {
        let t = self.triangles[tri];
        let mut b = Aabb::empty();
        b.expand_point(self.vertices[t[0]]);
        b.expand_point(self.vertices[t[1]]);
        b.expand_point(self.vertices[t[2]]);
        b
    }

    /// order[start..start+count] の三角形を含むノードを構築し、そのインデックスを返す。
    fn build_node(&mut self, start: usize, count: usize) -> usize {
        let mut bounds = Aabb::empty();
        for i in start..start + count {
            let b = self.tri_bounds(self.order[i]);
            bounds.merge(&b);
        }

        let node_index = self.nodes.len();
        self.nodes.push(BvhNode {
            bounds,
            left: 0,
            right: 0,
            start,
            count,
        });

        if count <= BVH_LEAF_SIZE {
            return node_index;
        }

        // 最も広い軸の中央値で分割
        let extent = bounds.max.sub(bounds.min);
        let axis = if extent.x >= extent.y && extent.x >= extent.z {
            0
        } else if extent.y >= extent.z {
            1
        } else {
            2
        };

        let centroid = |bvh: &TriangleBvh, tri: usize| -> f64 {
            let c = bvh.tri_bounds(tri).center();
            match axis {
                0 => c.x,
                1 => c.y,
                _ => c.z,
            }
        };

        let slice = &mut self.order[start..start + count];
        let mut keyed: Vec<(f64, usize)> = slice.iter().map(|&t| (0.0, t)).collect();
        for entry in keyed.iter_mut() {
            entry.0 = centroid(self, entry.1);
        }
        keyed.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        for (i, (_, tri)) in keyed.into_iter().enumerate() {
            self.order[start + i] = tri;
        }

        let mid = count / 2;
        let left = self.build_node(start, mid);
        let right = self.build_node(start + mid, count - mid);

        let node = &mut self.nodes[node_index];
        node.left = left;
        node.right = right;
        node.count = 0; // 内部ノード
        node_index
    }

    /// 頂点座標だけを更新し、BVH の境界を再フィットする(トポロジ不変が前提)。
    /// 形状が大きく変わった場合は精度が落ちるが、再構築より遥かに速い。
    pub fn refit(&mut self, vertices: Vec<Vec3>) {
        if vertices.len() != self.vertices.len() {
            return;
        }
        self.vertices = vertices;
        if self.nodes.is_empty() {
            return;
        }
        self.refit_node(0);
    }

    fn refit_node(&mut self, index: usize) -> Aabb {
        let (is_leaf, start, count, left, right) = {
            let n = &self.nodes[index];
            (n.is_leaf(), n.start, n.count, n.left, n.right)
        };

        let bounds = if is_leaf {
            let mut b = Aabb::empty();
            for i in start..start + count {
                let tb = self.tri_bounds(self.order[i]);
                b.merge(&tb);
            }
            b
        } else {
            let mut b = self.refit_node(left);
            let rb = self.refit_node(right);
            b.merge(&rb);
            b
        };

        self.nodes[index].bounds = bounds;
        bounds
    }

    /// 点 p から半径 radius 以内で最も近い三角形上の点を探す。
    /// 戻り値: (最近接点, 三角形インデックス, 距離)
    pub fn closest_point(&self, p: Vec3, radius: f64) -> Option<(Vec3, usize, f64)> {
        if self.nodes.is_empty() {
            return None;
        }
        let mut best: Option<(Vec3, usize, f64)> = None;
        let mut best_dist_sq = radius * radius;

        // 明示スタックで再帰を避ける(深いメッシュでもスタックを食わない)
        let mut stack = vec![0usize];
        while let Some(index) = stack.pop() {
            let node = &self.nodes[index];
            if node.bounds.distance_squared_to(p) > best_dist_sq {
                continue;
            }
            if node.is_leaf() {
                for i in node.start..node.start + node.count {
                    let tri = self.order[i];
                    let t = self.triangles[tri];
                    let q = closest_point_on_triangle(
                        p,
                        self.vertices[t[0]],
                        self.vertices[t[1]],
                        self.vertices[t[2]],
                    );
                    let d2 = q.sub(p).dot(q.sub(p));
                    if d2 < best_dist_sq {
                        best_dist_sq = d2;
                        best = Some((q, tri, d2.sqrt()));
                    }
                }
            } else {
                stack.push(node.left);
                stack.push(node.right);
            }
        }

        best
    }

    /// 三角形の面法線(正規化済み)。縮退三角形では None。
    pub fn triangle_normal(&self, tri: usize) -> Option<Vec3> {
        let t = self.triangles[tri];
        let a = self.vertices[t[0]];
        self.vertices[t[1]]
            .sub(a)
            .cross(self.vertices[t[2]].sub(a))
            .normalized()
    }
}

// --------------------------------------------------------------- 空間ハッシュ

/// 自己衝突検出用の一様グリッド空間ハッシュ。
/// 格子キー `(i64, i64, i64)` 用の軽量ハッシャ。
///
/// 標準の SipHash は暗号強度を持つぶん重く、毎サブステップ全頂点をハッシュする
/// 用途では無視できないコストになる。ここでは乗算とシフトだけの混合で済ませる。
#[derive(Default, Clone, Copy)]
pub struct GridHasher {
    state: u64,
}

impl std::hash::Hasher for GridHasher {
    fn finish(&self) -> u64 {
        // 最後に上位ビットを下位へ混ぜる(バケット選択は下位ビットを見るため)
        let mut h = self.state;
        h ^= h >> 33;
        h = h.wrapping_mul(0xff51_afd7_ed55_8ccd);
        h ^= h >> 29;
        h
    }

    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(b as u64);
        }
    }

    fn write_i64(&mut self, value: i64) {
        self.write_u64(value as u64);
    }

    fn write_u64(&mut self, value: u64) {
        self.state = (self.state.rotate_left(5) ^ value).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    }
}

#[derive(Default, Clone, Copy)]
pub struct GridHasherBuilder;

impl std::hash::BuildHasher for GridHasherBuilder {
    type Hasher = GridHasher;
    fn build_hasher(&self) -> GridHasher {
        GridHasher::default()
    }
}

type GridMap = std::collections::HashMap<(i64, i64, i64), Vec<u32>, GridHasherBuilder>;

pub struct SpatialHash {
    cell_size: f64,
    /// セルキー -> そのセルに入る頂点インデックス
    ///
    /// 毎サブステップ作り直すとバケットの `Vec` を都度確保することになるため、
    /// エントリは残したまま中身だけ空にして確保済みメモリを使い回す。
    buckets: GridMap,
}

impl Default for SpatialHash {
    fn default() -> Self {
        SpatialHash::new(1e-6)
    }
}

impl SpatialHash {
    pub fn new(cell_size: f64) -> Self {
        SpatialHash {
            cell_size: cell_size.max(1e-6),
            buckets: GridMap::default(),
        }
    }

    fn key(&self, p: Vec3) -> (i64, i64, i64) {
        (
            (p.x / self.cell_size).floor() as i64,
            (p.y / self.cell_size).floor() as i64,
            (p.z / self.cell_size).floor() as i64,
        )
    }

    /// セルサイズを変えて作り直す。サイズが変わらなければ確保済みメモリを保つ。
    pub fn rebuild_with(&mut self, cell_size: f64, positions: &[Vec3]) {
        let cell_size = cell_size.max(1e-6);
        if (cell_size - self.cell_size).abs() > f64::EPSILON * self.cell_size.max(1.0) {
            // セルサイズが変わるとキーの意味が変わるので、ここだけは捨てる
            self.cell_size = cell_size;
            self.buckets.clear();
        }
        self.rebuild(positions);
    }

    pub fn rebuild(&mut self, positions: &[Vec3]) {
        self.buckets.clear();
        for (i, p) in positions.iter().enumerate() {
            self.buckets.entry(self.key(*p)).or_default().push(i as u32);
        }
    }

    /// 点 p の近傍(自セル + 隣接26セル)にある頂点インデックスを列挙する。
    pub fn for_each_neighbor<F: FnMut(usize)>(&self, p: Vec3, mut f: F) {
        let (cx, cy, cz) = self.key(p);
        for dx in -1..=1 {
            for dy in -1..=1 {
                for dz in -1..=1 {
                    if let Some(bucket) = self.buckets.get(&(cx + dx, cy + dy, cz + dz)) {
                        for &i in bucket {
                            f(i as usize);
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit_quad() -> (Vec<Vec3>, Vec<[usize; 3]>) {
        let verts = vec![
            Vec3::new(0.0, 0.0, 0.0),
            Vec3::new(1.0, 0.0, 0.0),
            Vec3::new(1.0, 1.0, 0.0),
            Vec3::new(0.0, 1.0, 0.0),
        ];
        let tris = vec![[0, 1, 2], [0, 2, 3]];
        (verts, tris)
    }

    #[test]
    fn closest_point_inside_face() {
        let p = Vec3::new(0.3, 0.3, 1.0);
        let q = closest_point_on_triangle(
            p,
            Vec3::new(0.0, 0.0, 0.0),
            Vec3::new(1.0, 0.0, 0.0),
            Vec3::new(0.0, 1.0, 0.0),
        );
        assert!((q.sub(Vec3::new(0.3, 0.3, 0.0)).length()) < 1e-12);
    }

    #[test]
    fn closest_point_clamps_to_vertex() {
        let q = closest_point_on_triangle(
            Vec3::new(-1.0, -1.0, 0.0),
            Vec3::new(0.0, 0.0, 0.0),
            Vec3::new(1.0, 0.0, 0.0),
            Vec3::new(0.0, 1.0, 0.0),
        );
        assert!(q.sub(Vec3::zero()).length() < 1e-12);
    }

    #[test]
    fn bvh_finds_nearest_triangle() {
        let (verts, tris) = unit_quad();
        let bvh = TriangleBvh::new(verts, tris);
        let hit = bvh.closest_point(Vec3::new(0.5, 0.5, 0.4), 1.0);
        let (q, _, dist) = hit.expect("見つかるはず");
        assert!((dist - 0.4).abs() < 1e-9, "dist = {dist}");
        assert!(q.z.abs() < 1e-9);
    }

    #[test]
    fn bvh_respects_search_radius() {
        let (verts, tris) = unit_quad();
        let bvh = TriangleBvh::new(verts, tris);
        assert!(bvh.closest_point(Vec3::new(0.5, 0.5, 5.0), 1.0).is_none());
    }

    /// 多数の三角形でも総当たりと同じ結果になること
    #[test]
    fn bvh_matches_brute_force() {
        let n = 20;
        let mut verts = Vec::new();
        let mut tris = Vec::new();
        for y in 0..n {
            for x in 0..n {
                verts.push(Vec3::new(
                    x as f64 * 0.1,
                    y as f64 * 0.1,
                    ((x * y) % 7) as f64 * 0.01,
                ));
            }
        }
        for y in 0..n - 1 {
            for x in 0..n - 1 {
                let i = y * n + x;
                tris.push([i, i + 1, i + n + 1]);
                tris.push([i, i + n + 1, i + n]);
            }
        }
        let bvh = TriangleBvh::new(verts.clone(), tris.clone());

        for probe in [
            Vec3::new(0.55, 0.35, 0.5),
            Vec3::new(1.2, 1.7, -0.3),
            Vec3::new(0.05, 1.9, 0.2),
        ] {
            let mut brute = f64::MAX;
            for t in &tris {
                let q = closest_point_on_triangle(probe, verts[t[0]], verts[t[1]], verts[t[2]]);
                brute = brute.min(q.sub(probe).length());
            }
            let (_, _, bvh_dist) = bvh.closest_point(probe, 100.0).expect("見つかるはず");
            assert!(
                (bvh_dist - brute).abs() < 1e-9,
                "BVH {bvh_dist} vs 総当たり {brute}"
            );
        }
    }

    #[test]
    fn refit_tracks_moved_vertices() {
        let (verts, tris) = unit_quad();
        let mut bvh = TriangleBvh::new(verts.clone(), tris);
        let moved: Vec<Vec3> = verts.iter().map(|v| v.add(Vec3::new(0.0, 0.0, 3.0))).collect();
        bvh.refit(moved);

        assert!(bvh.closest_point(Vec3::new(0.5, 0.5, 0.0), 1.0).is_none());
        let hit = bvh.closest_point(Vec3::new(0.5, 0.5, 3.2), 1.0);
        assert!(hit.is_some(), "移動後の位置で見つかるはず");
    }

    #[test]
    fn spatial_hash_finds_close_pairs() {
        let positions = vec![
            Vec3::new(0.0, 0.0, 0.0),
            Vec3::new(0.01, 0.0, 0.0),
            Vec3::new(5.0, 5.0, 5.0),
        ];
        let mut hash = SpatialHash::new(0.1);
        hash.rebuild(&positions);

        let mut found = Vec::new();
        hash.for_each_neighbor(positions[0], |i| found.push(i));
        assert!(found.contains(&0) && found.contains(&1));
        assert!(!found.contains(&2), "遠い頂点は列挙されないはず");
    }

    /// セルサイズを変えても、変えなくても、作り直した結果は正しい
    #[test]
    fn rebuild_with_handles_cell_size_change() {
        let a = Vec3::new(0.0, 0.0, 0.0);
        let b = Vec3::new(0.05, 0.0, 0.0);
        let mut hash = SpatialHash::new(0.1);
        hash.rebuild_with(0.1, &[a, b]);

        let collect = |h: &SpatialHash, p: Vec3| {
            let mut v = Vec::new();
            h.for_each_neighbor(p, |i| v.push(i));
            v.sort_unstable();
            v
        };
        assert_eq!(collect(&hash, a), vec![0, 1]);

        // セルサイズを小さくすると b は近傍から外れる
        hash.rebuild_with(0.01, &[a, b]);
        assert_eq!(collect(&hash, a), vec![0], "セルサイズ変更が反映されていない");

        // 同じサイズで作り直しても、前回の頂点が残らない
        hash.rebuild_with(0.01, &[a]);
        assert_eq!(collect(&hash, a), vec![0], "前回のバケット内容が残っている");
    }
}
