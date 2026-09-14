//! 整数キー用の軽量ハッシャ。
//!
//! 標準の `HashMap` / `HashSet` は暗号強度のある SipHash を使う。安全側に倒した
//! 妥当な既定だが、**毎サブステップ全頂点分のキーを引く**ような用途では重すぎる。
//! 空間ハッシュのセルキー `(i64, i64, i64)` を差し替えたときは、それだけで
//! 自己衝突のコストが 64% 減った。
//!
//! ここでは乗算とシフトだけで混ぜる。外部入力を受けるわけではないので、
//! ハッシュ衝突攻撃を気にする必要はない。

use std::hash::{BuildHasher, Hasher};

/// 乗数は splitmix64 由来。ビットをよく撹拌する。
const MIX: u64 = 0x9e37_79b9_7f4a_7c15;

#[derive(Default, Clone, Copy)]
pub struct FastHasher {
    state: u64,
}

impl FastHasher {
    #[inline]
    fn mix(&mut self, value: u64) {
        self.state = (self.state.rotate_left(5) ^ value).wrapping_mul(MIX);
    }
}

impl Hasher for FastHasher {
    #[inline]
    fn finish(&self) -> u64 {
        // バケット選択は下位ビットを見るので、上位を下位へ混ぜ込んでから返す
        let mut h = self.state;
        h ^= h >> 33;
        h = h.wrapping_mul(0xff51_afd7_ed55_8ccd);
        h ^= h >> 29;
        h
    }

    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        // 整数キーしか想定していないが、念のため一般のバイト列も扱えるようにする
        let mut chunks = bytes.chunks_exact(8);
        for chunk in &mut chunks {
            self.mix(u64::from_ne_bytes(chunk.try_into().unwrap()));
        }
        let rest = chunks.remainder();
        if !rest.is_empty() {
            let mut buf = [0u8; 8];
            buf[..rest.len()].copy_from_slice(rest);
            self.mix(u64::from_ne_bytes(buf));
        }
    }

    // 整数はバイト列を経由せず直接混ぜる(既定の実装は write() に落ちて遅い)
    #[inline]
    fn write_u32(&mut self, value: u32) {
        self.mix(value as u64);
    }

    #[inline]
    fn write_i32(&mut self, value: i32) {
        self.mix(value as u32 as u64);
    }

    #[inline]
    fn write_u64(&mut self, value: u64) {
        self.mix(value);
    }

    #[inline]
    fn write_i64(&mut self, value: i64) {
        self.mix(value as u64);
    }

    #[inline]
    fn write_usize(&mut self, value: usize) {
        self.mix(value as u64);
    }
}

#[derive(Default, Clone, Copy)]
pub struct FastHashBuilder;

impl BuildHasher for FastHashBuilder {
    type Hasher = FastHasher;

    #[inline]
    fn build_hasher(&self) -> FastHasher {
        FastHasher::default()
    }
}

pub type FastMap<K, V> = std::collections::HashMap<K, V, FastHashBuilder>;
pub type FastSet<K> = std::collections::HashSet<K, FastHashBuilder>;

#[cfg(test)]
mod tests {
    use super::*;

    /// 集合として正しく振る舞う(当たり前だが、自前ハッシャなので確かめる)
    #[test]
    fn set_behaves_correctly() {
        let mut set: FastSet<(u32, u32)> = FastSet::default();
        for a in 0..50u32 {
            for b in 0..50u32 {
                set.insert((a, b));
            }
        }
        assert_eq!(set.len(), 2500);
        assert!(set.contains(&(7, 13)));
        assert!(!set.contains(&(50, 0)));
    }

    /// 格子状のキーがバケットに偏らない
    ///
    /// 偏るとハッシュマップが連鎖して遅くなる。下位ビットだけを見て
    /// 分布を調べ、極端な偏りが無いことを確認する。
    #[test]
    fn grid_keys_spread_across_buckets() {
        use std::hash::BuildHasher;

        let builder = FastHashBuilder;
        let buckets = 256usize;
        let mut counts = vec![0usize; buckets];

        for x in -8..8i64 {
            for y in -8..8i64 {
                for z in -8..8i64 {
                    let h = builder.hash_one((x, y, z));
                    counts[(h as usize) % buckets] += 1;
                }
            }
        }

        let total: usize = counts.iter().sum();
        let ideal = total as f64 / buckets as f64;
        let worst = *counts.iter().max().unwrap() as f64;
        assert!(
            worst < ideal * 4.0,
            "バケットが偏っている: 最悪 {worst} / 理想 {ideal:.1}"
        );
        assert!(
            counts.iter().filter(|&&c| c == 0).count() < buckets / 4,
            "使われないバケットが多すぎる"
        );
    }
}
