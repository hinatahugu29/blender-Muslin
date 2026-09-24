//! 制約のブロック色分け(M9)。
//!
//! 制約を元の順番のまま一定数ずつの**ブロック**に区切り、頂点を共有する
//! ブロックどうしが同じ色にならないよう色分けする。同じ色のブロックは
//! 互いの頂点に触れないので並列に解ける。ブロックの中は元の順番のまま
//! 逐次に解く。
//!
//! 制約1本ずつを色分けすると、Gauss-Seidel が「端から順に伝わる」性質を
//! 失って Jacobi に近づき、収束が目に見えて落ちた(1681頂点・cotton で
//! 伸び誤差が Draft 0.570 → 1.040、Normal 0.139 → 0.228)。ブロックにすれば
//! 順番が変わるのはブロックの境目だけになる。
//!
//! 解く順番は組み立て時に決め、並列でも逐次でも同じにする(= 並列にしても
//! 結果は1ビットも変わらない)。

use std::ops::Range;

/// 並列に回すときの1ブロックの制約数。
///
/// 大きいほど元の順番に近く(収束が落ちない)、小さいほど並列にできる
/// ブロックが増える。約3万頂点・球に被せた場面・20 スレッドで測った
/// (伸び誤差は色分け前が自己衝突なし 0.0542 / あり 0.0437):
///
/// | BLOCK | 1フレーム(なし / あり) | 伸び誤差(なし / あり) |
/// |-------|-------------------------|------------------------|
/// | 前    | 209 / 326 ms            | 0.0542 / 0.0437        |
/// | 256   |  74 / 145 ms            | 0.0626 / 0.0500        |
/// | 1024  |  70 / 146 ms            | 0.0557 / 0.0429        |
/// | 4096  |  87 / 177 ms            | 0.0533 / 0.0423        |
///
/// 1024 で速さを保ったまま、誤差は色分け前とほぼ同じになる。
pub const BLOCK: usize = 1024;

/// 制約を元の順番のまま `block` 本ずつに区切ってブロックごとに色分けし、
/// (色の順に並べた添字, 色ごとのブロックの区間の一覧) を返す。
///
/// `vertices_of(i)` は制約 i が触る頂点。ブロックの中の順番、同じ色の
/// ブロックの並びはどちらも元の順番を保つので、結果は入力だけで決まる。
/// `block` が `count` 以上なら、ブロック1つ・色1つ = 元の順番そのもの。
pub fn block_color_order<'a, F>(
    count: usize,
    vertex_count: usize,
    block: usize,
    vertices_of: F,
) -> (Vec<usize>, Vec<Vec<Range<usize>>>)
where
    F: Fn(usize) -> &'a [usize],
{
    let block = block.max(1);
    let blocks = count.div_ceil(block);

    // 頂点ごとに「そこに触れたブロックの色」の一覧を持つ。貪欲に、
    // 触れる頂点のどれにも使われていない一番小さい色を選ぶ
    let mut block_color = vec![0u32; blocks];
    let mut used: Vec<Vec<u32>> = vec![Vec::new(); vertex_count];
    let mut taken: Vec<bool> = Vec::new();
    let mut colors = 0u32;
    let mut touched: Vec<usize> = Vec::new();
    for (b, slot) in block_color.iter_mut().enumerate() {
        touched.clear();
        for i in b * block..((b + 1) * block).min(count) {
            touched.extend_from_slice(vertices_of(i));
        }
        touched.sort_unstable();
        touched.dedup();

        taken.clear();
        taken.resize(colors as usize + 1, false);
        for &v in &touched {
            for &c in &used[v] {
                taken[c as usize] = true;
            }
        }
        let chosen = taken.iter().position(|t| !t).unwrap_or(taken.len()) as u32;
        for &v in &touched {
            used[v].push(chosen);
        }
        *slot = chosen;
        colors = colors.max(chosen + 1);
    }

    let mut order = Vec::with_capacity(count);
    let mut groups: Vec<Vec<Range<usize>>> = vec![Vec::new(); colors as usize];
    for c in 0..colors {
        for (b, &bc) in block_color.iter().enumerate() {
            if bc != c {
                continue;
            }
            let start = order.len();
            order.extend(b * block..((b + 1) * block).min(count));
            groups[c as usize].push(start..order.len());
        }
    }
    (order, groups)
}

