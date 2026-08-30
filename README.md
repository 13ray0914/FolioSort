# FolioSort

[![CI](https://github.com/13ray0914/FolioSort/actions/workflows/ci.yml/badge.svg)](https://github.com/13ray0914/FolioSort/actions/workflows/ci.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-4.2.0-8b5cf6.svg)](https://github.com/13ray0914/FolioSort/releases)

FolioSort is a local-first, evidence-traceable literature review workspace. It turns a collection of scientific PDFs into structured claims, measurements, review reports, literature networks, and a scientific knowledge graph while preserving links back to the source sentences and visual evidence.

> [日本語README](README_JA.md) · [v4の詳細な日本語ガイド](README_V4_JA.md)

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

## Pipeline

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

## Quick start

The commands below use the default WSL workspace expected by the launcher scripts.

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

## Running the pipeline

Place PDFs in the configured raw-PDF directory or add them through the dashboard. For a full project update, use the dashboard's **Analyze / update selected project** button or run:

```bash
./scripts/run_review_pipeline.sh
```

The core eleven-stage pipeline can also be controlled directly:

```bash
python run_pipeline.py
python run_pipeline.py --ids P0002,P0005,P0008
python run_pipeline.py --from-step 6 --to-step 11
```

Completed stages are reused when their input, prompt, schema, model, and configuration hashes remain current.

## Graph interfaces

The Literature Network combines citation, semantic, claim, property, method, keyword, and bibliographic-coupling layers. Leiden clustering always uses the complete selected layers; display sparsification affects only canvas rendering.

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
