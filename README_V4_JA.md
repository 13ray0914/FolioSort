# FolioSort v4 詳細ガイド

このv4は、大学から正規に取得した**手元の査読済みPDFだけ**を対象にする設計です。Webから論文PDFを自動探索・自動取得する機能は追加していません。CrossrefとOpenAlexは、PDF取得ではなく、書誌情報・DOI・引用関係を補完するためにだけ使用します。

## v4で追加されるもの

1. Crossref/OpenAlexによる title、year、journal、DOI、OpenAlex ID の補完
2. 参考文献とローカルPDFの同一視
   - DOI完全一致
   - OpenAlex ID一致
   - title + first author + year の高閾値照合
3. figure、graph、scheme、table、equationの抽出
   - GROBID座標
   - PyMuPDFによるcrop
   - PyMuPDF table extraction
   - 任意のMinerUによるtable/formula/image/OCR補完
   - 任意の別vision serverによる画像内容の構造化解析
4. whole-paper summary memory → chunk extraction の二階層処理
5. SPECTER2 embedding
6. citation / semantic / property / method / bibliographic-coupling の別layer
7. multiplex Leiden clustering
8. Paper graphとは別のscientific knowledge graph
9. claim間の supports / contradicts / qualifies / extends 等の候補推論

---

# 1. 既存データの扱い

インストーラーは以下を維持します。

- `data/raw_pdfs/` 内のPDF
- `data/manifest.csv`
- P0001、P0002…というstable paper ID
- 既存SQLite DB
- 既存のTEI、JSON、inventory、evidence

また、更新前の主要ファイルを次へバックアップします。

```text
~/desktop/review/backups/v4_YYYYMMDD_HHMMSS/
```

`raw_pdfs/` は大きく、原本でもあるため、インストーラーはコピーも変更もしません。

## 最初のv4実行で再処理される理由

v4では、figure/table/formulaの座標を増やし、whole-paper memoryを詳細抽出より先に生成し、visual evidence IDを導入しています。そのため、最初のv4実行では原則として次を一度だけ再生成します。

```text
PDF
→ GROBID TEI
→ stable text/visual JSON
→ metadata enrichment
→ visual assets
→ whole-paper summary memory
→ inventory v4
→ evidence v4
→ validation v4
→ reference resolution
→ report v4
```

これは文脈損失とprovenanceの不整合を避けるためです。v3の抽出データはバックアップへ残ります。v4の初回再処理後は、PDF・prompt・schema・model・入力hashが変わらない限り、通常どおり差分処理されます。

---

# 2. インストール

WindowsのDownloadsへZIPを保存した例です。

```bash
cd ~/desktop
rm -rf review_pipeline_extension_v4
unzip /mnt/c/Users/Rei/Downloads/review_pipeline_extension_v4.zip
cd review_pipeline_extension_v4
chmod +x install_extension.sh
./install_extension.sh
```

ネットワーク環境は独立した `.venv_network` に自動インストールされます。これにより、既存の化学計算用Python環境にあるNumPy等との衝突を避けます。

SPECTER2も同時にインストールする場合は、次のようにできます。

```bash
REVIEW_INSTALL_SPECTER2=1 ./install_extension.sh
```

MinerUまで同時に導入する場合は、依存関係とモデル容量が大きいため、明示的に有効化します。

```bash
REVIEW_INSTALL_MINERU=1 ./install_extension.sh
```

通常は、まずv4本体だけを入れ、SPECTER2とMinerUを後から個別導入する方が安全です。

---

# 3. CrossrefとOpenAlexの設定

## Crossref

Crossrefはkeyなしで使用できますが、連絡可能なメールアドレスを付けるpolite accessが推奨されます。

```bash
export CROSSREF_MAILTO="あなたの大学メールアドレス"
```

## OpenAlex

少数の試行はkeyなしでも可能ですが、57報とそのreferenceを処理するなら、無料API keyの使用を強く推奨します。

```bash
export OPENALEX_API_KEY="あなたのOpenAlex API key"
export OPENALEX_MAILTO="あなたの大学メールアドレス"
```

毎回設定しなくてよいように、`~/.bashrc` の末尾へ追加できます。

```bash
nano ~/.bashrc
```

末尾へ以下を追加します。

