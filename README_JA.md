# ローカルLLM文献レビュー・パイプライン（PEG profile）

このプロジェクトは、手元のPDFを次の順に処理するためのものです。

```text
PDF追加
  -> SHA-256で新規/変更検出
  -> GROBIDでTEI XML化
  -> stable sentence ID付きJSON化
  -> Qwenで論文inventory抽出
  -> Qwenでmeasurement / atomic claim / citation context抽出
  -> Pythonで機械的検証
  -> 人間用review report生成
  -> human approval
```

**PDFのファイル名は科学情報として一切利用しません。**
現在の「年 + journal名 + 日本語の簡単な要約」というファイル名は、そのままで構いません。日本語要約をQwenへ渡すこともありません。ファイル名は、元PDFを人間が識別するための `original_filename` としてのみ保存されます。

---

## pipでインストールする

FolioSortはWSL2/UbuntuまたはLinuxのPython 3.10以上へ、GitHubから直接pip installできます。POSIXのプロセス制御とファイルロックを使用するため、WindowsネイティブPythonではなくWSL/Linux内で実行してください。

最初に[OpenAlexの設定ページ](https://openalex.org/settings/api)で無料アカウントを作成し、API keyを取得してください。keyなしで利用できる1日あたりの上限は少なく、多数の論文を処理すると`429 Too Many Requests`でmetadata取得が止まることがあります。無料API keyを使うと1日の利用枠が増えます。詳細は[OpenAlex公式の認証・rate limitガイド](https://help.openalex.org/api/authentication/)を参照してください。

```bash
python3 -m venv ~/.venvs/foliosort
source ~/.venvs/foliosort/bin/activate
python -m pip install --upgrade pip
python -m pip install "foliosort @ git+https://github.com/13ray0914/FolioSort.git@main"

foliosort --version
foliosort init ~/desktop/review
cd ~/desktop/review
```

`foliosort init`の途中で、取得したOpenAlex API keyを入力します。入力内容は画面に表示されません。

```text
Create a free OpenAlex account and copy your API key:
  https://openalex.org/settings/api
OpenAlex API key (input hidden; Enter to configure later):
```

API keyはGitの管理対象外であるlocalの`config.json`だけに保存され、logや画面には出力されません。`foliosort init`は実行用workspaceを作り、`config.example.json`から`config.json`を生成します。既存workspaceのapplication部分だけを更新する場合は、upgrade後に次を実行します。研究data、生成物、既存の`config.json`、保存済みAPI keyは保持されます。

```bash
foliosort init ~/desktop/review --force
```

自動構築など対話入力できない環境では、`foliosort init PATH --no-openalex-prompt`を使用し、`foliosort serve`を実行する環境に`OPENALEX_API_KEY`を設定してください。

graph用の分離環境とGROBIDを準備し、Qwenを起動・設定した後に確認して起動します。

```bash
./scripts/install_network_env.sh
docker compose -f docker-compose.grobid.yml up -d
foliosort check
foliosort serve
```

browserで `http://127.0.0.1:8766` を開きます。core pipelineだけを直接実行する場合は、たとえば `foliosort pipeline --from-step 6 --to-step 11` を使用できます。開発用のlocal checkoutでは `python -m pip install -e .` も利用できます。

### ローカルQwen実行環境

現在の自動起動設定は、Qwen3.8-27B Q4_K_M（約17GB）とQ4_0 MTP draft（約1.6GB）、GPU全層offload、Flash Attention、1 slot、65,536 token contextを使用します。[Qwen公式model card](https://huggingface.co/Qwen/Qwen3.8-27B)では27B dense model、native context 262,144 tokenとされていますが、FolioSortはmemoryを抑えるため65,536 tokenに設定しています。

| 項目 | 現在の既定構成の実用上の最低 | 推奨 | 補足 |
|---|---:|---:|---|
| GPU | CUDA対応NVIDIA GPU | 新しい世代のNVIDIA GPU | CPUのみ、または一部CPU offloadも可能ですが、解析は大幅に遅くなります。 |
| VRAM | 24GB | 32GB以上 | RTX 4090での実測は約23.3/24.0GiBでした。画面表示や他process用の余裕を考えると32GB以上が安全です。 |
| RAM | 32GB | 64GB以上 | CPU offload、別のlocal model、大量のPDFを同時に扱う場合は64GB以上を推奨します。 |
| 空き容量 | 25GB（llama.cppと2つのGGUF） | 40GB以上＋論文/output用領域 | GROBID、embedding、抽出画像、backupには追加容量が必要です。 |
| CPU | AVX2対応x86-64、8 core | 12 core以上 | PDF解析、検証、graph生成、CPU offloadに使用します。 |

必要量はGGUF量子化、llama.cppの版、context長、KV cache形式、batch size、GPUの画面使用量で変化します。24GBで不足する場合は、まず他のGPU applicationを終了し、その後 `scripts/run_review_pipeline.sh` の `-c 65536` を `-c 32768` に下げてください。それより短くすると、長い論文のrequestが収まらない場合があります。

modelや実行fileの場所は環境変数で変更できます。

```bash
export QWEN_SERVER="$HOME/desktop/llm/llama.cpp/build/bin/llama-server"
export QWEN_MODEL="$HOME/models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf"
export QWEN_DRAFT_MODEL="$HOME/models/Qwen3.8-27B/mtp-Qwen3.8-27B-Q4_0.gguf"
```

Qwenのweight、GGUF、llama.cpp、Docker自体はFolioSortのpip packageには含まれません。

---

## 0. なぜこの構成にしているか

大量論文のレビューで最も危険なのは、「LLMが読みやすい要約を作ったが、その一文が元論文のどこに書いてあったか追えない」状態です。

このパイプラインでは、本文中の各文に `s000001` のようなIDを付け、Qwenが抽出するmeasurementやclaimに必ず `evidence_sids` を付けます。

例:

```json
{
  "claim_id": "C0007",
  "statement": "The hydration behavior changed with PEG chain length.",
  "evidence_sids": ["s000381", "s000382"]
}
```

そのため、後から

```text
総説の一文
 -> claim ID
 -> evidence sentence ID
 -> 元PDF
```

と逆向きに追跡できます。

---

# 1. 初回セットアップ

プロジェクトディレクトリへ移動します。

```bash
cd peg_literature_pipeline
```

Python仮想環境を作ります。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`config.json` はすでにサンプル値で用意されています。必要なら `config.example.json` から作り直せます。

```bash
cp config.example.json config.json
```

---

# 2. GROBIDを起動する

GROBIDはPDFを、title / abstract / section / paragraph / sentence / referenceを保持したTEI XMLへ変換します。

Dockerが使える場合:

```bash
docker compose -f docker-compose.grobid.yml up -d
```

確認:

```bash
curl http://127.0.0.1:8070/api/isalive
```

`true` と出ればOKです。

停止するとき:

```bash
docker compose -f docker-compose.grobid.yml down
```

---

# 3. Qwenをllama.cpp serverとして起動する

このパイプラインはOpenAI互換の `/v1/chat/completions` を使います。

すでにQwen GGUFを動かしている場合、推論条件はなるべくそのまま使い、CLIではなく `llama-server` で起動してください。

概念例:

```bash
/path/to/llama-server \
  -m /path/to/your/Qwen3.8-27B-Q4_K_M.gguf \
  --host 127.0.0.1 \
  --port 8080 \
  --alias literature-qwen \
  --jinja \
  -c 32768
```

GPU offload等は、現在Qwenを正常に動かせている設定に合わせて追加してください。

`config.json` の既定値は以下を見に行きます。

```json
"base_url": "http://127.0.0.1:8080/v1"
```

モデル名を `"auto"` にしてあるため、`/v1/models` からロード済みモデルIDを自動取得します。

確認:

```bash
curl http://127.0.0.1:8080/v1/models
```

---

# 4. 環境チェック

GROBIDとQwenの両方を起動した状態で:

```bash
python check_environment.py
```

すべて `[OK]` になれば開始できます。

---

# 5. 約70報のPDFを入れる

現在のPDF名は変更しなくて構いません。

すべてを:

```text
data/raw_pdfs/
```

へコピーしてください。サブフォルダを作っても構いません。

例:

```text
data/raw_pdfs/
  1974_Journal_水中コンフォメーション.pdf
  1998_Macromolecules_PEG水和.pdf
  2021_JPCB_THzによる水和解析.pdf
  review/
    2023_Review_PEG応用総説.pdf
```

日本語要約部分は無視されます。

---

# 6. Script 01: PDF登録とmanifest作成

実行:

```bash
python scripts/01_make_manifest.py
```

このscriptは:

1. `data/raw_pdfs/` 内のPDFを再帰的に検索
2. 各PDFのSHA-256を計算
3. 新規PDFに `P0001`, `P0002` ... のstable IDを付与
4. SQLiteへ登録
5. `data/manifest.csv` を出力

します。

## 重要

P0001.pdfへリネームする必要はありません。

例えば:

```text
original_filename: 2021_JPCB_THzによる水和解析.pdf
paper_id: P0027
```

という対応だけをSQLite/manifestに保存します。

`data/manifest.csv` をExcel等で開くと、どの論文がどのP-IDになったか確認できます。

### 後からPDFを追加した場合

新しいPDFを `data/raw_pdfs/` に入れて、もう一度:

```bash
python scripts/01_make_manifest.py
```

とするだけです。

- 同じPDF: 何もしない
- 新規PDF: 新しいP-IDを付与
- 同じ内容でファイル名だけ変更: 元のP-IDを維持
- 同じpathのPDFを別版に差し替え: SHA-256変化を検出し、その論文の下流処理をreset
- 完全に同一内容の重複PDF: duplicateとしてskip

となります。

---

# 7. まず10報だけpilotにする

最初から70報全部をQwenに処理させないでください。

`data/manifest.csv` を見て、内容をよく知っている10報を選びます。

例:

```text
P0002
P0005
P0008
P0011
P0017
P0021
P0030
P0042
P0055
P0068
```

以後:

```bash
--ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

と指定します。

---

# 8. Script 02: PDF -> GROBID TEI XML

pilot 10報なら:

```bash
python scripts/02_grobid_parse.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
data/tei/P0002.tei.xml
...
```

ここではGROBIDに:

- full text extraction
- sentence segmentation
- unique IDs
- raw citation strings
- sentence coordinates
- figure/table coordinates
- bibliography coordinates

を要求しています。

失敗した論文だけSQLiteで `error` になります。他の論文は継続できます。

---

# 9. Script 03: TEI XML -> Qwen用JSON

```bash
python scripts/03_tei_to_json.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
data/paper_json/P0002.json
```

JSONには:

```text
metadata
abstract
sections
  paragraphs
    sentences
references
figures
tables
auxiliary_text
```

が入ります。

本文中の文には独自のstable IDを振ります。

```json
{
  "sid": "s000381",
  "page": 6,
  "citation_ref_ids": ["b12", "b13"],
  "text": "..."
}
```

Figure captionとGROBIDが回収したTable textにも:

```text
figcap0001
table0001
```

のようなevidence IDを付けます。

### この段階で一度確認すること

pilotのうち2〜3報について `data/paper_json/Pxxxx.json` を開き、

- titleが正しい
- abstractが入っている
- Results/Discussionが入っている
- referencesがある
- 本文が文字化けしていない

ことを確認してください。

---

# 10. Script 04: QwenでPaper Inventoryを抽出

```bash
python scripts/04_extract_inventory.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
data/extracted/P0002.inventory.json
```

PEG profileでは主に:

- article type
- objective
- PEG / PEO / OEG system
- degree of polymerization
- Mn
- Mw
- dispersity
- end groups
- topology
- architecture
- monodispersity status
- methods
- studied properties
- global conditions

を抽出します。

例:

```json
{
  "system_id": "SYS001",
  "system_name_raw": "tetraethylene glycol dimethyl ether",
  "normalized_name": "tetraethylene glycol dimethyl ether",
  "attributes": {
    "peg_family": "OEG",
    "dp_raw": "4",
    "dp_min": 4,
    "dp_max": 4,
    "mn_raw": null,
    "mw_raw": null,
    "dispersity_raw": null,
    "end_groups": ["methyl", "methyl"],
    "topology": "linear",
    "architecture": null,
    "monodispersity_status": "discrete oligomer",
    "other_descriptors": []
  },
  "evidence_sids": ["s000122"]
}
```

## chunk処理

長い論文を一度に丸ごと投げず、paragraph境界で約 `30000 characters` ごとに分割します。

設定は:

```json
"chunk_max_chars": 30000
```

で変更できます。

途中で止まっても、完成済みchunkは:

```text
data/llm_raw/P0002/inventory/
```

から再利用されます。

---

# 11. Script 05: measurement / claim / citation context抽出

```bash
python scripts/05_extract_evidence.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
data/extracted/P0002.evidence.json
```

## measurements

```json
{
  "measurement_id": "M0001",
  "property_normalized": "hydration_number",
  "value_raw": "1.3 water molecules per EO unit",
  "parsed_value": 1.3,
  "unit_raw": "water molecules per EO unit",
  "conditions_text": "at 298 K",
  "system_refs": ["SYS002"],
  "status": "explicitly_reported",
  "evidence_sids": ["s000381"]
}
```

## atomic claims

```json
{
  "claim_id": "C0012",
  "claim_type": "correlation",
  "statement": "...",
  "claim_origin": "this_paper_result",
  "evidence_sids": ["s000411", "s000412"]
}
```

claim_originを必ず分けます。

```text
this_paper_result
author_interpretation
review_synthesis
cited_literature_summary
```

これによりIntroduction中の他人の研究結果を、この論文自身のresultと誤認するリスクを下げます。

### primary paperのIntroduction

primary researchと判定された論文では、Introduction/background chunkでは原則としてcitation contextだけを抽出し、measurementやown-result claimを作らないようにしています。

### reviews

reviewでは本文全体を使えますが、`review_synthesis` と `cited_literature_summary` として区別します。後でprimary evidenceと同じ重みで扱わないためです。

---

# 12. Script 06: LLMとは独立した機械的validation

```bash
python scripts/06_validate_extraction.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
data/extracted/P0002.validation.json
```

Qwenに「自信がありますか」と聞くのではなく、Pythonで客観的にチェックします。

現在のvalidatorは少なくとも:

1. evidence sentence IDが本当に存在するか
2. system refがinventoryに存在するか
3. cited reference IDがreference listに存在するか
4. citation contextのreference IDが実際にそのsentenceに付いているか
5. `explicitly_reported` measurementの数値文字列がevidence中に存在するか
6. primary paperのbackground文だけでown-result claimを支えていないか
7. 完全重複claimがないか

を見ます。

判定:

```text
pass
review_required
```

です。

`review_required` は「論文が悪い」という意味ではなく、人間が重点的に見るべき箇所があるという意味です。

---

# 13. Script 07: 人間確認用report

```bash
python scripts/07_review_report.py \
  --ids P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068
```

出力:

```text
outputs/review_reports/P0002.md
outputs/review_queue.csv
```

reportには:

- bibliographic metadata
- systems
- methods
- measurements
- atomic claims
- 元sentence ID + 元文
- validation error/warning
- human checklist

がまとまっています。

このreportと元PDFを並べて確認してください。

問題なければ:

```bash
python scripts/07_review_report.py --approve P0002
```

複数同時なら:

```bash
python scripts/07_review_report.py --approve P0002 P0005 P0008
```

問題があれば:

```bash
python scripts/07_review_report.py --reject P0011 --note "PEG molecular weight was incorrectly parsed"
```

human statusはSQLiteとmanifestへ保存されます。

---

# 14. Pilot 10報で何を確認するか

特に以下をPDFと照合してください。

## 最優先

- chain length / DP
- Mn / Mw
- dispersity
- end group
- solvent
- temperature
- concentration
- numerical values
- units
- hydration等のproperty definition
- claimの因果関係
- evidence sentence

## 合格基準の目安

最初は厳しく:

```text
存在しないevidence ID: 0
重要な数値の誤り: 0
単位の誤り: 0
他論文の結果をown resultとした重大誤分類: 0
```

を目標にしてください。

重要claimの取りこぼしが多い場合はprompt/schemaを修正して再実行します。

---

# 15. Prompt/schemaを直した場合

PEG固有の設定は:

```text
profiles/peg/
  inventory.schema.json
  evidence.schema.json
  prompts/
    inventory_system.txt
    evidence_system.txt
  review_checklist.txt
```

に隔離されています。

例えばhydration関連項目を追加したら、該当schema/promptのhashが変わります。

次にscriptを実行すると、その変更の影響を受けるstageは自動的にout-of-dateと判断されて再処理されます。

通常は `--force` は不要です。

明示的に全再処理したい場合のみ:

```bash
python scripts/04_extract_inventory.py --ids P0002 --force
```

とします。

---

# 16. Pilotが通ったら70報全部を処理する

GROBIDとQwen serverを起動した状態で:

```bash
python run_pipeline.py
```

これだけでStep 1〜7を順番に実行します。

各stageはcurrentならskipするため、既存70報のうち未処理分だけ進みます。

### 途中でPCを使いたくなったら

普通にPython processを止めても構いません。

次回:

```bash
python run_pipeline.py
```

とすれば、完成済みstage/chunkを再利用して再開します。

---

# 17. 後からPDFを追加する運用

例えば1か月後に15報追加した場合:

```bash
cp /somewhere/new_papers/*.pdf data/raw_pdfs/
python run_pipeline.py
```

です。

既存70報はcurrentならskipし、新規15報だけが流れます。

つまり、このフォルダ自体を「育てていく文献データベース」として使えます。

---

# 18. 毎晩自動処理する場合

まず手動で70報を通して安定性を確認してください。

その後はOSのschedulerから:

```bash
cd /path/to/peg_literature_pipeline
source .venv/bin/activate
python run_pipeline.py >> logs/nightly.log 2>&1
```

を夜間に起動すれば、新しく置いたPDFだけが処理されます。

LLM serverとGROBIDを常駐させるか、scheduler側で先に起動する構成にします。

---

# 19. 別分野へ再利用する方法

Python本体は原則変更しません。

例えば「G-quadruplex ligandの文献レビュー」に変えるなら:

```bash
cp -r profiles/peg profiles/g4_ligand
```

そして:

```text
profiles/g4_ligand/inventory.schema.json
profiles/g4_ligand/evidence.schema.json
profiles/g4_ligand/prompts/*.txt
profiles/g4_ligand/review_checklist.txt
```

だけをその分野向けに変更します。

`config.json`:

```json
"profile": "g4_ligand"
```

へ変えます。

pipeline本体のtop-level contractは共通です。

Inventory:

```text
article_type
objectives
systems
methods
studied_properties
global_conditions
```

Evidence:

```text
measurements
claims
limitations
citation_contexts
```

`systems[].attributes` の中身だけを分野ごとに変えられます。

このため、将来的に:

```text
PEG literature
G-quadruplex literature
membrane catalysis literature
organometallic catalysis literature
...
```

を同じエンジンで処理できます。

---

# 20. 現段階でまだ実装していないもの

このpackageは「信頼できる文献evidence databaseを作るところ」までです。

pilot 10報の抽出品質を確認した後、Phase 2として以下を追加するのが安全です。

```text
08_resolve_openalex.py
09_build_citation_graph.py
10_embed_papers.py
11_build_similarity_graph.py
12_leiden_cluster.py
13_cluster_dossier.py
14_build_chapter_evidence.py
15_draft_section.py
16_audit_draft.py
```

このPhase 2は、今回作った:

```text
SQLite
paper_json
inventory.json
evidence.json
human approval
```

をそのまま入力にできます。

---

# 21. 最初に実際に行うコマンド一覧

## 一度だけ

```bash
cd peg_literature_pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

docker compose -f docker-compose.grobid.yml up -d
```

別terminalでQwen llama-serverを起動します。

確認:

```bash
python check_environment.py
```

PDFを入れた後:

```bash
python scripts/01_make_manifest.py
```

`data/manifest.csv` を見てpilot 10報を決定。

例えば:

```bash
IDS=P0002,P0005,P0008,P0011,P0017,P0021,P0030,P0042,P0055,P0068

python scripts/02_grobid_parse.py --ids $IDS
python scripts/03_tei_to_json.py --ids $IDS
python scripts/04_extract_inventory.py --ids $IDS
python scripts/05_extract_evidence.py --ids $IDS
python scripts/06_validate_extraction.py --ids $IDS
python scripts/07_review_report.py --ids $IDS
```

review reportをPDFと比較します。

品質が十分なら:

```bash
python run_pipeline.py
```

で残りを処理します。

# 22. ライセンス

FolioSortは [GNU Affero General Public License v3.0 以降](LICENSE) (AGPL-3.0-or-later) で配布されます。

主な理由は、依存パッケージの一部がコピーレフトライセンスであることです。

| パッケージ | ライセンス | 用途 |
|---|---|---|
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | AGPL-3.0（商用ライセンス代替あり） | `scripts/05_extract_visual_assets.py` でのPDF図表抽出 |
| [python-igraph](https://github.com/igraph/python-igraph) | GPL-2.0-or-later | `lib/network_runtime.py`, `scripts/13_build_multiplex_network.py` でのネットワーク解析 |
| [leidenalg](https://github.com/vtraag/leidenalg) | GPL-3.0 | 同上、クラスタリング |

これら以外の主要依存（requests、lxml、jsonschema、rapidfuzz、networkx、scikit-learn、numpy、scipy、torch、transformers、adapters、safetensors、allenai/specter2モデル重み）はApache-2.0/BSD/MITなどの寛容なライセンスで、AGPL-3.0と両立します。GROBIDはDocker経由でHTTP APIとして呼び出すのみで、プロセスは分離されています。生成HTMLが参照する`vis-network`（unpkg CDN配信）はApache-2.0/MITデュアルライセンスです。
