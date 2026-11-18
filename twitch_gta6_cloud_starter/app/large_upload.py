"""Resumable browser uploads for large, licensed source videos.

A large request previously failed with HTTP 413. Each authenticated
multipart request is <7 MiB; completed parts stay on Render's /data disk.
No third-party footage is fetched. The browser never stores admin tokens.
"""
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Annotated

from .video_pipeline import VideoPipelineError, MAX_SOURCE_SECONDS, probe

router = APIRouter(prefix="/video", tags=["large-video-upload"])
_CHUNK_BYTES = 6 * 1024 * 1024
_UPLOAD_LIMIT = min(350, max(32, int(os.getenv("VIDEO_CHUNKED_MAX_MB", "300")))) * 1024 * 1024
_LOCK = threading.Lock()


class StartUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(gt=0)
    authorized: bool
    clip_seconds: int = Field(default=65, ge=3, le=90)
    auto_subtitles: bool | None = None
    music_enabled: bool = True
    vision_enabled: bool = False
    voiceover: bool = False
    hook_text: str = Field(default="GTA VI | BIG REACTION", max_length=80)
    caption_text: str = Field(default="", max_length=5000)
    chat_seconds: list[Annotated[float, Field(ge=0, le=MAX_SOURCE_SECONDS, allow_inf_nan=False)]] = Field(default_factory=list, max_length=25000)


def _routes():
    from . import video_routes
    return video_routes


def _metadata_path(job_id):
    return _routes()._job_dir(job_id) / "upload.json"


def _write_meta(job_id, value):
    file = _metadata_path(job_id)
    file.parent.mkdir(parents=True, exist_ok=True)
    temp = file.with_suffix(".tmp")
    temp.write_text(json.dumps(value), encoding="utf-8")
    temp.replace(file)


def _read_meta(job_id):
    path = _metadata_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "No unfinished upload with that ID")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise HTTPException(500, "Upload metadata unavailable") from exc
    if data.get("state") != "uploading":
        raise HTTPException(409, "Upload was already finalized")
    return data