```bash
export CROSSREF_MAILTO="あなたの大学メールアドレス"
export OPENALEX_API_KEY="あなたのOpenAlex API key"
export OPENALEX_MAILTO="あなたの大学メールアドレス"
```

反映します。

```bash
source ~/.bashrc
```

Crossref/OpenAlexの全responseはSQLiteの `api_cache_v4` に保存されます。同一queryを毎回APIへ送り直しません。

---

# 4. SPECTER2のインストール

一度だけ実行します。

```bash
cd ~/desktop/review
./scripts/install_specter2_env.sh
```

次の実行時、Hugging FaceからSPECTER2のbase modelとproximity adapterが取得されます。

```bash
./scripts/12_build_embeddings.sh
```

入力は原則として、

```text
title + [SEP] + abstract
```

です。abstractがない古い論文では、v4のwhole-paper summary memoryから短い代替abstractを作ります。

設定は `config.json` の `embedding` にあります。

```json
{
  "embedding": {
    "enabled": true,
    "device": "cpu",
    "batch_size": 8,
    "max_length": 512,
    "base_model": "allenai/specter2_base",
    "adapter_model": "allenai/specter2",
    "adapter_name": "proximity"
  }
}
```

4090を使って高速化する場合は、SPECTER2環境にCUDA対応PyTorchが入っていることを確認した上で、

```json
"device": "cuda"
```

へ変更します。CPUでも57報なら実用的です。

---

# 5. graph、scheme、table、equationの処理

v4では3段階を用意しています。

## Level A: デフォルト、追加モデル不要

- GROBIDからfigure/table/formulaの座標とcaptionを取得
- PyMuPDFで該当領域をPNG crop
- tableについてはTEI rowsとPyMuPDF table extractionを保存
- equationについてはGROBIDの式テキストを保存
- `vis:fig0001`、`vis:table0001`、`vis:eq0001` のようなstable visual evidence IDを付与

出力例：

```text
data/visual_assets/P0001/fig0001.png
data/visual_assets/P0001/table0001.png
data/visual_analysis/P0001.visual.json
```

Level Aだけでも、caption、table text、equation textをsummary memoryやevidence extractionに渡せます。ただし、graphの曲線形状やchemical schemeの構造そのものを意味理解するにはvision modelが必要です。

## Level B: MinerU、任意

MinerUはtableをHTML、formulaをLaTeX、image/chartを独立要素として補完する用途に使います。インストールスクリプトは現在のMinerU 3.x系に合わせて`mineru[all]`を独立環境へ導入し、formula/tableを環境変数で明示的に有効化します。

```bash
cd ~/desktop/review
./scripts/install_mineru_env.sh
./scripts/enable_mineru.sh
```

状態確認：

```bash
./scripts/status_review_services.sh
```

MinerUを無効に戻す場合：

```bash
./scripts/disable_optional_visual_tools.sh
```

MinerUは比較的大きな環境です。初回モデル取得と解析には時間がかかります。デフォルトbackendは表・数式・画像領域の構造化を主目的とし、画像やチャートの意味解釈は別のvision modelで補います。MinerU側のVLM/hybrid image analysisを使う場合は、`config.json`の`visual.mineru.backend`を`hybrid-engine`等へ変え、`effort`を`high`にしてください。

## Level C: 別のmultimodal llama-server、任意

現在port 8080で使っているQwen3.8-27Bのtext GGUFは、起動コマンドにmultimodal projectorがなく、そのままでは画像を見ません。

画像解析を有効にするには、vision対応GGUFと対応する`mmproj`を別server、たとえばport 8081で起動します。

ローカルファイルを使う場合：

```bash
export VISION_MODEL="$HOME/models/<vision-model>.gguf"
export VISION_MMPROJ="$HOME/models/<matching-mmproj>.gguf"
cd ~/desktop/review
./scripts/start_vision_server.sh
./scripts/enable_vision_llm.sh
```

llama.cppが対応するHugging Face repositoryを直接使う場合：

```bash
export VISION_HF_REPO="ggml-org/<supported-multimodal-GGUF-repository>"
cd ~/desktop/review
./scripts/start_vision_server.sh
./scripts/enable_vision_llm.sh
```

停止：

```bash
./scripts/stop_vision_server.sh
```

