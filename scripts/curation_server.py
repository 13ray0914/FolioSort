#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from http import HTTPStatus
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
body{margin:0;background:#151515}header{position:sticky;top:0;z-index:4;background:#1c1c1f;border-bottom:1px solid #333;padding:12px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}header b{font-size:18px;margin-right:8px}select,input,textarea,button{background:#242428;color:#eee;border:1px solid #444;border-radius:7px;padding:8px;font:inherit}button{cursor:pointer}button:hover{border-color:#777}.danger{border-color:#7f1d1d}.layout{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:14px;padding:14px}.panel{background:#1c1c1f;border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:14px}.panel h2{margin:0 0 10px;font-size:16px}.item{border-top:1px solid #333;padding:10px 0}.item:first-of-type{border-top:0}.meta{font-size:12px;color:#9ca3af;white-space:pre-wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:7px}.full{grid-column:1/-1}.statement{width:100%;min-height:74px;box-sizing:border-box}.history{font-size:12px;max-height:520px;overflow:auto}.event{border-top:1px solid #333;padding:8px 0;word-break:break-word}.pill{display:inline-block;border:1px solid #555;border-radius:14px;padding:2px 7px;font-size:11px;margin-right:4px;color:#bbb}.notice{font-size:12px;color:#fbbf24}.ok{color:#86efac;font-size:12px}.toolbar{display:flex;gap:7px;flex-wrap:wrap}.toolbar>*{min-width:0}.toolbar select{min-width:260px}.add textarea{width:100%;box-sizing:border-box;min-height:70px}.small{font-size:12px}.raw{color:#93c5fd}.curated{color:#86efac}@media(max-width:900px){.layout{grid-template-columns:1fr}header{position:static}}
</style></head><body>
<header><b>Literature curation</b><select id="paper"></select><button id="reload">Reload</button><span id="status" class="small"></span></header>
<div class="layout"><main>
<div class="panel"><h2>Controlled vocabulary</h2><div class="notice">Raw extraction JSON is never edited. Changes are appended to an audit log and materialized into data/curated/.</div>
<div class="grid"><select id="aliasType"><option value="property">Property</option><option value="method">Method</option></select><input id="aliasRaw" placeholder="Alias, e.g. aqueous solubility"><input id="aliasCanonical" class="full" placeholder="Canonical term, e.g. water solubility"><input id="aliasReason" class="full" placeholder="Reason (optional)"><button id="aliasSave" class="full">Add global alias</button></div></div>
<div class="panel"><h2>Properties</h2><div id="properties"></div><div class="add"><h3>Add property</h3><input id="addPropValue" placeholder="Property"><input id="addPropCanonical" placeholder="Canonical term (optional)"><input id="addPropEvidence" placeholder="Evidence SIDs, comma separated"><button id="addProp">Add property</button></div></div>
<div class="panel"><h2>Methods</h2><div id="methods"></div><div class="add"><h3>Add method</h3><input id="addMethodValue" placeholder="Method"><input id="addMethodCanonical" placeholder="Canonical term (optional)"><input id="addMethodEvidence" placeholder="Evidence SIDs, comma separated"><button id="addMethod">Add method</button></div></div>
<div class="panel"><h2>Claims</h2><div id="claims"></div><div class="add"><h3>Add claim</h3><textarea id="addClaimStatement" placeholder="Claim statement"></textarea><div class="grid"><input id="addClaimType" placeholder="claim_type (optional)"><input id="addClaimEvidence" placeholder="Evidence SIDs, comma separated"><input id="addClaimTags" class="full" placeholder="Curated tags, comma separated"><button id="addClaim" class="full">Add claim</button></div></div></div>
</main><aside><div class="panel"><h2>Change history</h2><div id="history" class="history"></div></div><div class="panel"><h2>Current files</h2><div class="meta">Raw: data/extracted/\nCurated: data/curated/\nAudit: data/curation/events.jsonl\nSQLite: curation_events_v4</div></div></aside></div>
<script>
const $=id=>document.getElementById(id);let current=null;
function escList(v){return (v||'').split(',').map(x=>x.trim()).filter(Boolean)}
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function status(s,ok=true){$('status').textContent=s;$('status').className=ok?'ok':'notice'}
async function loadPapers(){const j=await api('/api/papers');$('paper').replaceChildren(...j.papers.map(p=>{const o=document.createElement('option');o.value=p.paper_id;o.textContent=`${p.paper_id} — ${p.title||''}`;return o}));if(j.papers.length){await loadPaper($('paper').value)}}
function termRow(item,type){const d=document.createElement('div');d.className='item';const rawKey=type==='property'?'property_raw':'method_raw';const normKey=type==='property'?'property_normalized':'method_normalized';const origKey=normKey+'_original';const top=document.createElement('div');top.className='meta';const raw=document.createElement('span');raw.className='raw';raw.textContent=`raw: ${item[rawKey]||''}`;top.append(raw,document.createElement('br'));const orig=document.createElement('span');orig.textContent=`LLM normalized: ${item[origKey]??item[normKey]??''}`;top.append(orig,document.createElement('br'));const uid=document.createElement('span');uid.textContent=`uid: ${item.curation_uid||''}`;top.append(uid);const inp=document.createElement('input');inp.value=item[normKey]||'';inp.style.width='100%';inp.style.boxSizing='border-box';const reason=document.createElement('input');reason.placeholder='Reason (optional)';reason.style.width='100%';reason.style.boxSizing='border-box';const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save paper override';save.addEventListener('click',async()=>{await post('/api/term/override',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,canonical:inp.value,reason:reason.value})});const del=document.createElement('button');del.textContent='Hide/delete from curated';del.className='danger';del.addEventListener('click',async()=>{if(confirm('Hide this term from the curated view? Raw extraction remains unchanged.'))await post('/api/term/delete',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,reason:reason.value})});buttons.append(save,del);d.append(top,inp,reason,buttons);return d}
function claimRow(item){const d=document.createElement('div');d.className='item';const meta=document.createElement('div');meta.className='meta';meta.textContent=`${item.claim_id||''} | ${item.claim_type||''} | uid: ${item.curation_uid||''}\nEvidence: ${(item.evidence_sids||[]).join(', ')}`;const ta=document.createElement('textarea');ta.className='statement';ta.value=item.statement||'';const typ=document.createElement('input');typ.value=item.claim_type||'';typ.placeholder='claim_type';typ.style.width='100%';typ.style.boxSizing='border-box';const tags=document.createElement('input');tags.value=(item.curated_tags||[]).join(', ');tags.placeholder='Curated tags';tags.style.width='100%';tags.style.boxSizing='border-box';const reason=document.createElement('input');reason.placeholder='Reason (optional)';reason.style.width='100%';reason.style.boxSizing='border-box';const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save claim edit';save.addEventListener('click',async()=>{await post('/api/claim/edit',{paper_id:current.paper_id,entity_uid:item.curation_uid,statement:ta.value,claim_type:typ.value,tags:escList(tags.value),reason:reason.value})});const del=document.createElement('button');del.textContent='Hide/delete from curated';del.className='danger';del.addEventListener('click',async()=>{if(confirm('Hide this claim from the curated view? Raw extraction remains unchanged.'))await post('/api/claim/delete',{paper_id:current.paper_id,entity_uid:item.curation_uid,reason:reason.value})});buttons.append(save,del);d.append(meta,ta,typ,tags,reason,buttons);return d}
async function loadPaper(id){const j=await api('/api/paper?id='+encodeURIComponent(id));current=j;$('properties').replaceChildren(...(j.inventory.studied_properties||[]).map(x=>termRow(x,'property')));$('methods').replaceChildren(...(j.inventory.methods||[]).map(x=>termRow(x,'method')));$('claims').replaceChildren(...(j.evidence.claims||[]).map(claimRow));renderHistory(j.history);status(`Loaded ${id}`)}
function renderHistory(events){const root=$('history');root.replaceChildren();for(const e of [...events].reverse()){const d=document.createElement('div');d.className='event';const t=document.createElement('div');t.textContent=`${e.created_at} — ${e.event_type}`;const m=document.createElement('div');m.className='meta';const oldv=e.old==null?'':JSON.stringify(e.old);const newv=e.new==null?'':JSON.stringify(e.new);m.textContent=`${e.entity_type||''} ${e.entity_uid||''}\nactor: ${e.actor||''}\nold: ${oldv.slice(0,700)}\nnew: ${newv.slice(0,700)}\nreason: ${e.reason||''}`;d.append(t,m);root.append(d)}if(!events.length)root.textContent='No edits yet.'}
async function post(path,payload){try{status('Saving…');await api(path,{method:'POST',body:JSON.stringify(payload)});await loadPaper(current.paper_id);status('Saved')}catch(e){status(e.message,false)}}
$('paper').addEventListener('change',()=>loadPaper($('paper').value));$('reload').addEventListener('click',()=>loadPaper($('paper').value));
$('aliasSave').addEventListener('click',async()=>{await post('/api/term/alias',{entity_type:$('aliasType').value,alias:$('aliasRaw').value,canonical:$('aliasCanonical').value,reason:$('aliasReason').value});$('aliasRaw').value='';$('aliasCanonical').value='';$('aliasReason').value=''})
$('addProp').addEventListener('click',async()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'property',value:$('addPropValue').value,canonical:$('addPropCanonical').value,evidence_sids:escList($('addPropEvidence').value)}));
$('addMethod').addEventListener('click',async()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'method',value:$('addMethodValue').value,canonical:$('addMethodCanonical').value,evidence_sids:escList($('addMethodEvidence').value)}));
$('addClaim').addEventListener('click',async()=>post('/api/claim/add',{paper_id:current.paper_id,statement:$('addClaimStatement').value,claim_type:$('addClaimType').value,evidence_sids:escList($('addClaimEvidence').value),tags:escList($('addClaimTags').value)}));
loadPapers().catch(e=>status(e.message,false));
</script></body></html>'''


class App:
    def __init__(self, config_path: str):
        self.config, self.root = load_config(config_path)
        self.paths = get_paths(self.config, self.root)
        self.cfg = self.config.get("curation", {})
        self.curated_dir = self.paths.get("curated", self.root / "data/curated")
        self.curation_dir = self.paths.get("curation", self.root / "data/curation")
        self.events_path = self.curation_dir / "events.jsonl"
        ontology_path = Path(self.cfg.get("ontology_path", f"profiles/{self.config['profile']}/ontology/terms.json"))
        self.ontology_path = ontology_path if ontology_path.is_absolute() else self.root / ontology_path
        conn = connect_db(self.paths["database"])
        ensure_curation_schema(conn)
        conn.close()

    @property
    def actor(self) -> str:
        return self.cfg.get("actor") or os.environ.get("USER") or "local_user"

    def db(self):
        conn = connect_db(self.paths["database"])
        ensure_curation_schema(conn)
        return conn

    def paper_ids(self) -> list[str]:
        conn = self.db()
        try:
            return [row["paper_id"] for row in conn.execute("SELECT paper_id FROM papers WHERE active=1 ORDER BY paper_id")]
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
    server_version = "LiteratureCuration/4.1"
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("CURATION %s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:
        assert APP is not None
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            data = HTML.encode("utf-8"); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if parsed.path == "/health": self.send_json({"ok": True}); return
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
            if paper_id not in APP.paper_ids(): self.send_json({"error": "unknown paper_id"}, 404); return
            self.send_json(APP.paper_payload(paper_id)); return
        if parsed.path == "/api/history":
            paper_id = (q.get("paper_id") or [""])[0]
            events = read_event_log(APP.events_path)
            if paper_id: events = [x for x in events if x.get("paper_id") in (None, paper_id)]
            self.send_json({"events": events}); return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert APP is not None
        try:
            data = self.body(); path = urllib.parse.urlparse(self.path).path
            conn = APP.db()
            common = {"conn": conn, "events_path": APP.events_path, "actor": APP.actor}
            if path == "/api/term/alias":
                term_type = data.get("entity_type"); alias = str(data.get("alias") or "").strip(); canonical = str(data.get("canonical") or "").strip()
                if term_type not in {"property","method"} or not alias or not canonical: raise ValueError("entity_type, alias and canonical are required")
                event = append_event(**common, event_type="term_alias", entity_type=term_type, new={"alias":alias,"canonical":canonical}, reason=data.get("reason")); APP.rematerialize(); self.send_json({"ok":True,"event":event}); return
            if path in {"/api/term/override","/api/term/delete"}:
                paper_id=data.get("paper_id"); term_type=data.get("entity_type"); uid=data.get("entity_uid")
                if paper_id not in APP.paper_ids() or term_type not in {"property","method"} or not uid: raise ValueError("valid paper_id, entity_type and entity_uid are required")
                current = APP.current_entity(paper_id, term_type, uid)
                if path.endswith("override"):
                    canonical=str(data.get("canonical") or "").strip()
                    if not canonical: raise ValueError("canonical is required")
                    event=append_event(**common,event_type="term_override",paper_id=paper_id,entity_type=term_type,entity_uid_value=uid,old=current,new={"canonical":canonical},reason=data.get("reason"))
                else:
                    event=append_event(**common,event_type="term_delete",paper_id=paper_id,entity_type=term_type,entity_uid_value=uid,old=current,new={"hidden":True},reason=data.get("reason"))
                APP.rematerialize(paper_id); self.send_json({"ok":True,"event":event}); return
            if path == "/api/term/add":
                paper_id=data.get("paper_id"); term_type=data.get("entity_type"); value=str(data.get("value") or "").strip()
                if paper_id not in APP.paper_ids() or term_type not in {"property","method"} or not value: raise ValueError("valid paper_id, entity_type and value are required")
                event=append_event(**common,event_type="term_add",paper_id=paper_id,entity_type=term_type,new={"value":value,"canonical":data.get("canonical"),"evidence_sids":data.get("evidence_sids") or []},reason=data.get("reason")); APP.rematerialize(paper_id); self.send_json({"ok":True,"event":event}); return
            if path in {"/api/claim/edit","/api/claim/delete"}:
                paper_id=data.get("paper_id"); uid=data.get("entity_uid")
                if paper_id not in APP.paper_ids() or not uid: raise ValueError("valid paper_id and entity_uid are required")
                current = APP.current_entity(paper_id, "claim", uid)
                if path.endswith("edit"):
                    patch={k:data[k] for k in ["statement","claim_type","subject","relation","object","conditions_text","tags"] if k in data}
                    if not str(patch.get("statement") or "").strip(): raise ValueError("statement is required")
                    event=append_event(**common,event_type="claim_edit",paper_id=paper_id,entity_type="claim",entity_uid_value=uid,old=current,new=patch,reason=data.get("reason"))
                else:
                    event=append_event(**common,event_type="claim_delete",paper_id=paper_id,entity_type="claim",entity_uid_value=uid,old=current,new={"hidden":True},reason=data.get("reason"))
                APP.rematerialize(paper_id); self.send_json({"ok":True,"event":event}); return
            if path == "/api/claim/add":
                paper_id=data.get("paper_id"); statement=str(data.get("statement") or "").strip()
                if paper_id not in APP.paper_ids() or not statement: raise ValueError("valid paper_id and statement are required")
                new={k:data.get(k) for k in ["statement","claim_type","subject","relation","object","conditions_text","evidence_sids","tags","system_refs"]}
                event=append_event(**common,event_type="claim_add",paper_id=paper_id,entity_type="claim",new=new,reason=data.get("reason")); APP.rematerialize(paper_id); self.send_json({"ok":True,"event":event}); return
            self.send_json({"error":"not found"},404)
        except Exception as exc:
            self.send_json({"error":str(exc)},400)
        finally:
            if 'conn' in locals():
                conn.close()


def main() -> None:
    global APP
    ap=argparse.ArgumentParser(description="Local, append-only human curation UI for terms and claims.")
    ap.add_argument("--config",default=str(ROOT/"config.json")); ap.add_argument("--host"); ap.add_argument("--port",type=int)
    args=ap.parse_args(); APP=App(args.config)
    host=args.host or APP.cfg.get("feedback_bind","127.0.0.1"); port=args.port or int(APP.cfg.get("feedback_port",8765))
    print(f"Curation UI: http://{host}:{port}")
    print("Raw extraction files are read-only; edits go to events.jsonl + SQLite + data/curated/.")
    ThreadingHTTPServer((host,port),Handler).serve_forever()

if __name__=="__main__": main()