def _stale_upload_cleanup():
    routes = _routes()
    if not routes.VIDEO_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    for folder in routes.VIDEO_DIR.iterdir():
        if not folder.is_dir():
            continue
        try:
            if str(uuid.UUID(folder.name)) != folder.name:
                continue
            metadata = folder / "upload.json"
            if not metadata.is_file():
                continue
            value = json.loads(metadata.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(value["created_at"])
            if value.get("state") == "uploading" and created_at < cutoff:
                shutil.rmtree(folder)
        except (ValueError, KeyError, OSError, TypeError):
            continue


@router.get("/uploader", response_class=HTMLResponse, include_in_schema=False)
def uploader_page():
    """Friendly browser workflow: chunked upload -> render -> ZIP download."""
    return UPLOADER_HTML


@router.post("/uploads/start", status_code=201)
def start_upload(data: StartUpload, x_admin_token: str | None = Header(default=None)):
    routes = _routes()
    routes._authorize(x_admin_token)
    if not data.authorized:
        raise HTTPException(403, "Confirm ownership or reuse permission for this video")
    if not data.filename.lower().endswith((".mp4", ".mov", ".mkv")):
        raise HTTPException(415, "Choose an MP4, MOV, or MKV file")
    if data.size_bytes > _UPLOAD_LIMIT:
        raise HTTPException(
            413, f"Video exceeds the browser-upload limit ({_UPLOAD_LIMIT // 1048576} MB)"
        )
    from .captions import openai_auth_status
    auto_subtitles = openai_auth_status()["configured"] if data.auto_subtitles is None else data.auto_subtitles
    if auto_subtitles and not openai_auth_status()["configured"]:
        raise HTTPException(503, "OpenAI transcription is not configured")
    routes.VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        routes._cleanup_finished_jobs()
        _stale_upload_cleanup()
        reserve = 180 * 1024 * 1024
        if shutil.disk_usage(routes.VIDEO_DIR).free < data.size_bytes + reserve:
            raise HTTPException(
                507, "Not enough disk space; download and clear old video jobs or compress your source"
            )
        job_id = str(uuid.uuid4())
        folder = routes._job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        now = datetime.now(timezone.utc).isoformat()
        meta = {
            "state": "uploading",
            "created_at": now,
            "expected_bytes": data.size_bytes,
            "received_bytes": 0,
            "next_index": 0,
            "authorized": True,
            "filename": Path(data.filename).name[:160],
            "clip_seconds": data.clip_seconds,
            "auto_subtitles": bool(auto_subtitles),
            "music_enabled": bool(data.music_enabled),
            "vision_enabled": data.vision_enabled,
            "voiceover": data.voiceover,
            "hook_text": data.hook_text,
            "caption_text": data.caption_text,
            "chat_seconds": data.chat_seconds,
            "subtitles_required": data.auto_subtitles is True,
        }
        _write_meta(job_id, meta)
        routes._write_status(folder, {
            "job_id": job_id, "state": "uploading", "created_at": now,
            "received_bytes": 0, "expected_bytes": data.size_bytes,
            "upload_progress": 0.0,
            "authorized": True,
            "original_filename": meta["filename"],
        })
    return {
        "job_id": job_id,
        "state": "uploading",
        "chunk_size_bytes": _CHUNK_BYTES,
        "max_upload_mb": _UPLOAD_LIMIT // (1024 * 1024),
    }


@router.post("/uploads/{job_id}/chunk")
def upload_chunk(job_id: str, index: int = Query(ge=0),
                 file: UploadFile = File(...),
                 x_admin_token: str | None = Header(default=None)):
    routes = _routes()
    routes._authorize(x_admin_token)
    with _LOCK:
        meta = _read_meta(job_id)
        if index < meta["next_index"]:
            # A retry is safe if an HTTP response was lost after successful write.
            return {"state": "uploading", "already_received": True,
                    "received_bytes": meta["received_bytes"],
                    "next_index": meta["next_index"]}
        if index > meta["next_index"]:
            raise HTTPException(409, "Unexpected chunk index. Resume from next_index")
        path = routes._job_dir(job_id) / "source.part"
        before = path.stat().st_size if path.exists() else 0
        if before != meta["received_bytes"]:
            raise HTTPException(409, "Disk upload mismatch; start a new session")
        received = 0
        try:
            with path.open("ab") as destination:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if (received > _CHUNK_BYTES
                            or before + received > meta["expected_bytes"]
                            or before + received > _UPLOAD_LIMIT):
                        raise HTTPException(413, "Chunk exceeds accepted size")
                    destination.write(chunk)
            if received == 0:
                raise HTTPException(422, "Empty upload chunk")
        except Exception:
            if path.exists():
                with path.open("r+b") as destination:
                    destination.truncate(before)
            raise
        finally:
            file.file.close()
        meta["received_bytes"] = before + received
        meta["next_index"] = index + 1
        _write_meta(job_id, meta)
        status = routes._read_status(job_id)
        status["received_bytes"] = meta["received_bytes"]
        status["upload_progress"] = round(
            100 * meta["received_bytes"] / meta["expected_bytes"], 1
        )
        routes._write_status(routes._job_dir(job_id), status)
        return {
            "state": "uploading",
            "received_bytes": meta["received_bytes"],
            "next_index": meta["next_index"],
            "upload_progress": status["upload_progress"],
        }


def _finalize_completed_upload(job_id: str, background_tasks: BackgroundTasks | None = None):
    """Finalize a trusted on-disk upload once, optionally during server startup.

    The web route authorizes before calling this. Startup only scans upload
    sessions previously marked authorized and completely received on disk.
    """
    routes = _routes()
    with _LOCK:
        meta = _read_meta(job_id)
        folder = routes._job_dir(job_id)
        temp_file = folder / "source.part"
        if not temp_file.is_file() or temp_file.stat().st_size != meta["expected_bytes"]:
            raise HTTPException(409, "Upload is incomplete; send the remaining chunks")
        if not meta.get("authorized"):
            raise HTTPException(403, "Upload is not authorized for processing")
        try:
            info = probe(temp_file)
        except VideoPipelineError as exc:
            raise HTTPException(422, "The completed upload is not a valid playable video") from exc
        if info["duration"] > MAX_SOURCE_SECONDS:
            raise HTTPException(422, "Source video exceeds the 12-hour input limit")
        if info["duration"] < meta["clip_seconds"] - 0.1 and meta["clip_seconds"] >= 61:
            raise HTTPException(422, "Video must be at least as long as the requested clip")
        if meta.get("chat_seconds"):
            (folder / "chat-seconds.json").write_text(json.dumps(meta["chat_seconds"]), encoding="utf-8")
        temp_file.rename(folder / "source.mp4")
        status = routes._read_status(job_id)
        status.update({
            "state": "queued",
            "upload_progress": 100.0,
            "input_bytes": meta["expected_bytes"],
            "requested_duration_seconds": meta["clip_seconds"],
            "auto_subtitles_requested": meta["auto_subtitles"],
            "subtitles_required": meta.get("subtitles_required", False),
            "vision_enabled": meta.get("vision_enabled", False),
            "voiceover": meta.get("voiceover", False),
            "music_original_generated": meta["music_enabled"],
            "hook_text": meta["hook_text"],
            "publication_status": "not_ready",
        })
        routes._write_status(folder, status)
        _metadata_path(job_id).unlink(missing_ok=True)
    options = {
        "auto_subtitles": meta["auto_subtitles"],
        "music_enabled": meta["music_enabled"],
        "hook_text": meta["hook_text"],
    }
    if background_tasks is not None:
        background_tasks.add_task(
            routes._worker, job_id, meta["caption_text"], meta["clip_seconds"],
            **options,
        )
    else:
        # Recover a fully-uploaded, previously rejected source after deploy,
        # without exposing admin credentials or demanding another upload.
        threading.Thread(
            target=routes._worker,
            args=(job_id, meta["caption_text"], meta["clip_seconds"]),
            kwargs=options,
            daemon=True,
            name="recovered-video-" + job_id[:8],
        ).start()
    return {
        "job_id": job_id, "state": "queued",
        "status_endpoint": "/video/jobs/" + job_id,
        "export_endpoint": "/video/jobs/" + job_id + "/export-link",
    }


@router.post("/uploads/{job_id}/finish", status_code=202)
def finish_upload(job_id: str, background_tasks: BackgroundTasks,
                  x_admin_token: str | None = Header(default=None)):
    _routes()._authorize(x_admin_token)
    return _finalize_completed_upload(job_id, background_tasks)


def recover_completed_uploads():
    """At startup, resume eligible 100%-received sessions blocked by old code.

    Incomplete uploads are intentionally left unchanged. Does not reprocess
    completed or failed jobs, and requires stored permission confirmation.
    """
    routes = _routes()
    if not routes.VIDEO_DIR.is_dir():
        return []
    recovered = []
    for metadata in sorted(routes.VIDEO_DIR.glob("*/upload.json")):
        try:
            job_id = metadata.parent.name
            if str(uuid.UUID(job_id)) != job_id:
                continue
            data = _read_meta(job_id)
            file = metadata.parent / "source.part"
            if (
                data.get("authorized") is True
                and data.get("received_bytes") == data.get("expected_bytes")
                and file.is_file()
                and file.stat().st_size == data.get("expected_bytes")
            ):
                _finalize_completed_upload(job_id)
                recovered.append(job_id)
                print("Recovered uploaded video job:", job_id)
        except Exception as error:
            print("Video upload recovery skipped:", metadata.parent.name, type(error).__name__)
    return recovered


UPLOADER_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GTA VI — Upload & Montage</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif}
body{margin:0;background:#090b12;color:#f0f3f8;padding:28px 12px}
main{max-width:740px;margin:auto;background:#171b26;border:1px solid #363e51;padding:28px;border-radius:18px}
h1{font-size:25px;margin:0 0 10px} p{color:#b8c2d2;line-height:1.55}
label{display:block;margin-top:18px;margin-bottom:6px;font-weight:650}
input[type=password],input[type=number],input[type=text],input[type=file],select{box-sizing:border-box;width:100%;padding:12px;background:#0e1321;border:1px solid #535d75;border-radius:9px;color:white;font-size:15px}
input[type=checkbox]{width:20px;height:20px;vertical-align:middle}
button,.download{display:inline-block;margin-top:19px;padding:12px 18px;background:#6179ff;color:white;border:0;border-radius:9px;font-weight:700;font-size:15px;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
[hidden]{display:none!important}
.controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.controls input{flex:1;min-width:220px}
button.secondary{background:#313f5f;margin-top:0}
button.secondary:hover{background:#40537e}
#jobPanel{margin-top:20px;padding:15px;background:#101d2c;border:1px solid #485c79;border-radius:11px}
#jobId{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
details{margin-top:18px}
summary{cursor:pointer;color:#c8d7fa;font-weight:650}
progress{margin-top:18px;width:100%;height:20px}
#status{white-space:pre-line;margin-top:18px;padding:12px;background:#0c1420;border-radius:9px}
.muted{font-size:13px;color:#a7b6c9}.ok{color:#8ce9ae}.err{color:#ff9a9a}
</style>
</head>
<body><main>
<h1>GTA VI — Montage automatique</h1>
<p>Envoie une vidéo autorisée (jusqu'à 300 Mo, même si elle dure plus de 15 minutes) ; le serveur la reçoit en morceaux, sélectionne un extrait, ajoute sous-titres anglais et musique, puis génère un MP4 de 65 secondes et un ZIP prêt à publier.</p><p class="muted">La sélection combine les variations sonores, les changements d’image et les pics du chat fourni. Les longues vidéos sont échantillonnées sur toute leur durée.</p>
<label for="token">X-Admin-Token (depuis Render → Environment → POLL_ADMIN_TOKEN)</label>
<div class="controls">
<input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="Jeton privé, jamais sauvegardé">
<button class="secondary" id="toggleToken" type="button" aria-label="Afficher ou masquer le token">Afficher</button>
<button class="secondary" id="copyToken" type="button">Copier le token saisi</button>
</div>
<p class="muted">Le token est uniquement celui que tu colles ici. Le serveur ne révèle jamais POLL_ADMIN_TOKEN. Évite de partager ton presse-papiers.</p>
<label for="video">Vidéo source (MP4, MOV ou MKV)</label><input id="video" type="file" accept=".mp4,.mov,.mkv,video/*">
<label for="chat">Chat horodaté (JSON facultatif)</label><input id="chat" type="file" accept=".json,application/json">
<p class="muted">Liste des secondes de chaque message depuis le début de la vidéo, par exemple [12.3, 14, 14.5].</p>
<label for="seconds">Durée du clip (secondes)</label><input id="seconds" type="number" min="3" max="90" value="65">
<label for="hook">Texte d'accroche</label><input id="hook" type="text" maxlength="80" value="GTA VI | BIG REACTION">
<label><input id="voiceover" type="checkbox" checked> Lire l’accroche en voix off anglaise</label>
<label for="subtitles">Sous-titres anglais</label>
<select id="subtitles"><option value="auto">Automatiques si OpenAI configuré</option><option value="true">Obligatoires</option><option value="false">Désactivés</option></select>
<label><input id="vision" type="checkbox"> Faire comparer les images des moments candidats par OpenAI</label>
<label><input id="music" type="checkbox" checked> Ajouter une musique originale discrète</label>
<label><input id="rights" type="checkbox"> Je possède les droits ou l'autorisation de réutiliser cette vidéo</label>
<button id="send">Tout exécuter : importer → monter → télécharger</button>
<progress id="progress" max="100" value="0" hidden></progress>
<div id="status" role="status" aria-live="polite">Aucune vidéo envoyée.</div>
<section id="jobPanel" hidden aria-label="Identifiant de montage">
<label for="jobId">job_id — identifiant de ta vidéo</label>
<div class="controls">
<input id="jobId" type="text" readonly spellcheck="false" aria-label="Job ID à copier">
<button class="secondary" id="copyJob" type="button">Copier le job_id</button>
</div>
<p class="muted">Garde cet identifiant pour récupérer le montage, même après avoir fermé cette page.</p>
</section>
<details>
<summary>J'ai déjà un job_id — retrouver le montage</summary>
<label for="existingJob">Coller un ancien job_id</label>
<div class="controls">
<input id="existingJob" type="text" spellcheck="false" placeholder="00000000-0000-0000-0000-000000000000">
<button class="secondary" id="resumeJob" type="button">Vérifier ce job_id</button>
</div>
</details>
<div id="result"></div>
<p class="muted">Aucun mot de passe n'est enregistré dans le navigateur. Les vidéos de test ne sont pas publiées automatiquement. Les fichiers terminés sont temporaires (48 h par défaut).</p>
</main>
<script>
(function(){
 const el=id=>document.getElementById(id);
 const send=el('send'), status=el('status'), result=el('result'), progress=el('progress');
 const jobField=el('jobId'), jobPanel=el('jobPanel');
 function show(message,error){status.textContent=message;status.className=error?'err':'ok'}
 function showJob(job){
   jobField.value=job;
   jobPanel.hidden=false;
 }
 async function copyText(value){
   if(!value)throw Error('Aucune valeur à copier.');
   if(navigator.clipboard&&navigator.clipboard.writeText){
     await navigator.clipboard.writeText(value);
     return;
   }
   // Safari clipboard fallback; the page never persists entered secrets.
   const temp=document.createElement('textarea');
   temp.value=value;
   temp.setAttribute('readonly','');
   temp.style.position='fixed';temp.style.left='-9999px';
   document.body.appendChild(temp);
   temp.select();
   const success=document.execCommand('copy');
   temp.remove();
   if(!success)throw Error('Copie impossible : sélectionne la valeur puis fais Cmd+C.');
 }
 async function copyButton(button,value,label){
   try{
     await copyText(value);
     button.textContent='Copié !';
     window.setTimeout(()=>{button.textContent=label},1800);
   }catch(e){show(e.message,true)}
 }
 el('toggleToken').addEventListener('click',()=>{
   const input=el('token'), visible=input.type==='text';
   input.type=visible?'password':'text';
   el('toggleToken').textContent=visible?'Afficher':'Masquer';
 });
 el('copyToken').addEventListener('click',()=>{
   copyButton(el('copyToken'),el('token').value.trim(),'Copier le token saisi');
 });
 el('copyJob').addEventListener('click',()=>{
   copyButton(el('copyJob'),jobField.value,'Copier le job_id');
 });
 async function api(path,options){
   const token=el('token').value.trim();
   const headers=Object.assign({'X-Admin-Token':token},options&&options.headers||{});
   const r=await fetch(path,Object.assign({},options||{},{headers:headers,cache:'no-store'}));
   if(!r.ok){
     const info=await r.text();
     let message=info.slice(0,300);
     try{const parsed=JSON.parse(info);message=typeof parsed.detail==='string'?parsed.detail:message}catch(e){}
     throw Error('HTTP '+r.status+' — '+message);
   }
   return r.json();
 }
 async function sendChunk(job,index,blob){
   for(let attempt=0;attempt<3;attempt++){
     try{
       const form=new FormData();
       form.append('file',blob,'segment-'+index+'.part');
       return await api('/video/uploads/'+job+'/chunk?index='+index,{method:'POST',body:form});
     }catch(error){
       if(attempt===2 || /^HTTP (401|403|409|413|422|507)/.test(error.message))throw error;
       await new Promise(resolve=>setTimeout(resolve,1000*(attempt+1)));
     }
   }
 }
 async function monitor(job){
   showJob(job);
   for(let count=0;count<180;count++){
     const state=await api('/video/jobs/'+job);
     show('Job : '+job+'\nStatut : '+state.state+'\n'+
       (state.result?('Durée : '+state.result.duration_seconds+' secondes\nSous-titres : '+state.result.caption_mode):'')+
       (state.transcription_warning?'\nAttention : transcription indisponible ('+state.transcription_warning+'); MP4 quand même produit.':''));
     if(state.state==='done'){
       const analysis=state.result.highlight_analysis;
       const summary=['Durée : '+state.result.duration_seconds+' s', 'Sous-titres : '+state.result.caption_mode];
       if(Number.isFinite(state.result.start_seconds))summary.push('Début du clip : '+state.result.start_seconds+' s');
       if(analysis){
         summary.push('Moment retenu : '+analysis.event_seconds+' s');
         summary.push('Durée échantillonnée : '+analysis.sampled_source_seconds+' / '+analysis.source_duration_seconds+' s');
         summary.push(analysis.chat_peaks.length
           ? 'Pics du chat : '+analysis.chat_peaks.map(p=>p.at_seconds+' s ('+p.messages+' messages en 5 s)').join(', ')
           : (analysis.chat_messages?'Aucun pic du chat détecté.':'Aucun chat fourni.'));
       }
       if(analysis?.vision)summary.push('Analyse des images : '+analysis.vision.reason);
       if(analysis?.vision_warning)summary.push(analysis.vision_warning);
       if(state.result.voiceover_warning)summary.push(state.result.voiceover_warning);
       if(state.result.caption_warning)summary.push('Sous-titres indisponibles — '+state.result.caption_warning);
       show('Job : '+job+'\nMontage terminé.\n'+summary.join('\n'));
       const download=document.createElement('button');
       download.textContent='Télécharger le MP4';
       download.title='Obtenir un lien MP4 seul';
       download.onclick=async()=>{
         download.disabled=true;
         try{
           const response=await fetch('/video/jobs/'+job+'/download',{headers:{'X-Admin-Token':el('token').value.trim()}});
           if(!response.ok)throw Error('Téléchargement MP4 — HTTP '+response.status);
           const url=URL.createObjectURL(await response.blob());
           const link=document.createElement('a');
           link.href=url;link.download='gta6-highlight-'+job.slice(0,8)+'.mp4';link.click();
           setTimeout(()=>URL.revokeObjectURL(url),60000);
         }catch(error){show(error.message,true)}
         finally{download.disabled=false}
       };
       result.replaceChildren(download);
       download.click();
       try{
         const link=await api('/video/jobs/'+job+'/export-link',{method:'POST'});
         const a=document.createElement('a');
         a.href=link.url;a.target='_blank';a.rel='noopener noreferrer';
         a.textContent='Télécharger le ZIP';a.className='download';
         result.append(a);
       }catch(error){
         const message=document.createElement('p');
         message.textContent='ZIP indisponible — '+error.message;
         result.append(message);
       }
       return;
     }
     if(state.state==='failed'){
       if(state.source_retained_for_retry){
         result.replaceChildren();
         const retry=document.createElement('button');
         retry.textContent='Relancer le montage sans renvoyer la vidéo';
         retry.className='secondary';
         retry.addEventListener('click',async()=>{
           retry.disabled=true;
           try{
             await api('/video/jobs/'+job+'/retry',{method:'POST'});
             result.replaceChildren();
             await monitor(job);
           }catch(e){show('Erreur : '+e.message,true);retry.disabled=false}
         });
         const noSpeech=document.createElement('button');
         noSpeech.textContent='Relancer sans transcription';
         noSpeech.className='secondary';
         noSpeech.style.marginLeft='9px';
         noSpeech.addEventListener('click',async()=>{
           noSpeech.disabled=true;
           try{
             await api('/video/jobs/'+job+'/retry',{method:'POST',
               headers:{'Content-Type':'application/x-www-form-urlencoded'},
               body:'auto_subtitles=false'});
             result.replaceChildren();
             await monitor(job);
           }catch(e){show('Erreur : '+e.message,true);noSpeech.disabled=false}
         });
         result.append(retry,noSpeech);
       }
       throw Error('Le montage a échoué ('+(state.error_detail||state.error||'erreur serveur')+'). Job : '+job);
     }
     await new Promise(resolve=>setTimeout(resolve,6000));
   }
   show('Montage encore en cours. Note ton job_id pour vérifier son statut : '+job);
 }
 const providedJob=new URLSearchParams(window.location.search).get('job_id');
 if(providedJob && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(providedJob)){
   el('existingJob').value=providedJob;
   el('existingJob').closest('details').open=true;
   showJob(providedJob);
   show('Import retrouvé : colle ton token puis clique sur Vérifier ce job_id.');
 }
 el('resumeJob').addEventListener('click',async()=>{
   const token=el('token').value.trim(),job=el('existingJob').value.trim();
   if(!token){show("Colle ton token d'administration dans le champ en haut.",true);return}
   if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(job)){
     show('job_id incorrect : colle l’identifiant complet de ta vidéo.',true);return;
   }
   const button=el('resumeJob');button.disabled=true;result.replaceChildren();
   try{
     showJob(job);
     const current=await api('/video/jobs/'+job);
     if(current.state==='uploading'){
       if(Number(current.upload_progress)<100)throw Error(
         'Import incomplet ('+current.upload_progress+' %). Il faut envoyer les morceaux manquants.'
       );
       show('Upload déjà complet. Reprise du montage : '+job);
       await api('/video/uploads/'+job+'/finish',{method:'POST'});
     }
     // One click resumes a failed existing video when the source was retained;
     // the FFmpeg fallback will still render if OpenAI exceeds its quota.
     const latest=await api('/video/jobs/'+job);
     if(latest.state==='failed' && latest.source_retained_for_retry){
       show('Réparation du montage précédent sans nouvel envoi : '+job);
       await api('/video/jobs/'+job+'/retry',{method:'POST'});
     }
     await monitor(job);
   }catch(e){show('Erreur : '+e.message,true)}
   finally{button.disabled=false}
 });
 send.addEventListener('click',async()=>{
   const file=el('video').files[0],token=el('token').value.trim();
   if(!token){show('Copie POLL_ADMIN_TOKEN depuis Render.',true);return}
   if(!file){show('Choisis la vidéo à monter.',true);return}
   if(!el('rights').checked){show("Coche l'autorisation de réutilisation.",true);return}
   if(file.size>300*1024*1024){show('Fichier supérieur à 300 Mo. Compresse-le avant envoi.',true);return}
   send.disabled=true;result.replaceChildren();progress.hidden=false;progress.value=0;
   try{
     const subtitles=el('subtitles').value;
     const params={
       filename:file.name,size_bytes:file.size,authorized:true,
       clip_seconds:Number(el('seconds').value),music_enabled:el('music').checked,
       hook_text:el('hook').value,vision_enabled:el('vision').checked,voiceover:el('voiceover').checked
     };
     if(subtitles!=='auto')params.auto_subtitles=subtitles==='true';
     const chatFile=el('chat').files[0];
     if(chatFile){
       if(chatFile.size>512*1024)throw Error('Le fichier de chat dépasse 512 Ko.');
       const chat=JSON.parse(await chatFile.text());
       if(!Array.isArray(chat)||chat.length>25000||chat.some(t=>!Number.isFinite(t)||t<0||t>43200)){
         throw Error('Le chat doit être une liste de 25 000 horodatages maximum, exprimés en secondes.');
       }
       params.chat_seconds=chat;
     }
     const started=await api('/video/uploads/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});
     const job=started.job_id,chunkSize=started.chunk_size_bytes;
     showJob(job);
     show('Upload créé : '+job+'. Envoi en cours…');
     let index=0;
     for(let offset=0;offset<file.size;offset+=chunkSize,index++){
       const chunk=file.slice(offset,Math.min(offset+chunkSize,file.size));
       await sendChunk(job,index,chunk);
       progress.value=Math.round(100*Math.min(file.size,offset+chunk.size)/file.size);
       show('Job : '+job+'\nImport : '+progress.value+' %');
     }
     const queued=await api('/video/uploads/'+job+'/finish',{method:'POST'});
     show('Upload terminé. Montage lancé : '+queued.job_id);
     await monitor(job);
   }catch(e){show('Erreur : '+e.message,true)}
   finally{send.disabled=false}
 });
})();
</script></body></html>"""
