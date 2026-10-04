# 再開の手引き(別の環境で Pull したあと)

2026-10-04 時点。ここから作業を再開する人(や、会話の記録を持たないセッション)向け。
`main` を Pull すれば、コードと文書はすべて揃う。**物理コア(`cloth_core.pyd`)だけは Git に入っていない**ので、
最初にビルドする。

## 1. 最初にすること

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests   # cloth_core.pyd を作る(約 1 分)
python scripts\verify_core.py                                           # 物理コアと bpy 非依存の部分(219 項目)
```

必要なもの: Rust(rustup/cargo)、Python 3.11 以降、maturin(`python -m pip install maturin`)。
詳しくは [README.md](../README.md) の「ビルド」。2026-10-04 に、まっさらなクローンでこの手順を通し、
下の全テストが通ることを確認している。

Blender を入れた環境なら、アドオン層もヘッドレスで検査できる:

```bash
blender --background --factory-startup --python scripts/blender_selftest.py         # 393 項目(Blender 5.1)
blender --background --factory-startup --python scripts/blender_selftest_curve.py   # 151 項目(Curve Pattern)
cd rust/cloth_core && cargo test --no-default-features --release                    # Rust の単体テスト 78 件
```

- Blender 4.2 と 5.1 で確認している(`blender_selftest.py` は 4.2 で 379 項目、5.1 で 393 項目。版によって走らない項目がある)
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

直近の到達点(M10 の大半が終わった状態):

- Curve を型紙の一次データにし、そこから布のメッシュを生成する(Shape Update / Rebuild Required / Rebuild)
- Rebuild で着せた姿勢とピン留めを引き継ぐ(型紙空間を介した補間)
- ピースを体の周りへ巻き付けて置く(`Arrange Around Collider`)
- Curve と布の対応の表示(縫い目ごとの色・ピース番号)
- 縫い目の溶接(`Weld Seams`)、縫い目をまたぐ曲げ制約(硬い生地を高品質で解いたときの蝶番を防ぐ)

## 3. 次の候補

どれも ROADMAP に書いてある。優先順は利用者と相談して決める。

1. **GUI での確認**([CHECKPOINTS.md](../CHECKPOINTS.md))— Undo / Redo、ペンツール、保存して開き直したあと、
   縫い目の曲げ抵抗の見え方。結果しだいで直すところが出る
2. 選択の連動(Curve で選んだ区間に対応する布の辺を光らせる) — 対応の表示の続き
3. ゴム紐の Curve 側への移行(縫い目と同じ形)
4. **M11: クッション・パファー**(圧力の第 1 段階 → 2 枚を重ねる配置 → 体積を考える圧力)。整理は ROADMAP の M11
5. 2D ビューと 3D ビューの並置(MD に近い体験。画面の見え方が中心なので、手触りの確認が要る)

## 4. 気をつけること

- **コアを変えたら `build.ps1` でビルドし直す。** アドオン側だけ更新して古い `.pyd` を使うと、
  `set_seam_bending` のような新しい関数が無く落ちる
- 利用者は日本語で、コミットメッセージも日本語で書いている(「何をしたか」より「なぜ」を書く)
- 描画(ビューポートの線・文字)と操作の感触は、ヘッドレスでは確認できない。座標の計算までを自動で見て、
  見え方は CHECKPOINTS に項目を足して GUI で見てもらう
- 既知の制約: 重力 0・ピン無しの布が、低ポリの体のまわりで回り続けることがある(ピン留めがあれば出ない。
  CHECKPOINTS の「既知の制約」)
- `Reference/` は他社製アドオンの読み取り専用の複製で、Git には入っていない(学習用。再配布しない)