Vision modelには、caption、近傍sentence、GROBID/PyMuPDF/MinerUのstructured textも一緒に渡します。画像だけから自由に推測させる設計ではありません。chemical schemeについては反応の概略・矢印・ラベル・比較関係を抽出できますが、構造式を厳密なSMILESへ変換するOCSRは別問題です。厳密な構造同定が必要な図は、元PDFまたは専門OCSRで人手確認してください。

---

# 6. Whole-paper summary memory

v4では、いきなり各chunkからinventory/evidenceを作りません。

```text
全論文をadaptive chunkで読む
→ 各chunkのevidence-linked summary
→ 階層的にmerge
→ whole-paper summary memory
→ memoryを各詳細chunkへ添付
→ inventory/evidence extraction
```

summary memoryには以下を保存します。

- central question
- study design
- systems overview
- method map
- definitions
- major findings
- interpretations
- limitations
- global constraints
- cross-chunk dependencies

重要なルールとして、memoryは方向づけにだけ使います。詳細なmeasurementやclaimは、元のsentence IDまたはvisual evidence IDを根拠として持たなければ採用しません。

出力：

```text
data/summary_memory/P0001.memory.json
```

---

# 7. ReferenceとローカルPDFの同一視

`10_resolve_references.py` は次の順で照合します。

1. DOI完全一致
2. OpenAlex ID一致
3. title + first author + year + journalのスコア
4. OpenAlex `referenced_works` による補助
5. unresolved referenceをCrossrefで補完
6. 解決したDOI/OpenAlex IDを再度ローカルPDFへ照合

高閾値で自動acceptし、曖昧な候補は無理に同一視しません。

出力：

```text
data/reference_matches/P0001.references.json
```

この情報から、direct citation layerとbibliographic coupling layerを作ります。

---

# 8. Multiplex graphとLeiden clustering

v4では関係を1つの類似度へ早期に潰さず、次のlayerを独立保持します。

```text
citation
semantic (SPECTER2)
property
method
bibliographic_coupling
```

各layerのedgeとweightを保持したまま、multiplex Leidenで共通membershipを最適化します。

設定例：

```json
{
  "multiplex_graph": {
    "layer_weights": {
      "citation": 1.35,
      "semantic": 0.8,
      "property": 0.45,
      "method": 0.35,
      "bibliographic_coupling": 0.55
    },
    "clustering": {
      "resolution": 1.0,
      "seed": 42
    }
  }
}
```

細かいclusterにするには`resolution`を上げ、大きなclusterにまとめるには下げます。

出力：

```text
outputs/network_gui/network.html
outputs/network_gui/network.json
outputs/network_gui/nodes.csv
outputs/network_gui/edges.csv
outputs/network_gui/network.graphml
```

GUIを開く：

```bash
./scripts/open_network_gui.sh
```

GUIではlayerごとの表示・非表示、cluster filter、paper検索、ノード詳細表示ができます。

---

# 9. Scientific knowledge graph

Paper graphとは別に、以下のnodeを作ります。

```text
paper
claim
measurement
property
method
system
visual
```

主なedge：

```text
CITES
HAS_CLAIM
REPORTS_MEASUREMENT
STUDIES_PROPERTY
USES_METHOD
STUDIES_SYSTEM
MEASURES_PROPERTY
MEASURED_ON
CLAIM_ABOUT_PROPERTY
CLAIM_ABOUT_SYSTEM
HAS_VISUAL
SUPPORTED_BY_VISUAL
```

さらにQwenに候補claim pairだけを渡し、次を推論できます。

```text
supports
contradicts
qualifies
extends
same_observation_different_interpretation
not_directly_comparable
```

この関係は著者が明示したrelationではなく、**model-inferred relation**です。元claim、conditions、system、method、property、evidence excerptを一緒に渡し、confidence threshold未満は採用しません。

デフォルトでは有効です。

```json
{
  "knowledge_graph": {
    "infer_claim_relations": true,
    "relation_min_confidence": 0.75
  }
}
```

無効にする場合：

```json
"infer_claim_relations": false
```

一回だけ無効にして作る場合：

```bash
./scripts/14_build_knowledge_graph.sh --no-claim-relations
```

出力：

```text
outputs/knowledge_graph/knowledge.html
outputs/knowledge_graph/knowledge.json
outputs/knowledge_graph/knowledge_nodes.csv
outputs/knowledge_graph/knowledge_edges.csv
outputs/knowledge_graph/knowledge.graphml
```

