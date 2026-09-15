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

/// ゼロ除算を避けるための下限。分母はどれも長さの2乗なので、
/// この程度を下回る三角形は退化しているとみなす。
const DEGENERATE_EPS: f64 = 1e-20;

/// 三角形 (a,b,c) 上で点 p に最も近い点を返す(Ericson, Real-Time Collision Detection)。
///
/// **退化した三角形でも NaN を返さない。** 頂点が重なった三角形(布が潰れたとき、
/// あるいは二重頂点のあるコライダーメッシュ)では分母が 0 になり、NaN が
/// 頂点座標へ流れ込んで発散していた。教科書の式には無い分岐を足してある。
pub fn closest_point_on_triangle(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> Vec3 {
    const EPS: f64 = DEGENERATE_EPS;
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
        // 分母は |ab|^2。a と b が重なっていると 0/0 で NaN になる
        let denom = d1 - d3;
        if denom <= EPS {
            return a;
        }
        let v = d1 / denom;
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
        // 分母は |ac|^2
        let denom = d2 - d6;
        if denom <= EPS {
            return a;
        }
        let w = d2 / denom;
        return a.add(ac.scale(w));
    }

    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        // 分母は |bc|^2
        let denom = (d4 - d3) + (d5 - d6);
        if denom <= EPS {
            return b;
        }
        let w = (d4 - d3) / denom;
        return b.add(c.sub(b).scale(w));
    }

    // 分母は三角形の面積の2倍。一直線に並んでいると 0 になる
    let sum = va + vb + vc;
    if sum <= EPS {
        return a;
    }
    let denom = 1.0 / sum;
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

    /// 三角形の3頂点を返す。広域探索を毎フレームに減らす際、
    /// キャッシュした三角形に対して最近点を取り直すのに使う。
    pub fn triangle(&self, tri: usize) -> Option<(Vec3, Vec3, Vec3)> {
        let t = self.triangles.get(tri)?;
        Some((self.vertices[t[0]], self.vertices[t[1]], self.vertices[t[2]]))
    }

    /// 点 p から `radius` 以内にある三角形を全て列挙する。
    ///
    /// `closest_point` は最近傍1個しか返さないが、フレーム先頭で候補を
    /// 作り置きする用途では、途中で最近傍が入れ替わっても足りるよう
    /// 半径内の三角形をまとめて拾っておく必要がある。
    pub fn for_each_triangle_within<F: FnMut(usize)>(&self, p: Vec3, radius: f64, mut f: F) {
        if self.nodes.is_empty() {
            return;
        }
        let radius_sq = radius * radius;
        let mut stack = vec![0usize];
        while let Some(index) = stack.pop() {
            let node = &self.nodes[index];
            if node.bounds.distance_squared_to(p) > radius_sq {
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
                    if q.sub(p).dot(q.sub(p)) <= radius_sq {
                        f(tri);
                    }
                }
            } else {
                stack.push(node.left);
                stack.push(node.right);
            }
        }
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
/// 一様グリッドの空間ハッシュ。自己衝突の近傍探索に使う。
///
/// HashMap ではなく計数ソートで作る。毎サブステップ作り直すため、
/// ハッシュ表の維持コストがそのまま効いてくるので、再利用できる
/// 平坦な配列だけで組む。
pub struct SpatialHash {
    cell_size: f64,
    /// バケット数 - 1(バケット数は2の冪)
    mask: usize,
    /// 各バケットの開始位置。長さはバケット数 + 1
    starts: Vec<u32>,
    /// バケット順に並べた頂点番号
    entries: Vec<u32>,
    /// `entries` と同じ並びの、セルの完全なハッシュ値
    ///
    /// 固定長のテーブルでは異なるセルが同じバケットに落ちる。数えるときに
    /// これを突き合わせて、他のセルの頂点を弾く。持たないと 1クエリあたり
    /// 十数個の余計な候補を数えることになる。
    keys: Vec<u64>,
    /// 書き込み位置の作業領域
    cursor: Vec<u32>,
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
            mask: 0,
            starts: Vec::new(),
            entries: Vec::new(),
            keys: Vec::new(),
            cursor: Vec::new(),
        }
    }

    #[inline]
    fn cell_of(&self, p: Vec3) -> (i64, i64, i64) {
        (
            (p.x / self.cell_size).floor() as i64,
            (p.y / self.cell_size).floor() as i64,
            (p.z / self.cell_size).floor() as i64,
        )
    }

    /// セル座標のハッシュ値。乗算とシフトだけで混ぜる。
    #[inline]
    pub(crate) fn hash_cell(cell: (i64, i64, i64)) -> u64 {
        let (x, y, z) = cell;
        let h = (x as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15)
            ^ (y as u64).wrapping_mul(0xc2b2_ae3d_27d4_eb4f)
            ^ (z as u64).wrapping_mul(0x1656_67b1_9e37_79f9);
        h ^ (h >> 29)
    }

    #[inline]
    fn bucket_of(&self, hash: u64) -> usize {
        (hash as usize) & self.mask
    }

    /// セルサイズを変えて作り直す。
    pub fn rebuild_with(&mut self, cell_size: f64, positions: &[Vec3]) {
        self.cell_size = cell_size.max(1e-6);
        self.rebuild(positions);
    }

    pub fn rebuild(&mut self, positions: &[Vec3]) {
        let n = positions.len();
        if n == 0 {
            self.mask = 0;
            self.starts.clear();
            self.entries.clear();
            self.keys.clear();
            return;
        }

        // 充填率を 0.5 程度に保つ(衝突が増えると余計な候補を数えることになる)
        let buckets = (2 * n).next_power_of_two().max(64);
        self.mask = buckets - 1;

        self.starts.clear();
        self.starts.resize(buckets + 1, 0);

        // 1回目: 個数を数える
        for p in positions {
            let b = self.bucket_of(Self::hash_cell(self.cell_of(*p)));
            self.starts[b + 1] += 1;
        }
        // 累積和
        for i in 0..buckets {
            self.starts[i + 1] += self.starts[i];
        }

        // 2回目: 書き込む
        self.cursor.clear();
        self.cursor.extend_from_slice(&self.starts[..buckets]);
        self.entries.clear();
        self.entries.resize(n, 0);
        self.keys.clear();
        self.keys.resize(n, 0);
        for (i, p) in positions.iter().enumerate() {
            let hash = Self::hash_cell(self.cell_of(*p));
            let b = self.bucket_of(hash);
            let slot = self.cursor[b] as usize;
            self.entries[slot] = i as u32;
            self.keys[slot] = hash;
            self.cursor[b] += 1;
        }
    }

    /// 点 p の近傍(自セル + 隣接26セル)にある頂点インデックスを列挙する。
    ///
    /// 各頂点はちょうど1つのセルに属し、27セルは互いに異なるので、
    /// ハッシュ値を突き合わせれば重複も余計な候補も出ない。
    pub fn for_each_neighbor<F: FnMut(usize)>(&self, p: Vec3, mut f: F) {
        if self.entries.is_empty() {
            return;
        }
        let (cx, cy, cz) = self.cell_of(p);

        for dx in -1..=1 {
            for dy in -1..=1 {
                for dz in -1..=1 {
                    let hash = Self::hash_cell((cx + dx, cy + dy, cz + dz));
                    let b = self.bucket_of(hash);
                    let from = self.starts[b] as usize;
                    let to = self.starts[b + 1] as usize;
                    for slot in from..to {
                        // 同じバケットに落ちた別セルの頂点を弾く
                        if self.keys[slot] == hash {
                            f(self.entries[slot] as usize);
                        }
                    }
                }
            }
        }
    }
}

