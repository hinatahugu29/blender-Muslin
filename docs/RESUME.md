# 再開の手引き(別の環境で Pull したあと)

2026-10-08 時点。ここから作業を再開する人(や、会話の記録を持たないセッション)向け。
`main` を Pull すれば、コードと文書はすべて揃う。**物理コア(`cloth_core.pyd`)だけは Git に入っていない**ので、
最初にビルドする。

## 1. 最初にすること

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests   # cloth_core.pyd を作る(約 1 分)
python scripts\verify_core.py                                           # 物理コアと bpy 非依存の部分(269 項目)
```

必要なもの: Rust(rustup/cargo)、Python 3.11 以降、maturin(`python -m pip install maturin`)。
詳しくは [README.md](../README.md) の「ビルド」。2026-10-04 に、まっさらなクローンでこの手順を通し、
下の全テストが通ることを確認している。

Blender を入れた環境なら、アドオン層もヘッドレスで検査できる:

```bash
blender --background --factory-startup --python scripts/blender_selftest.py         # 443 項目(Blender 5.1)
blender --background --factory-startup --python scripts/blender_selftest_curve.py   # 269 項目(Curve Pattern)
cd rust/cloth_core && cargo test --no-default-features --release                    # Rust の単体テスト 91 件
```

- Blender 4.2 と 5.1 で確認している(`blender_selftest.py` は 5.1 / 5.2 で 443 項目、4.2 で 425 項目。4.2 は 5.x で
  保存したサンプルを開けないので、その分だけ少ない。`blender_selftest_curve.py` はどちらも 267 項目)
- 利用者の実機は Blender 5.2.2 LTS(GUI で確認するのはこちら)。この PC では Steam 版が
  `D:\SteamLibrary\steamapps\common\Blenderlender.exe` にあるので、**試験は 5.2 でも回す**
  (2026-10-10 に 5.1 にしか無いアイコン `SPLITSCREEN` を使い、5.2 でパネルが描けなかった。
  パネルを描く試験があるので、5.2 で回していれば捕まえられた)。
  **Blender を `--factory-startup` で起動するときは、環境変数 `BLENDER_USER_RESOURCES` を一時フォルダに
  向ける。** 向けないと、利用者が入れている拡張機能の部品(5.2 の `extensions/.local` の cloth_core)を
  「使われていない」と見なして退避してしまう(2026-10-10 に一度起きた。再起動で入れ直される)
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

直近の到達点(M10 はほぼ完了、M11 は第 2 段階まで。2026-10-09):

- M10 Curve Pattern: Curve を型紙の一次データにし、そこから布のメッシュを生成する(Shape Update /
  Rebuild Required / Rebuild)。Rebuild で姿勢とピン留めを引き継ぐ。布は既定で描いた平面のまま Curve の横に
  出す(向き・方向・隙間は Rebuild の欄と F9)。体の周りへの配置(`Arrange Around Collider`)、対応の表示、
  縫い目の溶接、縫い目をまたぐ曲げ制約。**GUI での確認も一通り済んだ**(2026-10-08、5.2.2)。
  その後に、選択の連動(Curve で選んだ区間に当たる布の辺を白く光らせる)、ピースの外周の常時表示
  (Curve を選んでいる間だけ薄く)、ゴム紐を Curve の区間で持つ(Rebuild で消えない)を足した(GUI 確認待ち)
- M11 クッション: 圧力の第 1 段階(一定の圧力)、2 枚を重ねる配置(`Stack Pieces for Bag`)、体なしで
  袋を閉じる手順(`Close Bag`)。圧力の第 2 段階として、目標の体積を保つ圧力と詰め具合(`Pressure Mode`
  = Volume、`Fill`)。飽和して見えていたのは、縫い目をまたぐ曲げ制約が袋の縁を押し開いていたためで、
  袋の縁の縫い目は「折り返し(Folded)」にして曲げ制約を外す。マチ(帯)で座布団・丸いクッションも
  作れる(`Add Gusset`、サンプル 08)。表の内側に描いた開いた線(内部線)で表と裏を縫い止める
  キルティング・ボタン留め(`Quilt Through`、サンプル 09)(いずれも GUI 確認待ち)

## 3. 次の候補

どれも ROADMAP に書いてある。優先順は利用者と相談して決める。

1. **GUI 確認**([CHECKPOINTS.md](../CHECKPOINTS.md) の 2026-10-08〜09 の項目)— キルティング、マチ、選択の連動、
   ピースの外周、Curve のゴム紐、詰め具合(Fill)、縫い目の折り返し、圧力の第 1 段階。良ければ作業ブランチを main へ
2. 2D ビューと 3D ビューの並置 — 試作した(`Pattern View`)。見え方と手触りの確認待ち
3. 速さ(2026-10-10 の改善後、Blender の中で約 3 万頂点・Normal): 自己衝突なし 約 62ms、あり 約 115ms。
   目標の 100ms まで自己衝突ありで あと 15ms。残りの余地は ROADMAP の M9 に
4. キルティングの続き: 1 点だけの留め(今は短い線で代用)

使い勝手の判断(2026-10-09、ROADMAP の M8): 型紙を伸ばしたら柄は実寸のまま広がるべきだが、**今は着手しない**
(必要になったら声がかかる)。型紙オブジェクトの並べ方は今のままでよい。線は今のところ問題なし。
残る判断待ち: Dress / Adjust の最中に Curve の変更が拒否されたときの表示

## 4. 気をつけること

- **コアを変えたら `build.ps1` でビルドし直す。** アドオン側だけ更新して古い `.pyd` を使うと、
  `set_seam_bending` のような新しい関数が無く落ちる
- 利用者は日本語で、コミットメッセージも日本語で書いている(「何をしたか」より「なぜ」を書く)
- 描画(ビューポートの線・文字)と操作の感触は、ヘッドレスでは確認できない。座標の計算までを自動で見て、
  見え方は CHECKPOINTS に項目を足して GUI で見てもらう
- 既知の制約: 重力 0・ピン無しの布が、低ポリの体のまわりで回り続けることがある(ピン留めがあれば出ない。
  CHECKPOINTS の「既知の制約」)
- `Reference/` は他社製アドオンの読み取り専用の複製で、Git には入っていない(学習用。再配布しない)
