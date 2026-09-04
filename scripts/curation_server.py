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
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
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
    normalize_publication_year,
    read_event_log,
)
from lib.pipeline_common import connect_db, get_paths, load_config, now_iso, read_json
from lib.v4_common import CrossrefClient, OpenAlexClient, valid_doi
from lib.web_security import browser_request_is_trusted, is_loopback_http_url, read_json_object
from foliosort import __version__

MAX_JSON_BODY_BYTES = 1024 * 1024
CURATION_APP_VERSION = f"{__version__}-validation-review-v4"


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Curation Editor</title><script>try{const q=new URLSearchParams(location.search).get('theme');const saved=localStorage.getItem('foliosort-curation-theme');document.documentElement.dataset.theme=q==='light'||q==='dark'?q:(saved||'dark')}catch(_error){document.documentElement.dataset.theme='dark'}</script>
<style>
:root{font-family:Inter,Segoe UI,Arial,sans-serif;color-scheme:dark;background:#151515;color:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:#151515}header{position:sticky;top:0;z-index:4;background:#1c1c1f;border-bottom:1px solid #333;padding:12px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}header b{font-size:18px;margin-right:8px;white-space:nowrap}header #paper{flex:1 1 320px;width:min(520px,100%);max-width:100%;min-width:0}select,input,textarea,button{background:#242428;color:#eee;border:1px solid #444;border-radius:7px;padding:8px;font:inherit}button{cursor:pointer}button:hover{border-color:#777}.danger{border-color:#7f1d1d}.accent{border-color:#6d5bd0}.layout{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:14px;padding:14px}.panel{background:#1c1c1f;border:1px solid #333;border-radius:10px;padding:14px;margin-bottom:14px}.panel h2{margin:0 0 10px;font-size:16px}.panel h3{font-size:14px;margin:12px 0 7px}.item{border-top:1px solid #333;padding:12px 0}.item:first-of-type{border-top:0}.meta{font-size:12px;color:#9ca3af;white-space:pre-wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:7px}.full{grid-column:1/-1}.doiRow{display:grid;grid-template-columns:minmax(0,1fr) max-content;gap:7px;align-items:center}.doiRow button{white-space:nowrap}.statement{width:100%;min-height:90px}.original{background:#17171a;border:1px solid #333;border-radius:7px;padding:9px;white-space:pre-wrap;color:#93c5fd;font-size:12px;line-height:1.45;margin:7px 0}.history{font-size:12px;max-height:620px;overflow:auto}.event{border-top:1px solid #333;padding:8px 0;word-break:break-word}.notice{font-size:12px;color:#fbbf24}.ok{color:#86efac;font-size:12px}.small{font-size:12px}.raw{color:#93c5fd}.tabnote{font-size:12px;color:#a7f3d0}.highlight{outline:2px solid #8b5cf6;border-radius:8px;padding:8px}.sticky{position:sticky;top:72px}.field label{font-size:11px;color:#aaa;display:block;margin-top:5px}.field input,.field textarea,.field select{width:100%}.authors{width:100%;min-height:92px}.missing{border-color:#b45309!important}.preview{padding:8px;border-radius:7px;background:#222228;margin-top:7px;font-size:13px}.validationIssue{padding:8px 9px;margin-top:6px;border-radius:7px;background:#202024;border-left:3px solid #d6a23e;font:12px/1.45 Consolas,monospace;overflow-wrap:anywhere}.validationIssue.error{border-left-color:#dc6868}.validationSummary{padding:9px;border-radius:7px;background:#222228;margin:8px 0;font-size:13px}@media(max-width:950px){.layout{grid-template-columns:1fr}header{position:static}.sticky{position:static}.doiRow{grid-template-columns:1fr}}
:root{--page:#151515;--surface:#1c1c1f;--surface2:#222228;--control:#242428;--line:#333;--control-line:#444;--text:#e5e7eb;--muted:#9ca3af;--raw:#93c5fd;--note:#a7f3d0;--warning:#fbbf24;color-scheme:dark}:root[data-theme="light"]{--page:#f4f5f7;--surface:#fff;--surface2:#f0f2f5;--control:#fff;--line:#d6dae1;--control-line:#c8ced7;--text:#202124;--muted:#5f6670;--raw:#1d4f91;--note:#166534;--warning:#854d0e;color-scheme:light}body{background:var(--page);color:var(--text)}header,.panel{background:var(--surface);border-color:var(--line)}select,input,textarea,button{background:var(--control);color:var(--text);border-color:var(--control-line)}.meta{color:var(--muted)}.original,.preview,.validationIssue,.validationSummary{background:var(--surface2);border-color:var(--line)}.original,.raw{color:var(--raw)}.tabnote{color:var(--note)}.notice{color:var(--warning)}.item,.event{border-color:var(--line)}.field label{color:var(--muted)}.themeToggle{margin-left:auto;white-space:nowrap}.accent{border-color:#64748b}:root[data-theme="light"] .ok{color:#166534}:root[data-theme="light"] .danger{border-color:#b86c6c;color:#7f1d1d}@media(max-width:950px){.themeToggle{margin-left:0}}
</style></head><body>
<header><b>Curation Editor</b><select id="paper"></select><button id="reload">Reload</button><button id="rebuildNow">Rebuild affected network</button><span id="rebuildState" class="small"></span><span id="status" class="small"></span><button id="themeToggle" class="themeToggle" type="button">Light mode</button></header>
<div class="layout"><main>
<div class="panel"><h2>Publication metadata</h2><div class="tabnote">Correct title, author names, publication year, journal, or DOI when automatic metadata retrieval is incomplete. The imported metadata remains unchanged; edits are stored as an append-only overlay.</div><div id="metadataOriginal" class="original"></div><div class="grid"><div class="field full"><label>Title</label><input id="metaTitle"></div><div class="field"><label>Publication year</label><input id="metaYear" inputmode="numeric" maxlength="4" placeholder="e.g. 2024"></div><div class="field"><label>Journal</label><input id="metaJournal"></div><div class="field full"><label>DOI</label><div class="doiRow"><input id="metaDoi" placeholder="10.xxxx/..."><button id="metaFetchDoi" type="button">Fetch metadata from DOI</button></div><div class="meta">Fetched values fill the form for review. Click “Save metadata correction” to apply them and rebuild the network.</div></div><div class="field full"><label>Authors — one per line; “Family, Given” is recommended</label><textarea id="metaAuthors" class="authors"></textarea></div><input id="metaReason" class="full" placeholder="Reason for metadata correction (recommended)"><button id="metaSave" class="accent">Save metadata correction</button><button id="metaRestore">Restore imported metadata</button></div><div id="metaPreview" class="preview"></div><div class="notice" style="margin-top:7px">Saved curation is materialized immediately. FolioSort automatically queues a rebuild of every Multiplex Network project that contains this paper, so corrected author/year labels and curated terms/claims propagate to the graph without rerunning the heavy PDF/LLM process.</div></div>
<div class="panel"><h2>Automatic validation review</h2><div class="tabnote"><code>review_required</code> means deterministic checks found at least one warning or error; the paper is still usable. Review errors first, correct or reject affected claims below, then record a separate human decision here. The automatic report remains immutable for auditability.</div><div id="validationSummary" class="validationSummary">Loading validation…</div><div id="validationIssues"></div><div class="grid"><div class="field"><label>Human decision</label><select id="validationDecision"><option value="pending">pending</option><option value="approved">approved</option><option value="needs_revision">needs_revision</option><option value="rejected">rejected</option></select></div><div class="field full"><label>Review notes</label><textarea id="validationNotes" class="statement" placeholder="What was checked or corrected?"></textarea></div><button id="validationSave" class="accent full">Save validation review</button></div></div>
<div class="panel"><h2>Controlled vocabulary / keyword normalization</h2><div class="notice">Raw extraction JSON is never edited. Every change is appended to events.jsonl and SQLite, then materialized into data/curated/.</div>
<div class="grid"><select id="aliasType"><option value="property">Property</option><option value="method">Method</option><option value="keyword">Keyword / topic tag</option></select><input id="aliasRaw" placeholder="Alias, e.g. aqueous solubility"><input id="aliasCanonical" class="full" placeholder="Canonical term, e.g. water solubility"><input id="aliasReason" class="full" placeholder="Reason (recommended)"><button id="aliasSave" class="full accent">Add global alias</button></div></div>
<div class="panel"><h2>Properties</h2><div id="properties"></div><div class="add"><h3>Add property</h3><div class="grid"><input id="addPropValue" placeholder="Property"><input id="addPropCanonical" placeholder="Canonical term (optional)"><input id="addPropEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addProp" class="full">Add property</button></div></div></div>
<div class="panel"><h2>Methods</h2><div id="methods"></div><div class="add"><h3>Add method</h3><div class="grid"><input id="addMethodValue" placeholder="Method"><input id="addMethodCanonical" placeholder="Canonical term (optional)"><input id="addMethodEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addMethod" class="full">Add method</button></div></div></div>
<div class="panel"><h2>Keywords / topic tags</h2><div class="tabnote">Use these for review-level classification when a concept is broader than a measured property or experimental method. Manual canonical aliases remain the strongest equivalence signal; the Multiplex Network also has a separate automatic semantic-keyword layer that can infer equivalent/related concepts without rewriting these curated terms.</div><div id="keywords"></div><div class="add"><h3>Add keyword</h3><div class="grid"><input id="addKeywordValue" placeholder="Keyword / topic"><input id="addKeywordCanonical" placeholder="Canonical term (optional)"><input id="addKeywordEvidence" class="full" placeholder="Evidence SIDs, comma separated"><button id="addKeyword" class="full">Add keyword</button></div></div></div>
<div class="panel"><h2>Claim proofreading</h2><div class="tabnote">Blue text is the immutable LLM extraction. The editable fields below form the curated overlay. Multiplex Network includes a separate curated-claim similarity layer, so approved/edited claim wording can affect paper-paper edges after the automatic rebuild.</div><div id="claims"></div><div class="add"><h3>Add claim</h3><textarea id="addClaimStatement" class="statement" placeholder="Claim statement"></textarea><div class="grid"><input id="addClaimType" placeholder="claim_type (optional)"><select id="addClaimStatus"><option value="edited">edited</option><option value="approved">approved</option><option value="needs_revision">needs_revision</option></select><input id="addClaimEvidence" class="full" placeholder="Evidence SIDs, comma separated"><input id="addClaimTags" class="full" placeholder="Curated tags, comma separated"><textarea id="addClaimNotes" class="full" placeholder="Review notes"></textarea><button id="addClaim" class="full">Add claim</button></div></div></div>
</main><aside><div class="sticky"><div class="panel"><h2>Change history</h2><div id="history" class="history"></div></div><div class="panel"><h2>Data locations</h2><div class="meta">Raw extraction: data/extracted/\nRaw metadata: data/metadata/\nCurated overlays: data/curated/\nAudit: data/curation/events.jsonl\nSQLite: curation_events_v4</div></div></div></aside></div>
<script>
const $=id=>document.getElementById(id);let current=null;const params=new URLSearchParams(location.search);const requestedPaper=params.get('paper')||'';const requestedEntity=params.get('entity')||'';
function applyTheme(theme,persist=true){const next=theme==='light'?'light':'dark';document.documentElement.dataset.theme=next;$('themeToggle').textContent=next==='dark'?'Light mode':'Dark mode';$('themeToggle').setAttribute('aria-label',`Switch to ${next==='dark'?'light':'dark'} mode`);if(persist)localStorage.setItem('foliosort-curation-theme',next)}
$('themeToggle').onclick=()=>applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');applyTheme(document.documentElement.dataset.theme,false);
function escList(v){return (v||'').split(',').map(x=>x.trim()).filter(Boolean)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opts={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function status(s,ok=true){$('status').textContent=s;$('status').className=ok?'ok':'notice'}
async function post(path,payload){try{status('Saving…');const j=await api(path,{method:'POST',body:JSON.stringify(payload)});await loadPaper(payload.paper_id||$('paper').value);const projects=(j.rebuild_projects||[]).join(', ');status(projects?`Saved. Network rebuild queued: ${projects}. Open Multiplex Network pages will refresh automatically when ready.`:'Saved. Raw extraction / imported metadata unchanged.');await refreshRebuildStatus()}catch(e){status(String(e),false)}}
async function refreshRebuildStatus(){try{const j=await api('/api/rebuild_status');let text='';if(j.running)text=`Rebuilding: ${j.running}`;else if((j.pending||[]).length)text=`Queued: ${j.pending.join(', ')}`;else if(j.last_project)text=`Network ${j.last_ok?'updated':'failed'}: ${j.last_project}`;$('rebuildState').textContent=text;$('rebuildState').className=j.last_ok===false?'notice':'small'}catch(e){}}
$('rebuildNow').onclick=async()=>{try{const j=await api('/api/rebuild_networks',{method:'POST',body:JSON.stringify({paper_id:$('paper').value})});status(`Network rebuild queued: ${(j.rebuild_projects||[]).join(', ')}`);await refreshRebuildStatus()}catch(e){status(String(e),false)}};setInterval(refreshRebuildStatus,2000);
async function loadPapers(){const j=await api('/api/papers');$('paper').replaceChildren(...j.papers.map(p=>{const o=document.createElement('option');o.value=p.paper_id;o.textContent=`${p.paper_id} — ${p.year??'?'} — ${p.title||''}`;return o}));if(j.papers.length){if(requestedPaper&&j.papers.some(p=>p.paper_id===requestedPaper))$('paper').value=requestedPaper;await loadPaper($('paper').value)}}
function authorText(authors){return (authors||[]).map(a=>{if(typeof a==='string')return a;const fam=a.family||a.surname||'';const given=a.given||a.given_name||'';if(fam&&given)return `${fam}, ${given}`;return a.full_name||a.display_name||fam||given||''}).filter(Boolean).join('\n')}
function firstFamily(authors){const a=(authors||[])[0];if(!a)return '';if(typeof a==='string'){const x=a.trim();return x.includes(',')?x.split(',')[0].trim():(x.split(/\s+/).pop()||'')}return a.family||a.surname||((a.full_name||a.display_name||'').trim().split(/\s+/).pop()||'')}
function updateMetadataPreview(){const lines=$('metaAuthors').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),fam=firstFamily(lines),year=$('metaYear').value.trim()||'?';$('metaPreview').textContent=`Graph label preview: ${fam||current?.paper_id||'?'}, ${year}`}
function renderMetadata(meta){const c=meta?.canonical||{},o=meta?.canonical_original||{};$('metaTitle').value=c.title||'';$('metaYear').value=c.year??'';$('metaJournal').value=c.journal||'';$('metaDoi').value=c.doi||'';$('metaAuthors').value=authorText(c.authors||[]);$('metaYear').classList.toggle('missing',c.year===null||c.year===undefined||c.year==='');const rawAuthors=authorText(o.authors||[])||'(missing)';$('metadataOriginal').textContent=`Imported / raw metadata:
Title: ${o.title||'(missing)'}
Authors: ${rawAuthors}
Year: ${o.year??'(missing)'}
Journal: ${o.journal||'(missing)'}
DOI: ${o.doi||'(missing)'}`;updateMetadataPreview();}
async function fetchMetadataFromDoi(){const button=$('metaFetchDoi'),doi=$('metaDoi').value.trim();if(!doi){status('Enter a DOI first.',false);$('metaDoi').focus();return}const old=button.textContent;button.disabled=true;button.textContent='Fetching…';status('Fetching metadata from DOI…');try{const j=await api('/api/metadata/fetch_doi',{method:'POST',body:JSON.stringify({paper_id:current.paper_id,doi})}),m=j.metadata||{};$('metaTitle').value=m.title||'';$('metaYear').value=m.year??'';$('metaJournal').value=m.journal||'';$('metaDoi').value=m.doi||doi;$('metaAuthors').value=authorText(m.authors||[]);$('metaYear').classList.toggle('missing',!m.year);if(!$('metaReason').value.trim())$('metaReason').value=`Metadata fetched from DOI via ${(j.providers||[]).join(' + ')}`;updateMetadataPreview();status(`Fetched metadata from ${(j.providers||[]).join(' + ')}. Review the fields, then click Save metadata correction.`)}catch(e){status(String(e),false)}finally{button.disabled=false;button.textContent=old}}
function renderValidation(validation,human){const v=validation||{},errors=v.errors||[],warnings=v.warnings||[],statusText=v.overall_status||'not generated';$('validationSummary').innerHTML=`<b>Automatic status: ${esc(statusText)}</b> · ${errors.length} error${errors.length===1?'':'s'} · ${warnings.length} warning${warnings.length===1?'':'s'}<br><span class="meta">Errors usually require checking the cited evidence or claim. Warnings are conservative prompts and may be acceptable after inspection.</span>`;const rows=[...errors.map(x=>({level:'error',...x})),...warnings.map(x=>({level:'warning',...x}))];$('validationIssues').innerHTML=rows.slice(0,80).map(x=>`<div class="validationIssue ${x.level==='error'?'error':''}"><b>${esc(x.level.toUpperCase())}: ${esc(x.type||'validation issue')}</b>${x.item_id?` · ${esc(x.item_id)}`:''}<br>${esc(JSON.stringify(x))}</div>`).join('')+(rows.length>80?`<div class="meta">${rows.length-80} additional issues are available in data/extracted/${esc(current?.paper_id||'Pxxxx')}.validation.json.</div>`:'')||'<div class="ok">No automatic validation issues.</div>';$('validationDecision').value=human?.decision||'pending';$('validationNotes').value=human?.notes||'';}
function termRow(item,type){const d=document.createElement('div');d.className='item';d.dataset.entity=item.curation_uid||'';const keys={property:['property_raw','property_normalized'],method:['method_raw','method_normalized'],keyword:['keyword_raw','keyword_normalized']}[type];const [rawKey,normKey]=keys;const origKey=normKey+'_original';const top=document.createElement('div');top.className='meta';top.innerHTML=`<span class="raw">raw: ${esc(item[rawKey]||'')}</span><br>LLM normalized: ${esc(item[origKey]??item[normKey]??'')}<br>source: ${esc(item.canonical_source||'')}<br>uid: ${esc(item.curation_uid||'')}`;const inp=document.createElement('input');inp.value=item[normKey]||'';inp.style.width='100%';const reason=document.createElement('input');reason.placeholder='Reason (recommended)';reason.style.width='100%';const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save canonical override';save.onclick=()=>post('/api/term/override',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,canonical:inp.value,reason:reason.value});const del=document.createElement('button');del.textContent='Hide from curated view';del.className='danger';del.onclick=()=>{if(confirm('Hide this term from the curated view? Raw extraction remains unchanged.'))post('/api/term/delete',{paper_id:current.paper_id,entity_type:type,entity_uid:item.curation_uid,reason:reason.value})};buttons.append(save,del);d.append(top,inp,reason,buttons);return d}
function labeledInput(label,value){const wrap=document.createElement('div');wrap.className='field';const lab=document.createElement('label');lab.textContent=label;const inp=document.createElement('input');inp.value=value??'';wrap.append(lab,inp);return [wrap,inp]}
function claimRow(item){const d=document.createElement('div');d.className='item';d.dataset.entity=item.curation_uid||'';const meta=document.createElement('div');meta.className='meta';meta.textContent=`${item.claim_id||''} | uid: ${item.curation_uid||''}\nEvidence: ${(item.evidence_sids||[]).join(', ')}`;const original=document.createElement('div');original.className='original';original.textContent='Original LLM claim:\n'+(item.statement_original??item.statement??'');const ta=document.createElement('textarea');ta.className='statement';ta.value=item.statement||'';const [typeWrap,typeInp]=labeledInput('Claim type',item.claim_type);const [subWrap,subInp]=labeledInput('Subject',item.subject);const [relWrap,relInp]=labeledInput('Relation',item.relation);const [objWrap,objInp]=labeledInput('Object',item.object);const [condWrap,condInp]=labeledInput('Conditions / scope',item.conditions_text);const [tagsWrap,tagsInp]=labeledInput('Curated tags',(item.curated_tags||[]).join(', '));const statusWrap=document.createElement('div');statusWrap.className='field';const statusLab=document.createElement('label');statusLab.textContent='Review status';const statusSel=document.createElement('select');for(const value of ['unreviewed','edited','approved','needs_revision','rejected']){const o=document.createElement('option');o.value=value;o.textContent=value;statusSel.append(o)}statusSel.value=item.review_status||'unreviewed';statusWrap.append(statusLab,statusSel);const notes=document.createElement('textarea');notes.className='statement';notes.placeholder='Reviewer notes';notes.value=item.review_notes||'';const reason=document.createElement('input');reason.placeholder='Reason for this change (recommended)';reason.style.width='100%';const fields=document.createElement('div');fields.className='grid';fields.append(typeWrap,statusWrap,subWrap,relWrap,objWrap,condWrap,tagsWrap);tagsWrap.classList.add('full');const buttons=document.createElement('div');buttons.className='grid';const save=document.createElement('button');save.textContent='Save proofread claim';save.className='accent';save.onclick=()=>post('/api/claim/edit',{paper_id:current.paper_id,entity_uid:item.curation_uid,statement:ta.value,claim_type:typeInp.value,subject:subInp.value,relation:relInp.value,object:objInp.value,conditions_text:condInp.value,tags:escList(tagsInp.value),review_status:statusSel.value,review_notes:notes.value,reason:reason.value});const restore=document.createElement('button');restore.textContent='Restore raw wording';restore.onclick=()=>{if(confirm('Restore the editable fields to the original LLM extraction? This restoration is also logged.'))post('/api/claim/restore',{paper_id:current.paper_id,entity_uid:item.curation_uid,reason:reason.value})};const del=document.createElement('button');del.textContent='Hide from curated view';del.className='danger full';del.onclick=()=>{if(confirm('Hide this claim from the curated view? Raw extraction remains unchanged.'))post('/api/claim/delete',{paper_id:current.paper_id,entity_uid:item.curation_uid,reason:reason.value})};buttons.append(save,restore,del);d.append(meta,original,ta,fields,notes,reason,buttons);return d}
function renderHistory(events){const root=$('history');root.replaceChildren();for(const e of [...events].reverse()){const d=document.createElement('div');d.className='event';const t=document.createElement('div');t.textContent=`${e.created_at} — ${e.event_type}`;const m=document.createElement('div');m.className='meta';m.textContent=`${e.entity_type||''} ${e.entity_uid||''}\nactor: ${e.actor||''}\nold: ${JSON.stringify(e.old??'').slice(0,700)}\nnew: ${JSON.stringify(e.new??'').slice(0,700)}\nreason: ${e.reason||''}`;d.append(t,m);root.append(d)}if(!events.length)root.textContent='No edits yet.'}
async function loadPaper(id){const j=await api('/api/paper?id='+encodeURIComponent(id));current=j;renderMetadata(j.metadata||{});renderValidation(j.validation||{},j.human_review||null);$('properties').replaceChildren(...(j.inventory.studied_properties||[]).map(x=>termRow(x,'property')));$('methods').replaceChildren(...(j.inventory.methods||[]).map(x=>termRow(x,'method')));$('keywords').replaceChildren(...(j.inventory.keywords||[]).map(x=>termRow(x,'keyword')));$('claims').replaceChildren(...(j.evidence.claims||[]).map(claimRow));renderHistory(j.history);status(`Loaded ${id}`);if(requestedEntity){const target=document.querySelector(`[data-entity="${CSS.escape(requestedEntity)}"]`);if(target){target.classList.add('highlight');target.scrollIntoView({behavior:'smooth',block:'center'})}}}
$('metaSave').onclick=()=>post('/api/metadata/edit',{paper_id:current.paper_id,title:$('metaTitle').value,year:$('metaYear').value,journal:$('metaJournal').value,doi:$('metaDoi').value,authors_text:$('metaAuthors').value,reason:$('metaReason').value});
$('metaFetchDoi').onclick=fetchMetadataFromDoi;$('metaAuthors').addEventListener('input',updateMetadataPreview);$('metaYear').addEventListener('input',updateMetadataPreview);
$('metaRestore').onclick=()=>{if(confirm('Restore imported metadata values? The restoration itself will also be recorded in the audit history.'))post('/api/metadata/restore',{paper_id:current.paper_id,reason:$('metaReason').value})};
$('validationSave').onclick=()=>post('/api/validation/review',{paper_id:current.paper_id,decision:$('validationDecision').value,notes:$('validationNotes').value});
$('paper').onchange=()=>loadPaper($('paper').value);$('reload').onclick=()=>loadPaper($('paper').value);
$('aliasSave').onclick=async()=>{await post('/api/term/alias',{entity_type:$('aliasType').value,alias:$('aliasRaw').value,canonical:$('aliasCanonical').value,reason:$('aliasReason').value});$('aliasRaw').value='';$('aliasCanonical').value='';$('aliasReason').value=''};
$('addProp').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'property',value:$('addPropValue').value,canonical:$('addPropCanonical').value,evidence_sids:escList($('addPropEvidence').value)});
$('addMethod').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'method',value:$('addMethodValue').value,canonical:$('addMethodCanonical').value,evidence_sids:escList($('addMethodEvidence').value)});
$('addKeyword').onclick=()=>post('/api/term/add',{paper_id:current.paper_id,entity_type:'keyword',value:$('addKeywordValue').value,canonical:$('addKeywordCanonical').value,evidence_sids:escList($('addKeywordEvidence').value)});
$('addClaim').onclick=()=>post('/api/claim/add',{paper_id:current.paper_id,statement:$('addClaimStatement').value,claim_type:$('addClaimType').value,evidence_sids:escList($('addClaimEvidence').value),tags:escList($('addClaimTags').value),review_status:$('addClaimStatus').value,review_notes:$('addClaimNotes').value});
loadPapers().then(refreshRebuildStatus).catch(e=>status(String(e),false));
</script></body></html>'''


def _author_display(author: Any) -> str:
    if isinstance(author, str):
        return author.strip()
    if not isinstance(author, dict):
        return str(author or "").strip()
    family = str(author.get("family") or author.get("surname") or "").strip()
    given = str(author.get("given") or author.get("given_name") or "").strip()
    if family and given:
        return f"{family}, {given}"
    return str(author.get("full_name") or author.get("display_name") or family or given or "").strip()


def _parse_authors_text(value: str) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "," in line:
            family, given = [x.strip() for x in line.split(",", 1)]
            row: dict[str, Any] = {"family": family, "full_name": f"{given} {family}".strip()}
            if given:
                row["given"] = given
        else:
            parts = line.split()
            family = parts[-1] if parts else line
            row = {"family": family, "full_name": line}
            if len(parts) > 1:
                row["given"] = " ".join(parts[:-1])
        authors.append(row)
    return authors


class App:
    def __init__(self, config_path: str):
        self.config, self.root = load_config(config_path)
        self.paths = get_paths(self.config, self.root)
        cfg = self.config.get("curation", {})
        self.curated_dir = self.paths.get("curated", self.root / "data/curated")
        self.events_path = self.paths.get("curation", self.root / "data/curation") / "events.jsonl"
        self.ontology_path = self.root / cfg.get("ontology_path", "profiles/peg/ontology/terms.json")
        self.metadata_dir = self.paths.get("metadata", self.root / "data/metadata")
        self.actor = cfg.get("actor") or None
        self.auto_rebuild_network = bool(cfg.get("auto_rebuild_literature_network", True))
        self.rebuild_debounce_seconds = float(cfg.get("network_rebuild_debounce_seconds", 1.5))
        self._rebuild_lock = threading.Lock()
        self._rebuild_pending: set[str] = set()
        self._rebuild_thread: threading.Thread | None = None
        self._rebuild_running: str | None = None
        self._rebuild_last_project: str | None = None
        self._rebuild_last_ok: bool | None = None
        self._rebuild_last_error: str | None = None
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)
        conn = self.db()
        ensure_curation_schema(conn)
        conn.close()

    def db(self):
        return connect_db(self.paths["database"])

    def metadata_from_doi(self, conn, doi_value: str) -> dict[str, Any]:
        doi = valid_doi(doi_value)
        cfg = self.config.get("metadata_enrichment", {})
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        providers = (
            ("Crossref", CrossrefClient, cfg.get("crossref", {})),
            ("OpenAlex", OpenAlexClient, cfg.get("openalex", {})),
        )
        for name, client_class, provider_cfg in providers:
            if not provider_cfg.get("enabled", True):
                continue
            try:
                candidate = client_class(conn, provider_cfg).by_doi(doi)
                if candidate:
                    candidates.append(candidate)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if not candidates:
            detail = f" ({'; '.join(errors)})" if errors else ""
            raise LookupError(f"No publication metadata was found for DOI {doi}.{detail}")

        def first_value(field: str, default: Any = None) -> Any:
            for candidate in candidates:
                value = candidate.get(field)
                if value not in (None, "", []):
                    return value
            return default

        def plain_text(value: Any) -> str:
            without_tags = re.sub(r"<[^>]*>", "", str(value or ""))
            return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()

        return {
            "metadata": {
                "title": plain_text(first_value("title", "")),
                "year": first_value("year"),
                "journal": plain_text(first_value("journal", "")),
                "doi": doi,
                "authors": first_value("authors", []),
            },
            "providers": [str(candidate.get("provider") or "unknown") for candidate in candidates],
            "warnings": errors,
        }

    def paper_ids(self) -> list[str]:
        conn = self.db()
        try:
            return [row[0] for row in conn.execute("SELECT paper_id FROM papers WHERE active=1 ORDER BY paper_id")]
        finally:
            conn.close()

    def raw_metadata_canonical(self, paper_id: str) -> dict[str, Any]:
        base: dict[str, Any] = {}
        paper_path = self.paths["paper_json"] / f"{paper_id}.json"
        if paper_path.exists():
            base.update(read_json(paper_path).get("metadata") or {})
        conn = self.db()
        try:
            row = conn.execute("SELECT title,year,journal,doi FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
            if row:
                for field in ["title", "year", "journal", "doi"]:
                    if base.get(field) in (None, "") and row[field] not in (None, ""):
                        base[field] = row[field]
        finally:
            conn.close()
        raw_path = self.metadata_dir / f"{paper_id}.metadata.json"
        if raw_path.exists():
            base.update(read_json(raw_path).get("canonical") or {})
        return base

    def current_metadata(self, paper_id: str) -> dict[str, Any]:
        raw = self.raw_metadata_canonical(paper_id)
        curated_path = self.curated_dir / f"{paper_id}.metadata.json"
        current = dict(raw)
        payload: dict[str, Any] = {}
        if curated_path.exists():
            payload = read_json(curated_path)
            current.update(payload.get("canonical") or {})
        return {
            **payload,
            "paper_id": paper_id,
            "canonical_original": raw,
            "canonical": current,
            "curation_uid": f"metadata:{paper_id}",
        }

    def paper_summaries(self) -> list[dict[str, Any]]:
        out = []
        for paper_id in self.paper_ids():
            meta = self.current_metadata(paper_id).get("canonical") or {}
            out.append({"paper_id": paper_id, "title": meta.get("title"), "year": meta.get("year"), "journal": meta.get("journal")})
        return out

    def rematerialize(self, paper_id: str | None = None) -> None:
        if paper_id:
            materialize_paper(paper_id, extracted_dir=self.paths["extracted"], curated_dir=self.curated_dir, ontology_path=self.ontology_path, events_path=self.events_path, metadata_dir=self.metadata_dir)
        else:
            materialize_all(self.paper_ids(), extracted_dir=self.paths["extracted"], curated_dir=self.curated_dir, ontology_path=self.ontology_path, events_path=self.events_path, metadata_dir=self.metadata_dir)

    def affected_projects(self, paper_id: str | None = None) -> list[str]:
        conn = self.db()
        try:
            if paper_id:
                rows = conn.execute(
                    "SELECT DISTINCT project_slug FROM paper_projects WHERE paper_id=? ORDER BY project_slug",
                    (paper_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT DISTINCT pp.project_slug
                    FROM paper_projects pp
                    JOIN papers p ON p.paper_id=pp.paper_id
                    WHERE p.active=1
                    ORDER BY pp.project_slug
                    """
                ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            conn.close()

    def pipeline_running(self) -> bool:
        lock_path = self.root / "logs" / "auto_pipeline.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False

    def schedule_network_rebuild(self, paper_id: str | None = None, *, force: bool = False) -> list[str]:
        if not self.auto_rebuild_network and not force:
            return []
        projects = self.affected_projects(paper_id)
        if not projects:
            return []
        with self._rebuild_lock:
            self._rebuild_pending.update(projects)
            if self._rebuild_thread is None or not self._rebuild_thread.is_alive():
                self._rebuild_thread = threading.Thread(target=self._network_rebuild_worker, daemon=True)
                self._rebuild_thread.start()
        return projects

    def _network_rebuild_worker(self) -> None:
        time.sleep(max(0.2, self.rebuild_debounce_seconds))
        log_path = self.root / "logs" / "curation-network-rebuild.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            if self.pipeline_running():
                time.sleep(2.0)
                continue
            with self._rebuild_lock:
                if not self._rebuild_pending:
                    self._rebuild_running = None
                    return
                slug = sorted(self._rebuild_pending)[0]
                self._rebuild_pending.discard(slug)
                self._rebuild_running = slug
            py = self.root / ".venv_network" / "bin" / "python"
            script = self.root / "scripts" / "13_build_multiplex_network.py"
            ok = False
            error = ""
            try:
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n=== CURATION NETWORK REBUILD project={slug} ===\n")
                    result = subprocess.run(
                        [
                            str(py),
                            "-u",
                            str(script),
                            "--config",
                            str(self.root / "config.json"),
                            "--project",
                            slug,
                            "--skip-ai-cluster-naming",
                        ],
                        cwd=str(self.root),
                        env={**os.environ, "REVIEW_PROJECT": slug, "REVIEW_ROOT": str(self.root)},
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=1800,
                        check=False,
                    )
                    ok = result.returncode == 0
                    if not ok:
                        error = f"network builder exited with code {result.returncode}"
            except Exception as exc:
                error = str(exc)
            with self._rebuild_lock:
                self._rebuild_last_project = slug
                self._rebuild_last_ok = ok
                self._rebuild_last_error = error or None
                self._rebuild_running = None

    def rebuild_status(self) -> dict[str, Any]:
        with self._rebuild_lock:
            return {
                "pending": sorted(self._rebuild_pending),
                "running": self._rebuild_running,
                "last_project": self._rebuild_last_project,
                "last_ok": self._rebuild_last_ok,
                "last_error": self._rebuild_last_error,
            }

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
        elif entity_type == "metadata":
            return payload.get("metadata", {}).get("canonical") or {}
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
        metadata = self.current_metadata(paper_id)
        validation_path = self.paths["extracted"] / f"{paper_id}.validation.json"
        conn = self.db()
        try:
            human_row = conn.execute(
                "SELECT decision,notes,reviewed_at FROM human_reviews WHERE paper_id=?", (paper_id,)
            ).fetchone()
        finally:
            conn.close()
        return {
            "paper_id": paper_id,
            "metadata": metadata,
            "inventory": read_json(inv) if inv.exists() else {},
            "evidence": read_json(ev) if ev.exists() else {},
            "validation": read_json(validation_path) if validation_path.exists() else {},
            "human_review": dict(human_row) if human_row else None,
            "history": [x for x in read_event_log(self.events_path) if x.get("paper_id") in (None, paper_id)],
        }


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"LiteratureCuration/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("CURATION %s - %s\n" % (self.address_string(), fmt % args))

    def allowed_origin(self) -> bool:
        return browser_request_is_trusted(self.headers, self.server.server_port)

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if is_loopback_http_url(origin, self.server.server_port):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        return read_json_object(self.headers, self.rfile, max_bytes=MAX_JSON_BODY_BYTES)

    def do_OPTIONS(self) -> None:
        if not self.allowed_origin():
            self.send_json({"error": "cross-origin request denied"}, 403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

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
            self.send_json({"ok": True, "version": CURATION_APP_VERSION})
            return
        if parsed.path == "/api/papers":
            self.send_json({"papers": APP.paper_summaries()})
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
        if parsed.path == "/api/rebuild_status":
            self.send_json(APP.rebuild_status())
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        assert APP is not None
        if not self.allowed_origin():
            self.send_json({"error": "cross-origin request denied"}, 403)
            return
        conn = None
        try:
            data = self.body()
            path = urllib.parse.urlparse(self.path).path
            conn = APP.db()
            common = {"conn": conn, "events_path": APP.events_path, "actor": APP.actor}
            allowed_terms = {"property", "method", "keyword"}
            if path == "/api/rebuild_networks":
                paper_id = str(data.get("paper_id") or "") or None
                if paper_id and paper_id not in APP.paper_ids():
                    raise ValueError("valid paper_id is required")
                projects = APP.schedule_network_rebuild(paper_id, force=True)
                self.send_json({"ok": True, "rebuild_projects": projects})
                return
            if path == "/api/metadata/fetch_doi":
                paper_id = str(data.get("paper_id") or "")
                if paper_id not in APP.paper_ids():
                    raise ValueError("valid paper_id is required")
                result = APP.metadata_from_doi(conn, str(data.get("doi") or ""))
                self.send_json({"ok": True, "paper_id": paper_id, **result})
                return
            if path == "/api/validation/review":
                paper_id = str(data.get("paper_id") or "")
                decision = str(data.get("decision") or "pending")
                if paper_id not in APP.paper_ids():
                    raise ValueError("valid paper_id is required")
                if decision not in {"pending", "approved", "needs_revision", "rejected"}:
                    raise ValueError("unsupported validation decision")
                if decision == "pending":
                    conn.execute("DELETE FROM human_reviews WHERE paper_id=?", (paper_id,))
                else:
                    conn.execute(
                        """
                        INSERT INTO human_reviews(paper_id,decision,notes,reviewed_at)
                        VALUES (?,?,?,?)
                        ON CONFLICT(paper_id) DO UPDATE SET
                          decision=excluded.decision,notes=excluded.notes,reviewed_at=excluded.reviewed_at
                        """,
                        (paper_id, decision, str(data.get("notes") or "").strip(), now_iso()),
                    )
                conn.commit()
                self.send_json({"ok": True, "decision": decision, "rebuild_projects": []})
                return
            if path in {"/api/metadata/edit", "/api/metadata/restore"}:
                paper_id = str(data.get("paper_id") or "")
                if paper_id not in APP.paper_ids():
                    raise ValueError("valid paper_id is required")
                uid = f"metadata:{paper_id}"
                current = APP.current_metadata(paper_id).get("canonical") or {}
                if path.endswith("edit"):
                    year = normalize_publication_year(data.get("year"))
                    new = {
                        "title": str(data.get("title") or "").strip(),
                        "year": year,
                        "journal": str(data.get("journal") or "").strip(),
                        "doi": str(data.get("doi") or "").strip(),
                        "authors": _parse_authors_text(data.get("authors_text") or ""),
                    }
                    event = append_event(**common, event_type="metadata_edit", paper_id=paper_id, entity_type="metadata", entity_uid_value=uid, old=current, new=new, reason=data.get("reason"))
                else:
                    raw_meta = APP.raw_metadata_canonical(paper_id)
                    new = {field: raw_meta.get(field) for field in ["title", "year", "journal", "doi"]}
                    new["authors"] = list(raw_meta.get("authors") or [])
                    event = append_event(**common, event_type="metadata_edit", paper_id=paper_id, entity_type="metadata", entity_uid_value=uid, old=current, new=new, reason=data.get("reason") or "Restored imported metadata", extra={"action": "restore_imported_metadata"})
                APP.rematerialize(paper_id)
                projects = APP.schedule_network_rebuild(paper_id)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
                return
            if path == "/api/term/alias":
                term_type = data.get("entity_type")
                alias = str(data.get("alias") or "").strip()
                canonical = str(data.get("canonical") or "").strip()
                if term_type not in allowed_terms or not alias or not canonical:
                    raise ValueError("entity_type, alias and canonical are required")
                event = append_event(**common, event_type="term_alias", entity_type=term_type, new={"alias": alias, "canonical": canonical}, reason=data.get("reason"))
                APP.rematerialize()
                projects = APP.schedule_network_rebuild(None)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
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
                projects = APP.schedule_network_rebuild(paper_id)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
                return
            if path == "/api/term/add":
                paper_id = data.get("paper_id")
                term_type = data.get("entity_type")
                value = str(data.get("value") or "").strip()
                if paper_id not in APP.paper_ids() or term_type not in allowed_terms or not value:
                    raise ValueError("valid paper_id, entity_type and value are required")
                event = append_event(**common, event_type="term_add", paper_id=paper_id, entity_type=term_type, new={"value": value, "canonical": data.get("canonical"), "evidence_sids": data.get("evidence_sids") or []}, reason=data.get("reason"))
                APP.rematerialize(paper_id)
                projects = APP.schedule_network_rebuild(paper_id)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
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
                projects = APP.schedule_network_rebuild(paper_id)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
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
                projects = APP.schedule_network_rebuild(paper_id)
                self.send_json({"ok": True, "event": event, "rebuild_projects": projects})
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)
        finally:
            if conn is not None:
                conn.close()


def main() -> None:
    global APP
    ap = argparse.ArgumentParser(description="Local, append-only human curation UI for publication metadata, properties, methods, keywords, and claims.")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    APP = App(args.config)
    host = args.host or APP.config.get("curation", {}).get("feedback_bind", "127.0.0.1")
    port = args.port or int(APP.config.get("curation", {}).get("feedback_port", 8765))
    print(f"Curation UI: http://{host}:{port}")
    print("Raw extraction and imported metadata files are read-only; edits go to events.jsonl + SQLite + data/curated/.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