/// 三角形上の点 `q` を重心座標で表す。`q` は三角形の平面上にある前提。
///
/// `closest_point_on_triangle` が返した点に使い、押し出しを3頂点へ配分する。
pub fn barycentric_on_triangle(q: Vec3, a: Vec3, b: Vec3, c: Vec3) -> (f64, f64, f64) {
    let v0 = b.sub(a);
    let v1 = c.sub(a);
    let v2 = q.sub(a);

    let d00 = v0.dot(v0);
    let d01 = v0.dot(v1);
    let d11 = v1.dot(v1);
    let d20 = v2.dot(v0);
    let d21 = v2.dot(v1);

    let denom = d00 * d11 - d01 * d01;
    if denom.abs() < 1e-20 {
        // 縮退した三角形。均等に配分する
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0);
    }

    let v = (d11 * d20 - d01 * d21) / denom;
    let w = (d00 * d21 - d01 * d20) / denom;
    (1.0 - v - w, v, w)
}

/// 三角形用の空間ハッシュ。頂点-三角形の自己衝突で使う。
///
/// 頂点用の [`SpatialHash`] と違い、**挿入時に厚みぶん広げて** AABB が跨る
/// セル全部に入れる。こうすると引くときは頂点のいるセル1つを見るだけで済み、
/// 27セルを走査しなくてよい。三角形は頂点より数が少ないので、挿入を厚くして
/// 参照を薄くするほうが釣り合う。
///
/// セルの大きさは「最も大きい三角形が収まる」ように決める。こうすると
/// 1つの三角形が跨るセルが軸あたり高々2つ(+ 余白で 3つ)に収まる。
pub struct TriangleHash {
    cell_size: f64,
    mask: usize,
    starts: Vec<u32>,
    entries: Vec<u32>,
    /// セルのハッシュ値の下位32ビット。
    ///
    /// 同じバケットに落ちた別セルの三角形を弾くためだけに使うので、
    /// 取りこぼしが無ければ足りる。衝突しても余計な候補が1つ増えるだけで、
    /// そのあとの距離判定で落ちる。u64 で持つと 14,641頂点で 900KB になり、
    /// **毎サブステップ触るせいで伸び制約と曲げ制約までキャッシュから
    /// 追い出していた**(計測で stretch が 9.7 → 21.1ms に膨らんだ)。
    keys: Vec<u32>,
    cursor: Vec<u32>,
}

