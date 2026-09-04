# FolioSort

[![CI](https://github.com/13ray0914/FolioSort/actions/workflows/ci.yml/badge.svg)](https://github.com/13ray0914/FolioSort/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.4.0-8b5cf6.svg)](https://github.com/13ray0914/FolioSort/releases)

FolioSort is a local-first, evidence-traceable literature review workspace. It turns a collection of scientific PDFs into structured claims, measurements, review reports, literature networks, and a scientific knowledge graph while preserving links back to the source sentences and visual evidence.

> [日本語README](README_JA.md) · [v4の詳細な日本語ガイド](README_V4_JA.md)

<img width="1919" height="1059" alt="Image" src="https://github.com/user-attachments/assets/822d95df-2457-437b-b2f1-8b584214ec2a" />

## Why FolioSort?

LLM-generated summaries are difficult to audit when their statements cannot be traced to the original paper. FolioSort assigns stable IDs to papers, sentences, figures, tables, equations, claims, and measurements so that a review statement can be traced in reverse:

```text
review statement
  → claim or measurement
  → evidence sentence / figure / table
  → original PDF
```

PDF filenames are never treated as scientific evidence. They are retained only as human-readable references to the original files.

## Features

- Local PDF ingestion with SHA-256 change and duplicate detection
- Stable paper and evidence identifiers
- GROBID full-text, bibliography, and coordinate extraction
- Local Qwen/llama.cpp inventory, measurement, and atomic-claim extraction
- Whole-paper summary memory for long-document processing
- Mechanical validation independent of the LLM
- Human review, approval, rejection, and curation workflows
- Crossref and OpenAlex metadata enrichment and reference resolution
- Figure, table, graph, scheme, and equation extraction
- Optional SPECTER2 embeddings
- Multiplex literature network with Leiden clustering
- Scientific knowledge graph with progressive neighborhood expansion
- Project workspaces over a shared canonical PDF library
- Resumable, hash-aware processing that skips unchanged stages

## Process

```text
PDFs
  → manifest and stable paper IDs
  → GROBID TEI
  → sentence/visual JSON
  → metadata enrichment
  → visual asset extraction
  → whole-paper summary memory
  → inventory and evidence extraction
  → deterministic validation
  → reference resolution
  → human review reports
  → embeddings, networks, and knowledge graph
```

## Requirements

- Windows 10/11 with WSL2 and Ubuntu, or a compatible Linux environment
- Python 3.10 or newer
- Docker for GROBID
- A llama.cpp-compatible local text model server; the default configuration expects an OpenAI-compatible endpoint at `http://127.0.0.1:8080/v1`
- Sufficient storage for source PDFs, intermediate data, and optional models

Optional components include SPECTER2, MinerU, and a separate multimodal llama.cpp server for visual interpretation.

### Local Qwen hardware requirements

The automatic launcher is tuned for **Qwen3.8-27B Q4_K_M** with the Q4_0 MTP draft model, full GPU offload, one request slot, Flash Attention, and a 65,536-token context. The two GGUF files used by the current default are approximately 17 GB and 1.6 GB. Qwen's official model card describes the model as a 27B dense model with a native 262,144-token context; FolioSort deliberately uses a smaller context to control memory use. See the [official Qwen3.8-27B model card](https://huggingface.co/Qwen/Qwen3.8-27B) and [llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).

| Resource | Practical minimum for the default | Recommended | Notes |
|---|---:|---:|---|
| GPU | NVIDIA CUDA-capable GPU | Recent NVIDIA GPU | CPU-only and partial-offload execution are possible in llama.cpp but are much slower for this workflow. |
| VRAM | 24 GB | 32 GB or more | The tested RTX 4090 run used about 23.3/24.0 GiB. Other llama.cpp builds and display use can require extra headroom. |
| System RAM | 32 GB | 64 GB or more | Use at least 64 GB when partially offloading to CPU, running other local models, or processing many large PDFs. |
| Free disk | 25 GB for llama.cpp and the two GGUF files | 40 GB or more plus PDF/output space | FolioSort research data, GROBID images, embeddings, and backups need additional space. |
| CPU | Modern x86-64 CPU with AVX2, 8 cores | 12+ cores | CPU is used for parsing, validation, graph construction, and any layers not offloaded to the GPU. |

These figures are deployment guidance, not a guarantee: model quantization, context length, llama.cpp revision, KV-cache type, batch size, and GPU display usage all change memory consumption. The default `-c 65536` reserves substantially more cache than a shorter context. If 24 GB VRAM is insufficient, first stop other GPU applications, then try `-c 32768`; reducing it further can reject FolioSort's longer requests. Changing the launcher command requires editing `scripts/run_review_pipeline.sh`.

The launcher reads these environment variables, so custom locations do not require editing source files:

```bash
export QWEN_SERVER="$HOME/desktop/llm/llama.cpp/build/bin/llama-server"
export QWEN_MODEL="$HOME/models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf"
export QWEN_DRAFT_MODEL="$HOME/models/Qwen3.8-27B/mtp-Qwen3.8-27B-Q4_0.gguf"
```

The Qwen weights and llama.cpp binary are not installed by the FolioSort Python package.

## Download a release

Each tagged version is published on the [GitHub Releases page](https://github.com/13ray0914/FolioSort/releases) with versioned, immutable download files:

- `FolioSort-vX.Y.Z-source.zip` and `FolioSort-vX.Y.Z-source.tar.gz` — source snapshots
- `foliosort-X.Y.Z-py3-none-any.whl` and `foliosort-X.Y.Z.tar.gz` — Python package files
- `FolioSort-X.Y.Z-setup.exe` — Windows/WSL installer
- `SHA256SUMS.txt` — checksums for every attached file

The Windows installer sets up the Python package inside an existing Ubuntu/WSL distribution and creates FolioSort shortcuts. Install WSL first with `wsl --install -d Ubuntu` if necessary. The installer preserves the WSL workspace, PDFs, results, and local configuration when FolioSort is upgraded or uninstalled. It does not install Docker/GROBID, Qwen, llama.cpp, GPU drivers, or model weights. The current installer is not code-signed, so Windows SmartScreen may require **More info → Run anyway** after you verify the SHA-256 checksum.

## Install with pip

FolioSort is installable directly from its Git repository. Use a dedicated virtual environment inside WSL/Linux; the Windows-native Python runtime is not supported because the service launchers use POSIX process and locking facilities.

Install the local OCR engine and English/Japanese language data if image-only PDFs must be processed:

```bash
sudo apt update
sudo apt install ocrmypdf tesseract-ocr-eng tesseract-ocr-jpn
```

The **Run OCR blocked papers** button creates derived searchable PDFs in `data/ocr_pdfs/`; canonical source PDFs are never overwritten. Only successfully OCR-processed papers are sent through the remaining resumable stages.

Before initialization, create a free [OpenAlex account and API key](https://openalex.org/settings/api). OpenAlex's keyless daily budget is intended only for casual use and can stop a multi-paper metadata update with `429 Too Many Requests`; a free key raises the daily budget. See the official [OpenAlex authentication and rate-limit guide](https://help.openalex.org/api/authentication/).

```bash
python3 -m venv ~/.venvs/foliosort
source ~/.venvs/foliosort/bin/activate
python -m pip install --upgrade pip
python -m pip install "foliosort @ git+https://github.com/13ray0914/FolioSort.git@main"

foliosort --version
foliosort init ~/desktop/review
cd ~/desktop/review
```

During `foliosort init`, paste the OpenAlex API key at the hidden prompt:

```text
Create a free OpenAlex account and copy your API key:
  https://openalex.org/settings/api
OpenAlex API key (input hidden; Enter to configure later):
```

The key is stored only in the local, Git-ignored `config.json` and is never printed. `foliosort init` writes the application files and creates `config.json` from the example. It does not bundle Qwen, llama.cpp, Docker, or GROBID. To refresh an existing pip-created workspace after upgrading, run `foliosort init ~/desktop/review --force`; generated data, the existing `config.json`, and its API key are preserved.

For unattended installation, use `foliosort init PATH --no-openalex-prompt` and provide `OPENALEX_API_KEY` in the environment that runs `foliosort serve`.

Install the optional isolated graph environment and start GROBID:

```bash
./scripts/install_network_env.sh
docker compose -f docker-compose.grobid.yml up -d
```

After configuring/starting Qwen, verify and serve FolioSort:

```bash
foliosort check
foliosort serve
```

The server stays in the foreground and is available at [http://127.0.0.1:8766](http://127.0.0.1:8766). The `Analyze` button inherits the pip environment used by `foliosort serve`. The core stages can also be run directly with the compatibility command `foliosort pipeline --from-step 6 --to-step 11`.

For development from a local checkout, use `python -m pip install -e .` instead of the Git URL.

## Quick start

The clone-based setup remains available for development or source inspection. The commands below use the default WSL workspace expected by the launcher scripts.

```bash
git clone https://github.com/13ray0914/FolioSort.git ~/desktop/review
cd ~/desktop/review

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements_v4.txt

cp config.example.json config.json
```

Install the isolated graph-analysis environment:

```bash
./scripts/install_network_env.sh
```

Start GROBID:

```bash
docker compose -f docker-compose.grobid.yml up -d
curl http://127.0.0.1:8070/api/isalive
```

Start your local llama.cpp server and edit `config.json` if its URL or model name differs from the defaults. Then verify the environment:

```bash
python check_environment.py
```

Launch the FolioSort workspace:

```bash
./scripts/start_review_app.sh
```

The dashboard is served on the loopback interface at [http://127.0.0.1:8766](http://127.0.0.1:8766). From the dashboard you can create projects, add PDFs, start or stop analysis, curate extracted evidence, and open both graph views.

On Windows, a desktop shortcut can be created once with:

```bash
./scripts/install_windows_app.sh
```

## Running the process

Place PDFs in the configured raw-PDF directory or add them through the dashboard. For a full project update, use the dashboard's **Analyze / Update Selected Project** button

<img width="1919" height="1059" alt="Image" src="https://github.com/user-attachments/assets/bfbfe460-95ef-42a7-977f-0c3f8b820f21" />

or run:

```bash
./scripts/run_review_pipeline.sh
```

The core eleven-stage process can also be controlled directly:

```bash
python run_pipeline.py
python run_pipeline.py --ids P0002,P0005,P0008
python run_pipeline.py --from-step 6 --to-step 11
```

Completed stages are reused when their input, prompt, schema, model, and configuration hashes remain current.

## Graph interfaces

The Literature Network combines citation, semantic, claim, property, method, keyword, and bibliographic-coupling layers. Leiden clustering always uses the complete selected layers; display sparsification affects only canvas rendering. The saved layout first arranges papers inside each Leiden community, then separates community centroids so cluster boundaries remain visible. Search results can be highlighted as a group—for example, every paper matching an author query—without changing clustering.

`review_required` is an automatic validation flag, not a failed analysis. Open the Curation Editor, review errors before warnings, correct or reject affected claims, and record a separate human decision. Automatic validation reports remain unchanged as an audit record; an approved human decision is shown separately in the network.

The Scientific Knowledge Graph connects papers to claims, properties, methods, systems, measurements, and visual evidence. Its progressive expansion and Fast/Balanced/Full modes keep large graphs interactive without deleting scientific data from the export.

```bash
./scripts/open_network_gui.sh
./scripts/open_knowledge_gui.sh
```

## Main outputs

| Output | Purpose |
|---|---|
| `data/manifest.csv` | Stable paper registry |
| `data/paper_json/` | Sentence- and visual-ID-preserving paper JSON |
| `data/extracted/` | Inventories, measurements, claims, and validation |
| `data/visual_assets/` | Cropped figures, tables, and equations |
| `outputs/review_reports/` | Human-readable evidence reports |
| `outputs/projects/<project>/network_gui/` | Multiplex literature network |
| `outputs/projects/<project>/knowledge_graph/` | Scientific knowledge graph and tabular exports |

Generated research data, source PDFs, local configuration, caches, and model artifacts are intentionally excluded from Git by `.gitignore`.

## Security and privacy

- Application services bind to `127.0.0.1` by default.
- GROBID Docker ports are published only on the loopback interface.
- Browser-facing write endpoints validate request provenance and JSON content types.
- Uploaded PDFs and extracted scientific content remain local unless you explicitly configure an external service.
- Crossref and OpenAlex are used for metadata and citation enrichment, not for downloading paper PDFs.

FolioSort is research software, not an autonomous authority. Validate important claims, numerical values, units, and interpretations against the original papers before publication.

## Development

Run the regression suite:

```bash
python -m unittest discover -s tests -v
python -m compileall -q lib scripts run_pipeline.py check_environment.py
bash -n scripts/*.sh
docker compose -f docker-compose.grobid.yml config --quiet
```

CI runs the same security and smoke checks on pushes and pull requests.

## Documentation

- [Japanese setup and evidence-extraction guide](README_JA.md)
- [Japanese v4 architecture and migration guide](README_V4_JA.md)
- [Example configuration](config.example.json)
- [Default v4.1+ configuration overlay](config.v4_1.defaults.json)

## License

FolioSort is distributed under the [GNU Affero General Public License v3.0 or later](LICENSE) (`AGPL-3.0-or-later`). See the license section in [README_JA.md](README_JA.md#22-ライセンス) for dependency-specific notes.