GUIを開く：

```bash
./scripts/open_knowledge_gui.sh
```

---

# 10. 全処理の実行

まず、text QwenとDocker Desktopが利用可能な状態で実行します。

```bash
cd ~/desktop/review
./scripts/run_review_pipeline.sh
```

実行順：

```text
1 manifest
2 GROBID + visual coordinates
3 TEI → stable text/visual JSON
4 Crossref/OpenAlex metadata
5 figure/table/equation extraction
6 whole-paper summary memory
7 memory-aware inventory
8 memory-aware evidence
9 validation
10 reference/local-PDF resolution
11 human-readable report
12 SPECTER2 embedding
13 multiplex Leiden graph
14 knowledge graph
```

初回だけは57報の再処理になるため、かなり時間がかかります。途中で停止しても、成功済みstage、adaptive child chunk、summary merge、API response、SPECTER2 embeddingは再利用されます。

進捗ログ：

```bash
tail -f ~/desktop/review/logs/auto_pipeline_$(date +%Y%m%d).log
```

状態確認：

```bash
./scripts/status_review_services.sh
```

サービス停止：

```bash
./scripts/stop_review_services.sh
```

---

# 11. 一部機能を飛ばす

SPECTER2をまだ導入していない場合：

```bash
REVIEW_SKIP_EMBEDDINGS=1 ./scripts/run_review_pipeline.sh
```

GUI/graphを後で作る場合：

```bash
REVIEW_SKIP_NETWORKS=1 ./scripts/run_review_pipeline.sh
```

両方：

```bash
REVIEW_SKIP_EMBEDDINGS=1 REVIEW_SKIP_NETWORKS=1 ./scripts/run_review_pipeline.sh
```

コア抽出だけ先に完了できます。

---

# 12. 特定paperだけ処理

```bash
source .venv/bin/activate
python run_pipeline.py --from-step 2 --to-step 11 --ids P0001,P0010
```

特定stepだけ：

```bash
python scripts/04_enrich_metadata.py --ids P0001
python scripts/05_extract_visual_assets.py --ids P0001
python scripts/06_build_summary_memory.py --ids P0001
python scripts/10_resolve_references.py --ids P0001
```

APIを使わずローカルreference matchingだけ行う場合：

```bash
python scripts/10_resolve_references.py --ids P0001 --local-only
```

---

# 13. Autostart

手動実行が最後まで通ることを確認してから設定します。

```bash
./scripts/install_autostart.sh
```

今後Ubuntu/WSLを最初に開いた時に、同じ`run_review_pipeline.sh`がバックグラウンドで動きます。`flock`により処理の二重起動を防ぎます。

解除：

```bash
./scripts/uninstall_autostart.sh
```

---

# 14. 重要な限界

- metadata matchingとreference matchingは誤照合の可能性があるため、`manual_review`または低confidenceを確認してください。
- GROBID/PyMuPDFだけではgraphやchemical schemeの意味内容を完全には理解できません。必要な論文にはMinerUまたはvision modelを有効化してください。
- visual modelの出力も必ず元画像と照合してください。
- summary memoryは文脈保持を改善しますが、詳細claimの根拠には使いません。
- claim-to-claim relationはモデル推論であり、著者の明示的な結論と区別してください。
- SPECTER2はtitle+abstractを中心にしたpaper-level embeddingです。property/method/citation layerと併用することで補完します。
- `network.html`と`knowledge.html`は、local vis-network JSがない場合、表示時にCDNへアクセスします。CSV、JSON、GraphMLは完全にローカルです。

---

# 15. 推奨する最初の移行手順

```bash
cd ~/desktop/review

# API設定を確認
source ~/.bashrc

# SPECTER2環境を一度だけ作る
./scripts/install_specter2_env.sh

# 環境確認
./scripts/status_review_services.sh
python check_environment.py

# 初回v4再処理
./scripts/run_review_pipeline.sh
```

最初はMinerUとvision modelを無効のまま実行し、text、metadata、summary memory、SPECTER2、multiplex graph、knowledge graphまで確認するのが安全です。その後、図表が重要な代表論文数報でMinerU/vision解析を試し、精度を見てから全57報へ広げてください。
