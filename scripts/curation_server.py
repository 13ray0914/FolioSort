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
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.curation import (
    append_event,
    ensure_curation_schema,
    materialize_all,
    materialize_paper,
    read_event_log,
)
from lib.pipeline_common import connect_db, get_paths, load_config, read_json


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Literature Curation</title>
<style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;color-scheme:dark;background:#151515;color:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:#151515}header{position:sticky;top:0;z-index:4;background:#1c1c1f;border-bottom:1px solid #333;padding:12px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}header b{font-size:18px;margin-right:8px}select,input,textarea,button{background:#242428;color:#eee;border:1px solid #444;border-radius:7px;padding:8px;font:inherit}button{cursor:pointer}button:hover{border-color:#777}.danger{border-color:#7f1d1d}.accent{border-color:#6d5bd0}.layout{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:14px;padding:14px}.panel{background:#1c1c1f;border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:14px}.panel h2{margin:0 0 10px;font-size:16px}.panel h3{font-size:14px;margin:12px 0 7px}.item{border-top:1px solid #333;padding:12px 0}.item:first-of-type{border-top:0}.meta{font-size:12px;color:#9ca3af;white-space:pre-wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:7px}.full{grid-column:1/-1}.statement{width:100%;min-height:90px}.original{background:#17171a;border:1px solid #333;border-radius:7px;padding:9px;white-space:pre-wrap;color:#93c5fd;font-size:12px;line-height:1.45;margin:7px 0}.history{font-size:12px;max-height:620px;overflow:auto}.event{border-top:1px solid #333;padding:8px 0;word-break:break-word}.notice{font-size:12px;color:#fbbf24}.ok{color:#86efac;font-size:12px}.small{font-size:12px}.raw{color:#93c5fd}.tabnote{font-size:12px;color:#a7f3d0}.highlight{outline:2px solid #8b5cf6;border-radius:8px;padding:8px}.sticky{position:sticky;top:72px}.field label{font-size:11px;color:#aaa;display:block;margin-top:5px}.field input,.field textarea,.field select{width:100%}@media(max-width:950px){.layout{grid-template-columns:1fr}header{position:static}.sticky{position:static}}
</style></head><body>
<header><b>Literature curation</b><select id="paper"></select><button id="reload">Reload</button><span id="status" class="small"></span></header>
<div class="layout"><main>
<div class="panel"><h2>Controlled vocabulary / keyword normalization</h2><div class="notice">Raw extraction JSON is never edited. Every change is appended to events.jsonl and SQLite, then materialized into data/curated/.</div>
<div class="grid"><select id="aliasType"><option value="property">Property</option><option value="method">Method</option><option value="keyword">Keyword / topic tag</option></select><input id="aliasRaw" placeholder="Alias, e.g. aqueous solubility"><input id="aliasCanonical" class="full" placeholder="Canonical term, e.g. water solubility"><input id="aliasReason" class="full" placeholder="Reason (recommended)"><button id="aliasSave" class="full accent">Add global alias</button></div></div>
<div class="panel"><h2>Properties</h2><div id="properties"></div><div class="add"><h3>Add property</h3><div class="grid"><input id="addPropValue" placeholder="Property"><input id="addPropCanonical" placeholder="Canonical term (optional)"><input id="addPropEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addProp" class="full">Add property</button></div></div></div>
<div class="panel"><h2>Methods</h2><div id="methods"></div><div class="add"><h3>Add method</h3><div class="grid"><input id="addMethodValue" placeholder="Method"><input id="addMethodCanonical" placeholder="Canonical term (optional)"><input id="addMethodEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addMethod" class="full">Add method</button></div></div></div>
<div class="panel"><h2>Keywords / topic tags</h2><div class="tabnote">Use these for review-level classification when a concept is broader than a measured property or experimental method.</div><div id="keywords"></div><div class="add"><h3>Add keyword</h3><div class="grid"><input id="addKeywordValue" placeholder="Keyword / topic"><input id="addKeywordCanonical" placeholder="Canonical term (optional)"><input id="addKeywordEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addKeyword" class="full">Add keyword</button></div></div></div>
<div class="panel"><h2>Claim proofreading</h2><div class="tabnote">Blue text is the immutable LLM extraction. The editable fields below form the curated overlay used by graphs and later drafting.</div><div id="claims"></div><div class="add"><h3>Add claim</h3><textarea id="addClaimStatement" class="statement" placeholder="Claim statement"></textarea><div class="grid"><input id="addClaimType" placeholder="claim_type (optional)"><select id="addClaimStatus"><option value="edited">edited</option><option value="approved">approved</option><option value="needs_revision">needs_revision</option></select><input id="addClaimEvidence" class="full" placeholder="Evidence SIDs, comma separated"><input id="addClaimTags" class="full" placeholder="Curated tags, comma separated"><textarea id="addClaimNotes" class="full" placeholder="Review notes"></textarea><button id="addClaim" class="full">Add claim</button></div></div></div>
</main><aside><div class="sticky"><div class="panel"><h2>Change history</h2><div id="history" class="history"></div></div><div class="panel"><h2>Data locations</h2><div class="meta">Raw: data/extracted/\nCurated: data/curated/\nAudit: data/curation/events.jsonl\nSQLite: curation_events_v4</div></div></div></aside></div>
<script>
const $=id=>document.getElementById(id);let current=null;const params=new URLSearchParams(location.search);const requestedPaper=params.get('paper')||'';const requestedEntity=params.get('entity')||'';
function escList(v){return (v||'').split(',').map(x=>x.trim()).filter(Boolean)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function status(s,ok=true){$('status').textContent=s;$('status').className=ok?'ok':'notice'}
async function post(path,payload){try{status('Saving…');await api(path,{method:'POST',body:JSON.stringify(payload)});await loadPaper(payload.paper_id||$('paper').value);status('Saved. Raw extraction unchanged.')}catch(e){status(String(e),false)}}
async function loadPapers(){const j=await api('/api/papers');$('paper').replaceChildren(...j.papers.map(p=>{const o=document.createElement('option');o.value=p.paper_id;o.textContent=`${p.paper_id} — ${p.title||''}`;return o}));if(j.papers.length){if(requestedPaper&&j.papers.some(p=>p.paper_id===requestedPaper))$('paper').value=requestedPaper;await loadPaper($('paper').value)}}
function termRow(item,type){const d=document.createElement('div');d.className='item';d.dataset.entity=item.curation_uid||'';const keys={property:['property_raw','property_normalized'],method:['method_raw','method_normalized'],keyword:['keyword_raw','keyword_normalized']}[type];const [rawKey,normKey]=keys;const origKey=normKey+'_original';const top=document.createElement('div');top.className='meta';top.innerHTML=`<span class="raw">raw: ${esc(item[rawKey]||'')}</span><br>LLM normalized: ${esc(item[origKey]??item[normKey]??'')}<br>source: ${esc(item.canonical_source||'')}<br>uid: ${esc(item.curation_uid||'')}`;const inp=document.createElement('input');inp.value=item[normKey]||'';inp.style.width='100%';const reason=document.createElement('input');reason.placeholder='Reason (recommended)';reason.style.width='100%';const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save canonical override';save.onclick=()=>post('/api/term/override',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,canonical:inp.value,reason:reason.value});const del=document.createElement('button');del.textContent='Hide from curated view';del.className='danger';del.onclick=()=>{if(confirm('Hide this term from the curated view? Raw extraction remains unchanged.'))post('/api/term/delete',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,reason:reason.value})};buttons.append(save,del);d.append(top,inp,reason,buttons);return d}
function labeledInput(label,value){const wrap=document.createElement('div');wrap.className='field';const lab=document.createElement('label');lab.textContent=label;const inp=document.createElement('input');inp.value=value??'';wrap.append(lab,inp);return [wrap,inp]}
function claimRow(item){const d=document.createElement('div');d.className='item';d.dataset.entity=item.curation_uid||'';const meta=document.createElement('div');meta.className='meta';meta.textContent=`${item.claim_id||''} | uid: ${item.curation_uid||''}\nEvidence: ${(item.evidence_sids||[]).join(', ')}`;const original=document.createElement('div');original.className='original';original.textContent='Original LLM claim:\n'+(item.statement_original??item.statement??'');const ta=document.createElement('textarea');ta.className='statement';ta.value=item.statement||'';const [typeWrap,typeInp]=labeledInput('Claim type',item.claim_type);const [subWrap,subInp]=labeledInput('Subject',item.subject);const [relWrap,relInp]=labeledInput('Relation',item.relation);const [objWrap,objInp]=labeledInput('Object',item.object);const [condWrap,condInp]=labeledInput('Conditions / scope',item.conditions_text);const [tagsWrap,tagsInp]=labeledInput('Curated tags',(item.curated_tags||[]).join(', '));const statusWrap=document.createElement('div');statusWrap.className='field';const statusLab=document.createElement('label');statusLab.textContent='Review status';const statusSel=document.createElement('select');for(const value of ['unreviewed','edited','approved','needs_revision','rejected']){const o=document.createElement('option');o.value=value;o.textContent=value;statusSel.append(o)}statusSel.value=item.review_status||'unreviewed';statusWrap.append(statusLab,statusSel);const notes=document.createElement('textarea');notes.className='statement';notes.placeholder='Reviewer notes';notes.value=item.review_notes||'';const reason=document.createElement('input');reason.placeholder='Reason for this change (recommended)';reason.style.width='100%';const fields=document.createElement('div');fields.className='grid';fields.append(typeWrap,statusWrap,subWrap,relWrap,objWrap,condWrap,tagsWrap);tagsWrap.classList.add('full');const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save proofread claim';save.className='accent';save.onclick=()=>post('/api/claim/edit',{paper_id:current.paper_id,entity_uid:item.curation_uid,statement:ta.value,claim_type:typeInp.value,subject:subInp.value,relation:relInp.value,object:objInp.value,conditions_text:condInp.value,tags:escList(tagsInp.value),review_status:statusSel.value,review_notes:notes.value,reason:reason.value});const restore=document.createElement('button');restore.textContent='Restore raw wording';restore.onclick=()=>{if(confirm('Restore the editable fields to the original LLM extraction? This restoration is also logged.'))post('/api/claim/restore',{paper_id:current.paper_id,entity_uid:item.curation_uid,reason:reason.value})};const del=document.createElement('button');del.textContent='Hide from curated view';del.className='danger full';del.onclick=()=>{if(confirm('Hide this claim from the curated view? Raw extraction remains unchanged.'))post('/api/claim/delete',{paper_id:current.paper_id,entity_uid:item.curation_uid,reason:reason.value})};buttons.append(save,restore,del);d.append(meta,original,ta,fields,notes,reason,buttons);return d}
function renderHistory(events){const root=$('history');root.replaceChildren();for(const e of [...events].reverse()){const d=document.createElement('div');d.className='event';const t=document.createElement('div');t.textContent=`${e.created_at} — ${e.event_type}`;const m=document.createElement('div');m.className='meta';m.textContent=`${e.entity_type||''} ${e.entity_uid||''}\nactor: ${e.actor||''}\nold: ${JSON.stringify(e.old??'').slice(0,700)}\nnew: ${JSON.stringify(e.new??'').slice(0,700)}\nreason: ${e.reason||''}`;d.append(t,m);root.append(d)}if(!events.length)root.textContent='No edits yet.'}
async function loadPaper(id){const j=await api('/api/paper?id='+encodeURIComponent(id));current=j;$('properties').replaceChildren(...(j.inventory.studied_properties||[]).map(x=>termRow(x,'property')));$('methods').replaceChildren(...(j.inventory.methods||[]).map(x=>termRow(x,'method')));$('keywords').replaceChildren(...(j.inventory.keywords||[]).map(x=>termRow(x,'keyword')));$('claims').replaceChildren(...(j.evidence.claims||[]).map(claimRow));renderHistory(j.history);status(`Loaded ${id}`);if(requestedEntity){const target=document.querySelector(`[data-entity="${CSS.escape(requestedEntity)}"]`);if(target){target.classList.add('highlight');target.scrollIntoView({behavior:'smooth',block:'center'})}}}
$('paper').onchange=()=>loadPaper($('paper').value);$('reload').onclick=()=>loadPaper($('paper').value);
$('aliasSave').onclick=async()=>{await post('/api/term/alias',{entity_type:$('aliasType').value,alias:$('aliasRaw').value,canonical:$('aliasCanonical').value,reason:$('aliasReason').value});$('aliasRaw').value='';$('aliasCanonical').value='';$('aliasReason').value=''};
$('addProp').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'property',value:$('addPropValue').value,canonical:$('addPropCanonical').value,evidence_sids:escList($('addPropEvidence').value)});
$('addMethod').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'method',value:$('addMethodValue').value,canonical:$('addMethodCanonical').value,evidence_sids:escList($('addMethodEvidence').value)});
$('addKeyword').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'keyword',value:$('addKeywordValue').value,canonical:$('addKeywordCanonical').value,evidence_sids:escList($('addKeywordEvidence').value)});
$('addClaim').onclick=()=>post('/api/claim/add',{paper_id:current.paper_id,statement:$('addClaimStatement').value,claim_type:$('addClaimType').value,evidence_sids:escList($('addClaimEvidence').value),tags:escList($('addClaimTags').value),review_status:$('addClaimStatus').value,review_notes:$('addClaimNotes').value});
loadPapers().catch(e=>status(String(e),false));
</script></body></html>'''


class App:
    def __init__(self, config_path: str):
        self.config, self.root = load_config(config_path)
        self.paths = get_paths(self.config, self.root)
        cfg = self.config.get("curation", {})
        self.curated_dir = self.paths.get("curated", self.root / "data/curated")
        self.events_path = self.paths.get("curation", self.root / "data/curation") / "events.jsonl"
        self.ontology_path = self.root / cfg.get("ontology_path", "profiles/peg/ontology/terms.json")
        self.actor = cfg.get("actor") or None
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)
        conn = self.db()
        ensure_curation_schema(conn)
        conn.close()

    def db(self):
        return connect_db(self.paths["database"])

    def paper_ids(self) -> list[str]:
        conn = self.db()
        try:
            return [row[0] for row in conn.execute("SELECT paper_id FROM papers WHERE active=1 ORDER BY paper_id")]
        finally:
            conn.close()

    def rematerialize(self, paper_id: str | None = None) -> None:
        if paper_id:
            materialize_paper(paper_id, extracted_dir=self.paths["extracted"], curated_dir=self.curated_dir, ontology_path=self.ontology_path, events_path=self.events_path)
        else:
            materialize_all(self.paper_ids(), extracted_dir=self.paths["extracted"], curated_dir=self.curated_dir, ontology_path=self.ontology_path, events_path=self.events_path)

    def current_entity(self, paper_id: str, entity_type: str, uid: str) -> dict[str, Any] | None:
        payload = self.paper_payload(paper_id)
        if entity_type == "property":
            rows = payload.get("inventory", {}).get("studied_properties", [])
        elif entity_type == "method":
            rows = payload.get("inventory", {}).get("methods", [])
        elif entity_type == "keyword":
            rows = payload.get("inventory", {}).get("keywords", [])
        elif entity_type == "claim":
            rows = payload.get("evidence", {}).get("claims", [])
        else:
            rows = []
        for row in rows:
            if row.get("curation_uid") == uid:
                return row
        return None

    def paper_payload(self, paper_id: str) -> dict[str, Any]:
        self.rematerialize(paper_id)
        inv = self.curated_dir / f"{paper_id}.inventory.json"
        ev = self.curated_dir / f"{paper_id}.evidence.json"
        return {
            "paper_id": paper_id,
            "inventory": read_json(inv) if inv.exists() else {},
            "evidence": read_json(ev) if ev.exists() else {},
            "history": [x for x in read_event_log(self.events_path) if x.get("paper_id") in (None, paper_id)],
        }


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "LiteratureCuration/4.1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("CURATION %s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            data = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        if parsed.path == "/api/papers":
            conn = APP.db()
            try:
                rows = conn.execute("SELECT paper_id,title,year,journal FROM papers WHERE active=1 ORDER BY paper_id").fetchall()
                self.send_json({"papers": [dict(x) for x in rows]})
            finally:
                conn.close()
            return
        if parsed.path == "/api/paper":
            paper_id = (q.get("id") or [""])[0]
            if paper_id not in APP.paper_ids():
                self.send_json({"error": "unknown paper_id"}, 404)
                return
            self.send_json(APP.paper_payload(paper_id))
            return
        if parsed.path == "/api/history":
            paper_id = (q.get("paper_id") or [""])[0]
            events = read_event_log(APP.events_path)
            if paper_id:
                events = [x for x in events if x.get("paper_id") in (None, paper_id)]
            self.send_json({"events": events})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert APP is not None
        conn = None
        try:
            data = self.body()
            path = urllib.parse.urlparse(self.path).path
            conn = APP.db()
            common = {"conn": conn, "events_path": APP.events_path, "actor": APP.actor}
            allowed_terms = {"property", "method", "keyword"}
            if path == "/api/term/alias":
                term_type = data.get("entity_type")
                alias = str(data.get("alias") or "").strip()
                canonical = str(data.get("canonical") or "").strip()
                if term_type not in allowed_terms or not alias or not canonical:
                    raise ValueError("entity_type, alias and canonical are required")
                event = append_event(**common, event_type="term_alias", entity_type=term_type, new={"alias": alias, "canonical": canonical}, reason=data.get("reason"))
                APP.rematerialize()
                self.send_json({"ok": True, "event": event})
                return
            if path in {"/api/term/override", "/api/term/delete"}:
                paper_id = data.get("paper_id")
                term_type = data.get("entity_type")
                uid = data.get("entity_uid")
                if paper_id not in APP.paper_ids() or term_type not in allowed_terms or not uid:
                    raise ValueError("valid paper_id, entity_type and entity_uid are required")
                current = APP.current_entity(paper_id, term_type, uid)
                if path.endswith("override"):
                    canonical = str(data.get("canonical") or "").strip()
                    if not canonical:
                        raise ValueError("canonical is required")
                    event = append_event(**common, event_type="term_override", paper_id=paper_id, entity_type=term_type, entity_uid_value=uid, old=current, new={"canonical": canonical}, reason=data.get("reason"))
                else:
                    event = append_event(**common, event_type="term_delete", paper_id=paper_id, entity_type=term_type, entity_uid_value=uid, old=current, new={"hidden": True}, reason=data.get("reason"))
                APP.rematerialize(paper_id)
                self.send_json({"ok": True, "event": event})
                return
            if path == "/api/term/add":
                paper_id = data.get("paper_id")
                term_type = data.get("entity_type")
                value = str(data.get("value") or "").strip()
                if paper_id not in APP.paper_ids() or term_type not in allowed_terms or not value:
                    raise ValueError("valid paper_id, entity_type and value are required")
                event = append_event(**common, event_type="term_add", paper_id=paper_id, entity_type=term_type, new={"value": value, "canonical": data.get("canonical"), "evidence_sids": data.get("evidence_sids") or []}, reason=data.get("reason"))
                APP.rematerialize(paper_id)
                self.send_json({"ok": True, "event": event})
                return
            if path in {"/api/claim/edit", "/api/claim/delete", "/api/claim/restore"}:
                paper_id = data.get("paper_id")
                uid = data.get("entity_uid")
                if paper_id not in APP.paper_ids() or not uid:
                    raise ValueError("valid paper_id and entity_uid are required")
                current = APP.current_entity(paper_id, "claim", uid)
                if not current:
                    raise ValueError("claim was not found")
                if path.endswith("edit"):
                    fields = ["statement", "claim_type", "subject", "relation", "object", "conditions_text", "tags", "review_status", "review_notes"]
                    patch = {k: data[k] for k in fields if k in data}
                    if not str(patch.get("statement") or "").strip():
                        raise ValueError("statement is required")
                    event = append_event(**common, event_type="claim_edit", paper_id=paper_id, entity_type="claim", entity_uid_value=uid, old=current, new=patch, reason=data.get("reason"))
                elif path.endswith("restore"):
                    patch = {
                        "statement": current.get("statement_original", current.get("statement")),
                        "claim_type": current.get("claim_type_original", current.get("claim_type")),
                        "subject": current.get("subject_original", current.get("subject")),
                        "relation": current.get("relation_original", current.get("relation")),
                        "object": current.get("object_original", current.get("object")),
                        "conditions_text": current.get("conditions_text_original", current.get("conditions_text")),
                        "tags": current.get("curated_tags_original", []),
                        "review_status": "unreviewed",
                        "review_notes": "",
                    }
                    event = append_event(**common, event_type="claim_edit", paper_id=paper_id, entity_type="claim", entity_uid_value=uid, old=current, new=patch, reason=data.get("reason") or "Restored to raw LLM extraction", extra={"action": "restore_to_raw"})
                else:
                    event = append_event(**common, event_type="claim_delete", paper_id=paper_id, entity_type="claim", entity_uid_value=uid, old=current, new={"hidden": True}, reason=data.get("reason"))
                APP.rematerialize(paper_id)
                self.send_json({"ok": True, "event": event})
                return
            if path == "/api/claim/add":
                paper_id = data.get("paper_id")
                statement = str(data.get("statement") or "").strip()
                if paper_id not in APP.paper_ids() or not statement:
                    raise ValueError("valid paper_id and statement are required")
                fields = ["statement", "claim_type", "subject", "relation", "object", "conditions_text", "evidence_sids", "tags", "system_refs", "review_status", "review_notes"]
                new = {k: data.get(k) for k in fields}
                event = append_event(**common, event_type="claim_add", paper_id=paper_id, entity_type="claim", new=new, reason=data.get("reason"))
                APP.rematerialize(paper_id)
                self.send_json({"ok": True, "event": event})
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)
        finally:
            if conn is not None:
                conn.close()


def main() -> None:
    global APP
    ap = argparse.ArgumentParser(description="Local, append-only human curation UI for properties, methods, keywords, and claims.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    APP = App(args.config)
    host = args.host or APP.config.get("curation", {}).get("feedback_bind", "127.0.0.1")
    port = args.port or int(APP.config.get("curation", {}).get("feedback_port", 8765))
    print(f"Curation UI: http://{host}:{port}")
    print("Raw extraction files are read-only; edits go to events.jsonl + SQLite + data/curated/.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