impl Default for TriangleHash {
    fn default() -> Self {
        TriangleHash::new()
    }
}

impl TriangleHash {
    pub fn new() -> Self {
        TriangleHash {
            cell_size: 1.0,
            mask: 0,
            starts: Vec::new(),
            entries: Vec::new(),
            keys: Vec::new(),
            cursor: Vec::new(),
        }
    }

    #[inline]
    fn cell_index(&self, v: f64) -> i64 {
        (v / self.cell_size).floor() as i64
    }

    /// 三角形の AABB を margin ぶん広げ、跨るセルの範囲を返す。
    fn cell_range(
        &self,
        positions: &[Vec3],
        tri: [usize; 3],
        margin: f64,
    ) -> ((i64, i64, i64), (i64, i64, i64)) {
        let (a, b, c) = (positions[tri[0]], positions[tri[1]], positions[tri[2]]);
        let lo = Vec3::new(
            a.x.min(b.x).min(c.x) - margin,
            a.y.min(b.y).min(c.y) - margin,
            a.z.min(b.z).min(c.z) - margin,
        );
        let hi = Vec3::new(
            a.x.max(b.x).max(c.x) + margin,
            a.y.max(b.y).max(c.y) + margin,
            a.z.max(b.z).max(c.z) + margin,
        );
        (
            (self.cell_index(lo.x), self.cell_index(lo.y), self.cell_index(lo.z)),
            (self.cell_index(hi.x), self.cell_index(hi.y), self.cell_index(hi.z)),
        )
    }

