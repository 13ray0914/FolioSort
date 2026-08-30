#!/usr/bin/env python3
from __future__ import annotations

# Re-exec user-facing scripts with the project's virtualenv.
# This avoids PATH/pyenv selecting a Python build without required stdlib extensions
# such as _sqlite3. The pipeline wrapper already activates this venv; this guard
# makes direct ./scripts/*.py invocation equally reliable.
import os as _bootstrap_os
import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_BOOT_ROOT = _BootstrapPath(__file__).resolve().parents[1]
_BOOT_VENV = _BOOT_ROOT / ".venv"
_BOOT_PY = _BOOT_VENV / "bin" / "python"
if _BOOT_PY.exists() and _BootstrapPath(_bootstrap_sys.prefix).resolve() != _BOOT_VENV.resolve():
    _bootstrap_os.execv(str(_BOOT_PY), [str(_BOOT_PY), str(_BootstrapPath(__file__).resolve()), *_bootstrap_sys.argv[1:]])

import argparse
import fcntl
import json
import mimetypes
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.pipeline_common import connect_db, get_paths, load_config
from lib.projects import (
    DEFAULT_PROJECT_SLUG,
    create_project,
    ensure_project_schema,
    list_projects,
    normalize_project_slug,
    project_knowledge_dir,
    project_name,
    project_network_dir,
    project_paper_ids,
    project_upload_dir,
    rename_project,
)

APP_VERSION = "4.1.3-selected-layer-recluster-fast"
MAX_UPLOAD_BYTES = 250 * 1024 * 1024

HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FolioSort</title><style>
:root{color-scheme:dark;font-family:Inter,Segoe UI,Arial,sans-serif;background:#151515;color:#e8e8eb}*{box-sizing:border-box}body{margin:0;background:#151515}.wrap{max-width:1180px;margin:0 auto;padding:24px}h1{font-size:26px;margin:0 0 5px}h2{font-size:16px;margin:0 0 10px}.muted{color:#a1a1aa;font-size:12px}.grid{display:grid;grid-template-columns:1.08fr .92fr;gap:16px;margin-top:18px;align-items:start}.card{background:#1c1c1f;border:1px solid #33343a;border-radius:12px;padding:16px}.drop{border:2px dashed #555862;border-radius:12px;padding:28px 18px;text-align:center;background:#202024;transition:.15s}.drop.drag{border-color:#b6b7c3;background:#28282e}.drop b{display:block;font-size:18px;margin-bottom:7px}button,.btn,input,select{background:#2a2a30;color:#f4f4f5;border:1px solid #4a4a53;border-radius:8px;padding:10px 13px;font:inherit}button,.btn{cursor:pointer}button:hover,.btn:hover{border-color:#85858f}button:disabled{opacity:.42;cursor:not-allowed}.primary{background:#373741;font-weight:700;flex:1}.danger{background:#472525;border-color:#7d3838;font-weight:700;flex:0 0 160px}.projectrow{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:center}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.toolbar button{flex:1;min-width:150px}.pipelineActions{display:flex;gap:8px;margin-top:12px}.statusline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}.pill{font-size:11px;padding:4px 8px;border-radius:999px;border:1px solid #444752;color:#d4d4d8}.ok{color:#86efac;border-color:#365c43}.busy{color:#fde68a;border-color:#6b5a2c}.bad{color:#fca5a5;border-color:#713c3c}.files{margin-top:12px;max-height:250px;overflow:auto}.file{padding:8px 0;border-top:1px solid #303036;font-size:13px;word-break:break-all}.log{background:#111113;border:1px solid #303036;border-radius:8px;padding:10px;white-space:pre-wrap;overflow:auto;max-height:650px;min-height:480px;font:12px/1.45 Consolas,monospace}.hint{margin-top:8px;font-size:12px;color:#a1a1aa}.counts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}.metric{background:#222226;border-radius:8px;padding:10px}.metric b{font-size:20px;display:block}.hidden{display:none}.projectName{font-size:13px;margin-top:8px}.divider{height:1px;background:#303036;margin:16px 0}.subhead{font-size:14px;font-weight:700;margin:0 0 10px}.results{margin-top:16px}@media(max-width:800px){.grid{grid-template-columns:1fr}.wrap{padding:14px}.counts{grid-template-columns:1fr 1fr}.projectrow{grid-template-columns:1fr 1fr}.projectrow select{grid-column:1/-1}.pipelineActions{flex-direction:column}.danger{flex:auto}.log{min-height:300px;max-height:430px}}
</style></head><body><div class="wrap">
<h1>FolioSort</h1><div class="muted">Local literature workspace for project-scoped PDF analysis, graph generation, curation, and original-PDF access. WSL runs in the background.</div>
<div class="grid"><div>
<div class="card"><h2>Project</h2><div class="projectrow"><select id="project"></select><button id="newProject">New project</button><button id="renameProject">Rename</button></div><div id="projectInfo" class="hint">Each project gets its own Literature Network and Scientific Knowledge Graph. Analysis data remain shared and deduplicated globally.</div><div class="counts"><div class="metric"><b id="active">-</b><span class="muted">active papers</span></div><div class="metric"><b id="memory">-</b><span class="muted">memories ready</span></div><div class="metric"><b id="networkState">-</b><span class="muted">network</span></div></div><div id="projectDisplay" class="projectName muted"></div><div class="divider"></div><div class="subhead">Add papers</div><div id="drop" class="drop" tabindex="0"><b>Drop PDF files here</b><span>or click to choose files</span><input id="pick" type="file" accept="application/pdf,.pdf" multiple class="hidden"></div><div id="uploadMsg" class="hint">PDFs dropped here are assigned to the selected project. Exact byte-identical duplicates are deleted, while the existing paper is also assigned to this project.</div><div id="files" class="files"></div><div class="pipelineActions"><button id="analyze" class="primary">Analyze / update selected project</button><button id="stopPipeline" class="danger" disabled>Stop pipeline</button></div><div class="statusline"><span id="pipePill" class="pill">Pipeline: checking</span><span id="svcPill" class="pill">FolioSort: ready</span></div></div>
<div class="card results"><h2>Results</h2><div class="toolbar"><button id="network">Literature network</button><button id="knowledge">Knowledge graph</button><button id="curation">Curation editor</button></div><div class="hint">Results open only the selected project. Network nodes use “Family name, year”. Knowledge Graph expansion has Back/Forward history and adjustable label size.</div></div>
</div><div><div class="card"><h2>Pipeline log</h2><div id="log" class="log">Waiting for status...</div></div></div></div></div>
<script>
const $=x=>document.getElementById(x),drop=$('drop'),pick=$('pick');let currentProject=localStorage.getItem('foliosort-project')||localStorage.getItem('review-project')||'default';let refreshing=false;
function saveProject(){localStorage.setItem('foliosort-project',currentProject)}
function esc(s){return String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}function fmtBytes(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';return (n/1048576).toFixed(1)+' MB'}
async function jsonFetch(url,opt={}){const r=await fetch(url,opt);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function purl(path){return path+(path.includes('?')?'&':'?')+'project='+encodeURIComponent(currentProject)}
async function upload(list){const fs=[...list].filter(f=>f.name.toLowerCase().endsWith('.pdf'));if(!fs.length)return;const fd=new FormData();fs.forEach(f=>fd.append('files',f,f.name));$('uploadMsg').textContent=`Uploading ${fs.length} PDF(s) to ${currentProject}...`;try{const j=await jsonFetch(purl('/api/upload'),{method:'POST',body:fd});$('uploadMsg').textContent=j.message;await refresh();}catch(e){$('uploadMsg').textContent='Upload failed: '+e}}
drop.onclick=()=>pick.click();drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();pick.click()}};pick.onchange=()=>upload(pick.files);['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('drag')}));['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('drag')}));drop.addEventListener('drop',e=>upload(e.dataTransfer.files));
$('project').addEventListener('change',()=>{currentProject=$('project').value;saveProject();refresh()});
$('newProject').onclick=async()=>{const name=prompt('New project name');if(!name)return;try{const j=await jsonFetch('/api/create_project?name='+encodeURIComponent(name),{method:'POST'});currentProject=j.project_slug;saveProject();await refresh();$('uploadMsg').textContent=`Project created: ${j.name}`;}catch(e){alert(e)}};
$('renameProject').onclick=async()=>{const selected=$('project').selectedOptions[0];const name=prompt('New display name for this project',selected?selected.textContent.replace(/\s*\(\d+\)\s*$/,''):'');if(!name)return;try{await jsonFetch('/api/rename_project?project='+encodeURIComponent(currentProject)+'&name='+encodeURIComponent(name),{method:'POST'});await refresh();}catch(e){alert(e)}};
$('analyze').onclick=async()=>{try{const j=await jsonFetch(purl('/api/analyze'),{method:'POST'});$('uploadMsg').textContent=j.message;await refresh();}catch(e){$('uploadMsg').textContent='Could not start pipeline: '+e}};
$('stopPipeline').onclick=async()=>{if(!confirm('Stop the running pipeline? Completed outputs will be kept.'))return;$('stopPipeline').disabled=true;$('uploadMsg').textContent='Stopping pipeline...';try{const j=await jsonFetch('/api/stop_pipeline',{method:'POST'});$('uploadMsg').textContent=j.message;await refresh();}catch(e){$('uploadMsg').textContent='Could not stop pipeline: '+e;await refresh()}};
$('network').onclick=()=>window.open(purl('/network'),'_blank');$('knowledge').onclick=()=>window.open(purl('/knowledge'),'_blank');$('curation').onclick=async()=>{try{await jsonFetch('/api/start_curation',{method:'POST'});window.open('http://127.0.0.1:8765/','_blank')}catch(e){alert(e)}};
async function refresh(){if(refreshing)return;refreshing=true;try{const j=await jsonFetch(purl('/api/status'));const sel=$('project');const projects=j.projects||[];if(!projects.some(p=>p.project_slug===currentProject)){currentProject=projects[0]?.project_slug||'default';saveProject()}sel.innerHTML=projects.map(p=>`<option value="${esc(p.project_slug)}" ${p.project_slug===currentProject?'selected':''}>${esc(p.name)} (${p.active_papers})</option>`).join('');$('active').textContent=j.active_papers;$('memory').textContent=j.memory_count;$('networkState').textContent=j.network_ready?'ready':'not yet';$('projectDisplay').textContent=`${j.project_name} · ${currentProject}`;let runText=j.pipeline_running?'running':'idle';if(j.pipeline_running&&j.running_project_name)runText+=` · ${j.running_project_name}`;$('pipePill').textContent='Pipeline: '+runText;$('pipePill').className='pill '+(j.pipeline_running?'busy':'ok');$('stopPipeline').disabled=!j.pipeline_stoppable;$('stopPipeline').title=j.pipeline_running&&!j.pipeline_stoppable?'This pipeline was not started by FolioSort, so FolioSort will not kill an unknown terminal process.':'';$('files').innerHTML=(j.raw_pdfs||[]).slice(-30).reverse().map(f=>`<div class="file">${esc(f.name)} <span class="muted">${fmtBytes(f.size)}</span></div>`).join('')||'<div class="muted">No PDFs in this project yet.</div>';$('log').textContent=j.log_tail||'No pipeline log yet.';const L=$('log');L.scrollTop=L.scrollHeight;$('svcPill').textContent='FolioSort: ready';$('svcPill').className='pill ok'}catch(e){$('svcPill').textContent='FolioSort: disconnected';$('svcPill').className='pill bad'}finally{refreshing=false}}
refresh();setInterval(refresh,2500);
</script></body></html>'''



class FolioSortApp:
    def __init__(self, config_path: str):
        self.config, self.root = load_config(config_path)
        self.paths = get_paths(self.config, self.root)
        self.raw_dir = self.paths["raw_pdfs"]
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.pipeline_lock = self.log_dir / "auto_pipeline.lock"
        self.pipeline_pid_file = self.log_dir / "review-app-pipeline.pid"
        self.pipeline_project_file = self.log_dir / "review-app-pipeline.project"
        self.curation_pid_file = self.log_dir / "curation-server.pid"
        conn = connect_db(self.paths["database"])
        try:
            ensure_project_schema(conn)
        finally:
            conn.close()

    def db(self) -> sqlite3.Connection:
        conn = connect_db(self.paths["database"])
        ensure_project_schema(conn)
        return conn

    def pipeline_running(self) -> bool:
        self.pipeline_lock.touch(exist_ok=True)
        with self.pipeline_lock.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False

    def owned_pipeline_pid(self) -> int | None:
        try:
            pid = int(self.pipeline_pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return None
        proc = Path(f"/proc/{pid}/cmdline")
        try:
            cmdline = proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            self.pipeline_pid_file.unlink(missing_ok=True)
            return None
        if "run_review_pipeline.sh" not in cmdline:
            self.pipeline_pid_file.unlink(missing_ok=True)
            return None
        return pid

    def running_project_slug(self) -> str | None:
        if not self.pipeline_running():
            return None
        try:
            return normalize_project_slug(self.pipeline_project_file.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def latest_log(self) -> Path | None:
        logs = sorted(self.log_dir.glob("auto_pipeline_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return logs[0] if logs else None

    def append_log_marker(self, message: str) -> None:
        path = self.latest_log()
        if not path:
            return
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[Review App] {message}\n")
        except OSError:
            pass

    def log_tail(self, lines: int = 120) -> str:
        path = self.latest_log()
        if not path:
            return ""
        try:
            data = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(data[-lines:])
        except OSError:
            return ""

    def projects(self) -> list[dict[str, Any]]:
        conn = self.db()
        try:
            return list_projects(conn)
        finally:
            conn.close()

    def project_name(self, project_slug: str) -> str:
        conn = self.db()
        try:
            return project_name(conn, project_slug)
        finally:
            conn.close()

    def create_project(self, name: str) -> str:
        conn = self.db()
        try:
            slug = create_project(conn, name)
            project_upload_dir(self.raw_dir, slug)
            return slug
        finally:
            conn.close()

    def rename_project(self, project_slug: str, name: str) -> None:
        conn = self.db()
        try:
            rename_project(conn, project_slug, name)
        finally:
            conn.close()

    def active_papers(self, project_slug: str) -> int:
        conn = self.db()
        try:
            return len(project_paper_ids(conn, project_slug, active_only=True))
        finally:
            conn.close()

    def memory_count(self, project_slug: str) -> int:
        conn = self.db()
        try:
            ids = project_paper_ids(conn, project_slug, active_only=True)
        finally:
            conn.close()
        memory_dir = self.root / "data" / "summary_memory"
        return sum((memory_dir / f"{paper_id}.memory.json").exists() for paper_id in ids)

    def project_raw_files(self, project_slug: str) -> list[dict[str, Any]]:
        slug = normalize_project_slug(project_slug)
        result: dict[str, dict[str, Any]] = {}
        conn = self.db()
        try:
            ids = project_paper_ids(conn, slug, active_only=True)
            for paper_id in ids:
                row = conn.execute("SELECT source_relpath FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
                if not row:
                    continue
                path = self.raw_dir / row["source_relpath"]
                if path.exists():
                    result[path.resolve().as_posix()] = {"name": path.name, "size": path.stat().st_size, "paper_id": paper_id}
        finally:
            conn.close()
        # Include newly uploaded files before the manifest has assigned a P-ID.
        upload_dir = project_upload_dir(self.raw_dir, slug)
        for path in upload_dir.glob("*.pdf"):
            if path.is_file():
                result[path.resolve().as_posix()] = {"name": path.name, "size": path.stat().st_size, "paper_id": None}
        if slug == DEFAULT_PROJECT_SLUG:
            for path in self.raw_dir.glob("*.pdf"):
                if path.is_file():
                    result[path.resolve().as_posix()] = {"name": path.name, "size": path.stat().st_size, "paper_id": None}
        return sorted(result.values(), key=lambda item: item["name"].casefold())

    def resolve_pdf(self, paper_id: str) -> Path:
        conn = self.db()
        try:
            row = conn.execute("SELECT source_relpath,active FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            raise FileNotFoundError(f"Unknown paper ID: {paper_id}")
        path = (self.raw_dir / row["source_relpath"]).resolve()
        raw_resolved = self.raw_dir.resolve()
        if raw_resolved not in path.parents and path != raw_resolved:
            raise ValueError("Invalid source path")
        if not path.exists():
            raise FileNotFoundError(f"Original PDF is not present: {row['source_relpath']}")
        return path

    def open_windows_file(self, path: Path) -> None:
        try:
            win = subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
            subprocess.Popen(["explorer.exe", win], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            raise RuntimeError(f"Could not open Windows PDF viewer: {exc}") from exc

    def recluster_network(self, project_slug: str, layers: list[str], resolution: float) -> dict[str, Any]:
        slug = normalize_project_slug(project_slug)
        network_json = project_network_dir(self.root, slug) / "network.json"
        if not network_json.exists():
            raise FileNotFoundError(f"Literature Network has not been generated for project {slug}")
        allowed = {
            "citation", "semantic", "claim", "property", "method",
            "keyword", "keyword_semantic", "bibliographic_coupling",
        }
        selected = [str(name) for name in layers if str(name) in allowed]
        if not selected:
            raise ValueError("Select at least one network layer")
        resolution = min(3.0, max(0.2, float(resolution)))
        python = self.root / ".venv_network" / "bin" / "python"
        script = self.root / "scripts" / "15_recluster_network.py"
        if not python.exists():
            raise RuntimeError(".venv_network is missing. Run scripts/install_network_env.sh")
        completed = subprocess.run(
            [str(python), str(script), "--project", slug, "--layers", ",".join(selected), "--resolution", str(resolution), "--save"],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "reclustering failed").strip()
            raise RuntimeError(detail[-4000:])
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid reclustering response: {completed.stdout[-1000:]}") from exc

    def start_pipeline(self, project_slug: str) -> tuple[bool, str]:
        slug = normalize_project_slug(project_slug)
        if self.pipeline_running():
            return False, "Pipeline is already running."
        # Ensure the project exists before launching a long-running child.
        conn = self.db()
        try:
            name = project_name(conn, slug)
        finally:
            conn.close()
        proc = subprocess.Popen(
            [str(self.root / "scripts" / "run_review_pipeline.sh")],
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "REVIEW_ROOT": str(self.root), "REVIEW_PROJECT": slug},
        )
        self.pipeline_pid_file.write_text(str(proc.pid) + "\n", encoding="utf-8")
        self.pipeline_project_file.write_text(slug + "\n", encoding="utf-8")

        def cleanup() -> None:
            proc.wait()
            try:
                if self.pipeline_pid_file.exists() and self.pipeline_pid_file.read_text().strip() == str(proc.pid):
                    self.pipeline_pid_file.unlink(missing_ok=True)
            except OSError:
                pass

        threading.Thread(target=cleanup, daemon=True).start()
        return True, f"Pipeline started for project: {name}. Existing analysis is reused; only this project's graphs are rebuilt."

    def stop_pipeline(self, grace_seconds: float = 8.0) -> tuple[bool, str]:
        pid = self.owned_pipeline_pid()
        if pid is None:
            if self.pipeline_running():
                return False, "A pipeline is running, but it was not started by FolioSort. It was left untouched for safety."
            return False, "Pipeline is already idle."
        self.append_log_marker(f"STOP requested from GUI for process group {pid}")
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.pipeline_pid_file.unlink(missing_ok=True)
            return False, "Pipeline had already stopped."
        deadline = time.monotonic() + max(1.0, grace_seconds)
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                self.pipeline_pid_file.unlink(missing_ok=True)
                self.append_log_marker("Pipeline stopped gracefully after GUI request")
                return True, "Pipeline stopped gracefully. Completed outputs were kept."
            time.sleep(0.25)
        try:
            os.killpg(pid, signal.SIGKILL)
            message = "Pipeline did not stop within 8 seconds, so the app force-stopped its process group. Completed outputs were kept."
        except ProcessLookupError:
            message = "Pipeline stopped. Completed outputs were kept."
        self.pipeline_pid_file.unlink(missing_ok=True)
        self.append_log_marker(message)
        return True, message

    def start_curation(self) -> str:
        port = int((self.config.get("curation") or {}).get("feedback_port", 8765))
        expected_version = "4.1.3-metadata-curation"
        old_server_running = False
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.7) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("version") == expected_version:
                    return f"http://127.0.0.1:{port}/"
                old_server_running = True
        except Exception:
            pass
        if old_server_running:
            subprocess.run([str(self.root / "scripts" / "stop_curation_gui.sh")], cwd=str(self.root), check=False)
            time.sleep(0.5)
        proc = subprocess.Popen(
            [str(self.root / ".venv" / "bin" / "python"), str(self.root / "scripts" / "curation_server.py")],
            cwd=str(self.root), stdout=(self.log_dir / "curation-server.log").open("ab"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.curation_pid_file.write_text(str(proc.pid) + "\n", encoding="utf-8")
        return f"http://127.0.0.1:{port}/"


APP: FolioSortApp | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"FolioSort/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("REVIEWAPP %s - %s\n" % (self.address_string(), fmt % args))

    def allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin in (None, "null", "http://127.0.0.1:8766", "http://localhost:8766")

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ("null", "http://127.0.0.1:8766", "http://localhost:8766"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def send_html(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists():
            self.send_html(f"<h2>Not generated yet</h2><p>{path.name} does not exist for this project. Run Analyze first.</p>", 404); return
        data = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def project_from_query(self, parsed: urllib.parse.ParseResult) -> str:
        q = urllib.parse.parse_qs(parsed.query)
        return normalize_project_slug((q.get("project") or [DEFAULT_PROJECT_SLUG])[0])

    def do_OPTIONS(self) -> None:
        self.send_response(204); self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS"); self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.end_headers()

    def do_GET(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/": self.send_html(HTML); return
        if parsed.path == "/health": self.send_json({"ok": True, "version": APP_VERSION}); return
        if parsed.path == "/api/status":
            slug = self.project_from_query(parsed)
            running_slug = APP.running_project_slug()
            self.send_json({
                "pipeline_running": APP.pipeline_running(),
                "pipeline_stoppable": APP.owned_pipeline_pid() is not None,
                "running_project": running_slug,
                "running_project_name": APP.project_name(running_slug) if running_slug else None,
                "project_slug": slug,
                "project_name": APP.project_name(slug),
                "projects": APP.projects(),
                "active_papers": APP.active_papers(slug),
                "memory_count": APP.memory_count(slug),
                "network_ready": (project_network_dir(APP.root, slug) / "network.html").exists(),
                "knowledge_ready": (project_knowledge_dir(APP.root, slug) / "knowledge.html").exists(),
                "raw_pdfs": APP.project_raw_files(slug),
                "log_tail": APP.log_tail(),
            }); return
        if parsed.path == "/network":
            slug = self.project_from_query(parsed); self.serve_file(project_network_dir(APP.root, slug) / "network.html", "text/html; charset=utf-8"); return
        if parsed.path == "/knowledge":
            slug = self.project_from_query(parsed); self.serve_file(project_knowledge_dir(APP.root, slug) / "knowledge.html", "text/html; charset=utf-8"); return
        if parsed.path.startswith("/assets/"):
            # All generated graph pages use the same local vis-network asset.
            self.serve_file(APP.root / "assets" / Path(parsed.path).name); return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        if not self.allowed_origin():
            self.send_json({"error": "cross-origin request denied"}, 403); return
        try:
            if parsed.path == "/api/network/recluster":
                slug = self.project_from_query(parsed)
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 65536:
                    raise ValueError("A small JSON request body is required")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                result = APP.recluster_network(slug, list(body.get("layers") or []), float(body.get("resolution", 1.0)))
                self.send_json(result); return
            if parsed.path == "/api/create_project":
                q = urllib.parse.parse_qs(parsed.query); name = (q.get("name") or [""])[0]
                slug = APP.create_project(name)
                self.send_json({"ok": True, "project_slug": slug, "name": APP.project_name(slug)}); return
            if parsed.path == "/api/rename_project":
                q = urllib.parse.parse_qs(parsed.query); slug = normalize_project_slug((q.get("project") or [""])[0]); name = (q.get("name") or [""])[0]
                APP.rename_project(slug, name)
                self.send_json({"ok": True, "project_slug": slug, "name": APP.project_name(slug)}); return
            if parsed.path == "/api/upload":
                slug = self.project_from_query(parsed)
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type.lower(): raise ValueError("multipart/form-data required")
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD_BYTES * 5: raise ValueError("Upload request is empty or too large")
                body = self.rfile.read(length)
                envelope = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8") + body
                message = BytesParser(policy=policy.default).parsebytes(envelope)
                target_dir = project_upload_dir(APP.raw_dir, slug)
                saved=[]
                for part in message.iter_parts():
                    name = Path(part.get_filename() or "").name
                    if not name.lower().endswith(".pdf"): continue
                    payload = part.get_payload(decode=True) or b""
                    if not payload: continue
                    if len(payload) > MAX_UPLOAD_BYTES: raise ValueError(f"{name}: file exceeds 250 MB")
                    if payload[:5] != b"%PDF-": raise ValueError(f"{name}: not a valid PDF header")
                    target = target_dir / name
                    if target.exists():
                        stem, suffix = target.stem, target.suffix; i=2
                        while target.exists(): target = target_dir / f"{stem} ({i}){suffix}"; i+=1
                    target.write_bytes(payload); saved.append(target.name)
                if not saved: raise ValueError("No PDF files received")
                self.send_json({"ok": True, "saved": saved, "message": f"Added {len(saved)} PDF(s) to {APP.project_name(slug)}. Press Analyze to update this project."}); return
            if parsed.path == "/api/analyze":
                slug = self.project_from_query(parsed); started,msg=APP.start_pipeline(slug); self.send_json({"ok": True, "started": started, "message": msg}); return
            if parsed.path == "/api/stop_pipeline":
                stopped,msg=APP.stop_pipeline(); self.send_json({"ok": True, "stopped": stopped, "message": msg}); return
            if parsed.path == "/api/open_pdf":
                q=urllib.parse.parse_qs(parsed.query); paper_id=(q.get("id") or [""])[0]
                pdf=APP.resolve_pdf(paper_id); APP.open_windows_file(pdf); self.send_json({"ok": True, "paper_id": paper_id, "path": str(pdf)}); return
            if parsed.path == "/api/start_curation":
                url=APP.start_curation(); self.send_json({"ok": True, "url": url}); return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)


def main() -> None:
    global APP
    ap=argparse.ArgumentParser(description="Windows-facing local dashboard for the literature review pipeline")
    ap.add_argument("--config", default=str(ROOT / "config.json")); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8766)
    args=ap.parse_args(); APP=FolioSortApp(args.config)
    print(f"FolioSort: http://{args.host}:{args.port}/")
    print("Select a project, drop PDFs, Analyze, and use Stop pipeline when needed.")
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()

if __name__ == "__main__": main()