/// 同じ色の中で並列に書き込むための生ポインタ。
///
/// 安全なのは「同じ色のブロックは頂点を共有しない」が成り立つ間だけ。
/// `block_color_order` で並べた区間の中でしか使わないこと。
#[derive(Clone, Copy)]
pub(crate) struct Shared<T>(*mut T);

// 書き込み先がブロックごとに重ならないことを色分けが保証する
unsafe impl<T> Send for Shared<T> {}
unsafe impl<T> Sync for Shared<T> {}

impl<T: Copy> Shared<T> {
    pub(crate) fn new(values: &mut [T]) -> Self {
        Shared(values.as_mut_ptr())
    }

    /// # Safety
    /// 同じ色の中で i を触るのが自分のブロックだけであること
    pub(crate) unsafe fn get(self, i: usize) -> T {
        *self.0.add(i)
    }

    /// # Safety
    /// 同じ色の中で i を触るのが自分のブロックだけであること
    pub(crate) unsafe fn set(self, i: usize, v: T) {
        *self.0.add(i) = v;
    }
}

/// 色ごとに、ブロックの中の制約を順に `solve_one(制約番号)` で解く。
/// 同じ色のブロックは並列にしてよい。
///
/// 並列でも逐次でも、各ブロックの計算は同じ入力から同じ順に行われ、
/// 同じ色のブロックは互いの結果を読まないので、結果は1ビットも変わらない。
pub(crate) fn for_each_colored<F>(colors: &[Vec<Range<usize>>], parallel: bool, solve_one: F)
where
    F: Fn(usize) + Sync,
{
    use rayon::prelude::*;
    for blocks in colors {
        if parallel && blocks.len() > 1 {
            blocks.par_iter().for_each(|b| b.clone().for_each(&solve_one));
        } else {
            for b in blocks {
                b.clone().for_each(&solve_one);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid_edges(n: usize) -> Vec<Vec<usize>> {
        let mut e = Vec::new();
        for y in 0..n {
            for x in 0..n {
                let i = y * n + x;
                if x + 1 < n {
                    e.push(vec![i, i + 1]);
                }
                if y + 1 < n {
                    e.push(vec![i, i + n]);
                }
            }
        }
        e
    }

    /// 並べ替えが正しい(全部1回ずつ・同じ色のブロックは頂点を共有しない)ことを確かめ、色数を返す
    fn check_valid(items: &[Vec<usize>], vertex_count: usize, block: usize) -> usize {
        let (order, groups) = block_color_order(items.len(), vertex_count, block, |i| &items[i]);
        let mut seen = vec![false; items.len()];
        for &i in &order {
            assert!(!seen[i]);
            seen[i] = true;
        }
        assert!(seen.iter().all(|s| *s));
        for blocks in &groups {
            let mut owner = vec![usize::MAX; vertex_count];
            for (k, b) in blocks.iter().enumerate() {
                for &i in &order[b.clone()] {
                    for &v in &items[i] {
                        assert!(owner[v] == usize::MAX || owner[v] == k,
                                "同じ色の別のブロックが頂点 {v} を共有している");
                        owner[v] = k;
                    }
                }
                // ブロックの中は元の順番のまま
                assert!(order[b.clone()].windows(2).all(|w| w[1] == w[0] + 1));
            }
        }
        groups.len()
    }

    #[test]
    fn grid_blocks_get_few_colors_without_sharing() {
        let items = grid_edges(80);
        let colors = check_valid(&items, 6400, 64);
        assert!(colors <= 4, "{colors} 色");
    }

    #[test]
    fn one_big_block_keeps_the_original_order() {
        let items = grid_edges(10);
        let (order, groups) = block_color_order(items.len(), 100, items.len(), |i| &items[i]);
        assert_eq!(order, (0..items.len()).collect::<Vec<_>>());
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0], vec![0..items.len()]);
    }

    #[test]
    fn single_constraint_blocks_are_valid_too() {
        // ブロック 1 本 = 制約ごとの色分け。1つの頂点を 100 本が共有 → 100 色
        let items: Vec<Vec<usize>> = (0..100).map(|k| vec![0, k + 1]).collect();
        assert_eq!(check_valid(&items, 101, 1), 100);
    }

    #[test]
    fn empty_input() {
        let (order, groups) = block_color_order(0, 10, BLOCK, |_| &[][..]);
        assert!(order.is_empty() && groups.is_empty());
    }
}