    /// 三角形を入れ直す。`margin` は自己衝突の厚み。
    pub fn rebuild(&mut self, positions: &[Vec3], triangles: &[[usize; 3]], margin: f64) {
        let n = triangles.len();
        if n == 0 {
            self.mask = 0;
            self.starts.clear();
            self.entries.clear();
            self.keys.clear();
            return;
        }

        // セルの大きさは「広げたあとの三角形が軸あたり高々2セルに収まる」
        // ように決める。ここを最大辺そのものにすると、平らな布では1三角形が
        // 18セルほどに跨り、**書き込みだけで 18.29ms/frame** かかった
        // (14,641頂点、substeps 4)。広げた幅に合わせると 1/4 以下になる。
        //
        // 大きくするほど書き込みは減るが、1セルあたりの三角形が増えて
        // 参照時の最近接点の計算が増える。両者の釣り合いで決めた値。
        let mut extent: f64 = 0.0;
        for t in triangles {
            let (a, b, c) = (positions[t[0]], positions[t[1]], positions[t[2]]);
            extent = extent
                .max(a.x.max(b.x).max(c.x) - a.x.min(b.x).min(c.x))
                .max(a.y.max(b.y).max(c.y) - a.y.min(b.y).min(c.y))
                .max(a.z.max(b.z).max(c.z) - a.z.min(b.z).min(c.z));
        }
        self.cell_size = (extent + 2.0 * margin).max(1e-6);

        // バケット数は控えめにする。毎サブステップ全体を舐めるので、
        // 大きく取るとゼロ埋めと累積和だけでキャッシュを潰す。
        let buckets = (2 * n).next_power_of_two().max(64);
        self.mask = buckets - 1;

        self.starts.clear();
        self.starts.resize(buckets + 1, 0);

        // 1回目: セルごとの個数を数える。
        // 範囲を配列に取っておく案も試したが、14,641頂点で 1.35MB になり、
        // キャッシュを潰して逆に遅くなった。作り直すほうが速い。
        for t in triangles {
            let (lo, hi) = self.cell_range(positions, *t, margin);
            for x in lo.0..=hi.0 {
                for y in lo.1..=hi.1 {
                    for z in lo.2..=hi.2 {
                        let b = (SpatialHash::hash_cell((x, y, z)) as usize) & self.mask;
                        self.starts[b + 1] += 1;
                    }
                }
            }
        }
        for i in 0..buckets {
            self.starts[i + 1] += self.starts[i];
        }

        let total = self.starts[buckets] as usize;
        self.cursor.clear();
        self.cursor.extend_from_slice(&self.starts[..buckets]);
        self.entries.clear();
        self.entries.resize(total, 0);
        self.keys.clear();
        self.keys.resize(total, 0);

        // 2回目: 書き込む
        for (ti, t) in triangles.iter().enumerate() {
            let (lo, hi) = self.cell_range(positions, *t, margin);
            for x in lo.0..=hi.0 {
                for y in lo.1..=hi.1 {
                    for z in lo.2..=hi.2 {
                        let hash = SpatialHash::hash_cell((x, y, z));
                        let b = (hash as usize) & self.mask;
                        let slot = self.cursor[b] as usize;
                        self.entries[slot] = ti as u32;
                        self.keys[slot] = hash as u32;
                        self.cursor[b] += 1;
                    }
                }
            }
        }
    }

    /// 点 `p` のセルに入っている三角形を列挙する。
    ///
    /// 挿入時に厚みぶん広げてあるので、`p` から厚み以内にある三角形は
    /// 必ず `p` のセルに入っている。1セルだけ見れば足りる。
    pub fn for_each_triangle<F: FnMut(usize)>(&self, p: Vec3, mut f: F) {
        if self.entries.is_empty() {
            return;
        }
        let cell = (
            self.cell_index(p.x),
            self.cell_index(p.y),
            self.cell_index(p.z),
        );
        let hash = SpatialHash::hash_cell(cell);
        let b = (hash as usize) & self.mask;
        let from = self.starts[b] as usize;
        let to = self.starts[b + 1] as usize;
        let key = hash as u32;
        for slot in from..to {
            // 同じバケットに落ちた別セルの三角形を弾く
            if self.keys[slot] == key {
                f(self.entries[slot] as usize);
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
    /// 退化した三角形でも NaN を返さないこと。
    ///
    /// 分母は順に |ab|^2, |ac|^2, |bc|^2, 三角形の面積の2倍に対応する。
    /// 頂点が重なった三角形(布が潰れたとき、あるいは二重頂点のあるコライダー)
    /// では 0/0 になり、NaN が頂点座標に流れ込んで発散していた。
    #[test]
    fn closest_point_handles_degenerate_triangles() {
        let p = Vec3::new(0.3, 0.4, 1.0);
        let o = Vec3::zero();
        let x = Vec3::new(1.0, 0.0, 0.0);
        let y = Vec3::new(0.0, 1.0, 0.0);

        let cases = [
            ("a と b が一致", o, o, y),
            ("a と c が一致", o, x, o),
            ("b と c が一致", o, x, x),
            ("3点とも一致", o, o, o),
            ("一直線に並ぶ", o, x, x.scale(2.0)),
        ];
        for (name, a, b, c) in cases {
            let q = closest_point_on_triangle(p, a, b, c);
            assert!(
                q.x.is_finite() && q.y.is_finite() && q.z.is_finite(),
                "{name} で NaN が出た: {q:?}"
            );
        }
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
