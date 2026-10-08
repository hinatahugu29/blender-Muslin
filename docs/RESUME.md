# 再開の手引き(別の環境で Pull したあと)

2026-10-08 時点。ここから作業を再開する人(や、会話の記録を持たないセッション)向け。
`main` を Pull すれば、コードと文書はすべて揃う。**物理コア(`cloth_core.pyd`)だけは Git に入っていない**ので、
最初にビルドする。

## 1. 最初にすること

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests   # cloth_core.pyd を作る(約 1 分)
python scripts\verify_core.py                                           # 物理コアと bpy 非依存の部分(229 項目)
```

必要なもの: Rust(rustup/cargo)、Python 3.11 以降、maturin(`python -m pip install maturin`)。
詳しくは [README.md](../README.md) の「ビルド」。2026-10-04 に、まっさらなクローンでこの手順を通し、
下の全テストが通ることを確認している。

Blender を入れた環境なら、アドオン層もヘッドレスで検査できる:

```bash
blender --background --factory-startup --python scripts/blender_selftest.py         # 407 項目(Blender 5.1)
blender --background --factory-startup --python scripts/blender_selftest_curve.py   # 184 項目(Curve Pattern)
cd rust/cloth_core && cargo test --no-default-features --release                    # Rust の単体テスト 83 件
```

- Blender 4.2 と 5.1 で確認している(`blender_selftest.py` は 4.2 で 393 項目、5.1 で 407 項目。4.2 は 5.x で
  保存したサンプルを開けないので、その分だけ少ない。`blender_selftest_curve.py` はどちらも 184 項目)
- 利用者の実機は Blender 5.2.2 LTS(GUI で確認するのはこちら)
- **`samples/*.blend` は `blender_selftest.py` が全部開いて検査する。** 作業用の .blend を `samples/` に置かない
  (調査用のファイルは `scripts/spikes/data/` へ)

## 2. 今どこにいるか

| 何を見る | どこ |
|---|---|
| 全体の進捗(マイルストーン M0〜M11) | [ROADMAP.md](../ROADMAP.md) |
| GUI で確認する項目と、既知の制約 | [CHECKPOINTS.md](../CHECKPOINTS.md)(先頭の「手作業で残っているもの」) |
| Curve Pattern の設計と実装状況 | [docs/design/curve_pattern.md](design/curve_pattern.md) |
| Blender の Curve の点の同一性の実測 | [docs/design/curve_spike_results.md](design/curve_spike_results.md) |
| 仕様 | [SPEC.md](../SPEC.md) |

直近の到達点(M10 はほぼ完了、M11 は第 1 段階まで):

- M10 Curve Pattern: Curve を型紙の一次データにし、そこから布のメッシュを生成する(Shape Update /
  Rebuild Required / Rebuild)。Rebuild で姿勢とピン留めを引き継ぐ。布は既定で描いた平面のまま Curve の横に
  出す(向き・方向・隙間は Rebuild の欄と F9)。体の周りへの配置(`Arrange Around Collider`)、対応の表示、
  縫い目の溶接、縫い目をまたぐ曲げ制約。**GUI での確認も一通り済んだ**(2026-10-08、5.2.2)
- M11 クッション: 圧力の第 1 段階(一定の圧力)、2 枚を重ねる配置(`Stack Pieces for Bag`)、体なしで
  袋を閉じる手順(`Close Bag`)。クッションが一通り作れる。**一定の圧力では膨らみきって飽和する**ので、
  圧力の値でふくらみ具合を決められない(次の課題)

## 3. 次の候補

どれも ROADMAP に書いてある。優先順は利用者と相談して決める。

1. **M11 の GUI 確認**([CHECKPOINTS.md](../CHECKPOINTS.md) の「圧力で増えた確認項目」)— Pressure の欄、
   強すぎる圧力の警告、平らな布の膨らむ向き、クッションを一通り作る
2. **M11 圧力の第 2 段階(体積)** — `p = p0 × V0 / V` で、膨らむほど圧力を下げて狙った体積で止める
3. 選択の連動(Curve で選んだ区間に対応する布の辺を光らせる) — 対応の表示の続き
4. ゴム紐の Curve 側への移行(縫い目と同じ形)
5. マチ(帯状のピース)、内部線(キルティング・タフティング)
6. 2D ビューと 3D ビューの並置(MD に近い体験。画面の見え方が中心なので、手触りの確認が要る)

使い勝手の判断待ち(ROADMAP の M8「未決」、CHECKPOINTS): 型紙オブジェクトを伸ばしたときの柄、
型紙オブジェクトのピースの並べ方、Dress / Adjust の最中に Curve の変更が拒否されたときの表示

## 4. 気をつけること

- **コアを変えたら `build.ps1` でビルドし直す。** アドオン側だけ更新して古い `.pyd` を使うと、
  `set_seam_bending` のような新しい関数が無く落ちる
- 利用者は日本語で、コミットメッセージも日本語で書いている(「何をしたか」より「なぜ」を書く)
- 描画(ビューポートの線・文字)と操作の感触は、ヘッドレスでは確認できない。座標の計算までを自動で見て、
  見え方は CHECKPOINTS に項目を足して GUI で見てもらう
- 既知の制約: 重力 0・ピン無しの布が、低ポリの体のまわりで回り続けることがある(ピン留めがあれば出ない。
  CHECKPOINTS の「既知の制約」)
- `Reference/` は他社製アドオンの読み取り専用の複製で、Git には入っていない(学習用。再配布しない)
