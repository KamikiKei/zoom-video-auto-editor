#!/usr/bin/env python3
"""
Zoom動画 自動編集 — みんなが使えるWebインターフェース

起動:
  python3 app.py            # http://localhost:8765
  python3 app.py --port 9000

同じWi-Fi/LANの人は http://<このPCのIP>:8765 で使える。
依存: Python標準ライブラリ + ffmpeg + numpy/scipy (話者分離用)

流れ:
  1. 動画とサムネを選ぶ (動画はブラウザ上でそのままプレビューできる)
  2. 「話者を解析」→ 運営/登壇者を色分け表示。試聴して登壇者を選ぶと
     交代地点が開始タイムに自動で入る
  3. 開始/終了は手動でも調整可 (再生位置をボタンでセット)
  4. 「編集する」→ 完成mp4をダウンロード
"""
import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).parent
JOBS_DIR = BASE / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
JOBS = {}  # job_id -> dict(status, dir, result, diarization, error)
LOCK = threading.Lock()

HTML = r"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zoom動画 かんたん自動編集</title>
<style>
  :root{ --bg:#f6f7f9; --card:#fff; --line:#ddd; --acc:#2255aa; --acc2:#e8873a; }
  *{box-sizing:border-box} body{margin:0;font-family:sans-serif;background:var(--bg);color:#222}
  main{max-width:880px;margin:0 auto;padding:20px 14px 60px}
  h1{font-size:22px;margin:8px 0 4px} .sub{color:#666;font-size:13px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
  .card h2{font-size:16px;margin:0 0 10px} .step{color:var(--acc);font-weight:bold}
  .drop{border:2px dashed #bbb;border-radius:10px;padding:18px;text-align:center;color:#666;cursor:pointer}
  .drop.over{border-color:var(--acc);background:#eef3fb}
  video{width:100%;border-radius:8px;background:#000;margin-top:10px}
  img.thumb{max-width:320px;width:100%;border-radius:8px;margin-top:10px}
  label{font-size:13px;color:#444} input[type=text],input[type=number]{padding:7px 9px;border:1px solid #bbb;border-radius:7px;font-size:15px;width:110px}
  button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;cursor:pointer}
  button.sub{background:#fff;color:var(--acc);border:1px solid var(--acc);padding:7px 12px;font-size:13px}
  button:disabled{opacity:.45;cursor:default}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:8px 0}
  .timeline{position:relative;height:34px;background:#eee;border-radius:7px;overflow:hidden;margin:10px 0;cursor:pointer}
  .seg{position:absolute;top:0;height:100%}
  .seg.sp0{background:#5b8dd9}.seg.sp1{background:var(--acc2)}.seg.sp2{background:#63b06a}.seg.sp3{background:#b06ab0}
  .spcard{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
  .dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px}
  .d0{background:#5b8dd9}.d1{background:var(--acc2)}.d2{background:#63b06a}.d3{background:#b06ab0}
  progress{width:100%;height:14px}
  .msg{font-size:13px;color:#555;white-space:pre-wrap}
  .ok{color:#1d7a35;font-weight:bold} .err{color:#c0392b}
  .hidden{display:none}
</style></head><body><main>
<h1>Zoom動画 かんたん自動編集</h1>
<div class="sub">OP/EDカット ＋ サムネを頭とお尻に ＋ ディゾルブ。全部自動でやります。</div>

<div class="card"><h2><span class="step">1.</span> 動画とサムネを選ぶ</h2>
  <div class="drop" id="videoDrop">動画 (mp4) をここにドロップ / クリックで選択<input type="file" id="videoFile" accept="video/*" class="hidden"></div>
  <div class="msg" id="videoName"></div>
  <video id="player" controls class="hidden"></video>
  <div style="height:10px"></div>
  <div class="drop" id="thumbDrop">OP/ED素材（画像 or 動画）をドロップ / クリックで選択<input type="file" id="thumbFile" accept="image/*,video/*" class="hidden"></div>
  <div class="row">
    <label><input type="radio" name="assetType" value="image" checked> 画像（表示秒数を決める）</label>
    <label><input type="radio" name="assetType" value="video"> 動画（そのままつなぐ）</label>
  </div>
  <img id="thumbPreview" class="thumb hidden">
  <video id="thumbVideoPreview" class="thumb hidden" controls muted playsinline></video>
</div>

<div class="card"><h2><span class="step">2.</span> 登壇者に代わる所を探す</h2>
  <div class="msg">まず動画をアップロードして話者を解析します。運営と登壇者を色で分けます。</div>
  <div class="drop" id="transcriptDrop">Zoom文字起こし (.vtt) があればここにドロップ（話者名つきで正確に分けられます）<input type="file" id="transcriptFile" accept=".vtt,.txt" class="hidden"></div>
  <div class="msg" id="transcriptName"></div>
  <div class="row"><button id="analyzeBtn" disabled>話者を解析する</button><span class="msg" id="analyzeMsg"></span></div>
  <progress id="upProg" class="hidden" max="100" value="0"></progress>
  <div class="timeline hidden" id="timeline"></div>
  <div id="speakerCards"></div>
  <div class="row">
    <label>開始 <input type="text" id="startInput" placeholder="0:00"></label>
    <button class="sub" id="setStartBtn">▶ 今の位置を開始に</button>
    <label>終了 <input type="text" id="endInput" placeholder="最後まで"></label>
    <button class="sub" id="setEndBtn">▶ 今の位置を終了に</button>
  </div>
  <div class="msg" id="durInfo"></div>
</div>

<div class="card"><h2><span class="step">3.</span> 仕上げの設定</h2>
  <div class="row">
    <label>頭サムネ <input type="number" id="introSec" value="3" step="0.5" min="0.5" style="width:70px"> 秒</label>
    <label>お尻サムネ <input type="number" id="outroSec" value="3" step="0.5" min="0.5" style="width:70px"> 秒</label>
    <label>ディゾルブ <input type="number" id="dissolve" value="0.8" step="0.1" min="0" style="width:70px"> 秒</label>
    <span class="msg" id="assetNote"></span>
  </div>
  <div class="row">
    <label><input type="checkbox" id="silenceCut" checked> 無音をカットする</label>
    <label><input type="number" id="silenceSec" value="3" step="0.5" min="1" style="width:60px"> 秒以上の無音を除去</label>
  </div>
  <div id="cutWrap" class="hidden">
    <div class="msg">カット候補（無音区間）。チェックが入った区間を切ります。外した区間は残します。時刻をクリックするとその位置を再生します。</div>
    <div id="cutList"></div>
    <div class="row"><button class="sub" id="refetchCutsBtn">カット候補を再検出</button><span class="msg" id="cutMsg"></span></div>
  </div>
</div>

<div class="card"><h2><span class="step">4.</span> 編集してダウンロード</h2>
  <div class="row"><button id="runBtn" disabled>編集する</button><span class="msg" id="runMsg"></span></div>
  <progress id="procProg" class="hidden" max="100" value="0"></progress>
  <video id="resultPlayer" controls class="hidden"></video>
  <div class="row"><a id="dlLink" class="hidden" download><button>完成mp4をダウンロード</button></a></div>
</div>
</main>
<script>
const $ = id => document.getElementById(id);
let videoFile=null, thumbFile=null, transcriptFile=null, jobId=null, uploaded=false, diar=null, videoDur=0;

function fmt(s){ if(s==null||isNaN(s)) return ''; s=Math.max(0,Math.round(s)); const m=Math.floor(s/60), h=Math.floor(m/60); return h? `${h}:${String(m%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}` : `${m}:${String(s%60).padStart(2,'0')}`; }
function parseT(v){ v=(v||'').trim(); if(!v) return null; if(/^\d+(\.\d+)?$/.test(v)) return parseFloat(v); const p=v.split(':').map(Number); if(p.some(isNaN)) return null; return p.reduce((a,b)=>a*60+b,0); }

function bindDrop(dropId, inputId, cb){
  const d=$(dropId), i=$(inputId);
  d.onclick=()=>i.click();
  i.onchange=()=>{ if(i.files[0]) cb(i.files[0]); };
  d.ondragover=e=>{e.preventDefault();d.classList.add('over')};
  d.ondragleave=()=>d.classList.remove('over');
  d.ondrop=e=>{e.preventDefault();d.classList.remove('over'); if(e.dataTransfer.files[0]) cb(e.dataTransfer.files[0]);};
}
bindDrop('videoDrop','videoFile', f=>{
  videoFile=f; uploaded=false; jobId=null; diar=null;
  $('videoName').textContent = f.name+' ('+(f.size/1048576).toFixed(0)+' MB)';
  const p=$('player'); p.src=URL.createObjectURL(f); p.classList.remove('hidden');
  p.onloadedmetadata=()=>{ videoDur=p.duration; $('endInput').value=fmt(videoDur); updateDur(); };
  $('analyzeBtn').disabled=false; $('runBtn').disabled = !thumbFile;
});
function syncAssetType(){
  const t=document.querySelector('input[name=assetType]:checked').value;
  const isVideo = t==='video';
  $('introSec').disabled=isVideo; $('outroSec').disabled=isVideo;
  $('assetNote').textContent = isVideo ? '動画素材はそのままの長さで頭とお尻につなぎます' : '';
}
document.querySelectorAll('input[name=assetType]').forEach(r=> r.onchange=syncAssetType);
bindDrop('thumbDrop','thumbFile', f=>{
  thumbFile=f;
  const isVideo = (f.type||'').startsWith('video');
  document.querySelector(`input[name=assetType][value="${isVideo?'video':'image'}"]`).checked=true;
  syncAssetType();
  const im=$('thumbPreview'), vm=$('thumbVideoPreview');
  if(isVideo){
    im.classList.add('hidden');
    vm.src=URL.createObjectURL(f); vm.classList.remove('hidden');
    vm.onloadedmetadata=()=>{ if(document.querySelector('input[name=assetType]:checked').value==='video')
      $('assetNote').textContent=`動画素材はそのままの長さ（${vm.duration.toFixed(1)}秒）で頭とお尻につなぎます`; };
  } else {
    vm.classList.add('hidden'); vm.removeAttribute('src');
    im.src=URL.createObjectURL(f); im.classList.remove('hidden');
  }
  $('runBtn').disabled = !videoFile;
});
bindDrop('transcriptDrop','transcriptFile', f=>{
  transcriptFile=f; $('transcriptName').textContent = '文字起こし: '+f.name+'（解析に使います）';
});
function updateDur(){
  const s=parseT($('startInput').value), e=parseT($('endInput').value);
  if(s!=null && e!=null && e>s) $('durInfo').textContent = `本編は ${fmt(e-s)} になります`;
}
$('startInput').oninput=updateDur; $('endInput').oninput=updateDur;
$('setStartBtn').onclick=()=>{ $('startInput').value=fmt($('player').currentTime); updateDur(); };
$('setEndBtn').onclick=()=>{ $('endInput').value=fmt($('player').currentTime); updateDur(); };

function xhrUpload(url, file, progEl){
  return new Promise((res,rej)=>{
    const x=new XMLHttpRequest(); x.open('PUT', url);
    x.upload.onprogress=e=>{ if(e.lengthComputable&&progEl){ progEl.classList.remove('hidden'); progEl.value=e.loaded/e.total*100; } };
    x.onload=()=> x.status<300 ? res() : rej(new Error('upload '+x.status));
    x.onerror=()=>rej(new Error('upload failed'));
    x.send(file);
  });
}
async function pollStatus(){
  // 一時的な接続失敗では落とさず、辛抱強く再試行する
  let fails=0;
  while(true){
    try{
      const st=await (await fetch(`/api/status/${jobId}`)).json();
      return {st, fails};
    }catch(e){
      fails++;
      if(fails>=40) throw new Error('サーバーに接続できません。黒い画面（サーバー）が閉じていないか確認してください');
      await new Promise(r=>setTimeout(r,2000));
    }
  }
}
async function ensureJob(){
  if(jobId) return jobId;
  const r=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  jobId=(await r.json()).job_id; return jobId;
}
async function ensureUploaded(){
  await ensureJob();
  if(!uploaded){ await xhrUpload(`/api/upload/${jobId}/video`, videoFile, $('upProg')); uploaded=true; }
}
$('analyzeBtn').onclick=async()=>{
  const btn=$('analyzeBtn'); btn.disabled=true; $('analyzeMsg').textContent='アップロード中…';
  try{
    await ensureUploaded();
    if(transcriptFile) await xhrUpload(`/api/upload/${jobId}/transcript`, transcriptFile, null);
    $('analyzeMsg').textContent= transcriptFile ? '文字起こしを読み込み中…' : '解析中… (長い動画だと数分かかります)';
    await fetch(`/api/diarize/${jobId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speakers:2})});
    while(true){
      await new Promise(r=>setTimeout(r,2000));
      const {st}=await pollStatus();
      if(st.stage==='diarized'){ diar=st.diarization; break; }
      if(st.stage==='error'){ throw new Error(st.error||'解析に失敗'); }
    }
    $('analyzeMsg').innerHTML= diar.backend==='zoom-transcript'
      ? '<span class="ok">解析できました</span> Zoom文字起こし（話者名つき）で分けました。登壇者を確認してください。'
      : '<span class="ok">解析できました</span> 話者ごとの声を聞いて、登壇者を選んでください。';
    renderDiar();
    loadSilences();
  }catch(e){ $('analyzeMsg').innerHTML='<span class="err">'+e.message+'</span>'; }
  btn.disabled=false;
};
function selectedPresenters(){
  return [...document.querySelectorAll('input[name=presenters]:checked')].map(el=>Number(el.value));
}
function updateStartFromPresenters(){
  const ps=selectedPresenters();
  if(!diar||!ps.length) return;
  const segs=diar.segments.filter(sg=>ps.includes(sg.speaker));
  if(!segs.length) return;
  const seg=segs.find(sg=>(sg.end-sg.start)>=5)||segs[0];
  $('startInput').value=fmt(seg.start); updateDur();
  $('analyzeMsg').innerHTML=`<span class="ok">開始を ${fmt(seg.start)} にセットしました</span>（選んだ登壇者の最初の長い発言の頭）。プレビューで確認して、違うと思ったら手で直してください。`;
}
function renderDiar(presenters){
  const tl=$('timeline'); tl.innerHTML=''; tl.classList.remove('hidden');
  diar.segments.forEach(sg=>{
    const d=document.createElement('div'); d.className='seg sp'+sg.speaker;
    d.style.left=(sg.start/videoDur*100)+'%'; d.style.width=((sg.end-sg.start)/videoDur*100)+'%';
    d.title=`${(diar.names&&diar.names[sg.speaker])||('話者'+(sg.speaker+1))} ${fmt(sg.start)}〜${fmt(sg.end)}`;
    d.onclick=()=>{ $('player').currentTime=sg.start; $('player').play(); };
    tl.appendChild(d);
  });
  tl.onclick=e=>{ const r=tl.getBoundingClientRect(); $('player').currentTime=(e.clientX-r.left)/r.width*videoDur; };
  const wrap=$('speakerCards'); wrap.innerHTML='';
  const defaults = presenters!=null ? presenters.map(Number)
    : [diar.presenter_guess!=null ? diar.presenter_guess : diar.main_speaker_guess];
  Object.keys(diar.speakers).forEach(sp=>{
    const nm=(diar.names&&diar.names[sp])||`話者${Number(sp)+1}`;
    const isPresenter = diar.presenter_guess!=null && String(diar.presenter_guess)===String(sp);
    const note = isPresenter ? ' <span class="msg">(紹介の直後なので登壇者っぽい)</span>'
      : (String(diar.main_speaker_guess)===String(sp)?' <span class="msg">(いちばん長く話してるので登壇者っぽい)</span>':'');
    const card=document.createElement('div'); card.className='spcard';
    card.innerHTML=`<span class="dot d${sp}"></span><b>${nm}</b> (発話 合計 ${fmt(diar.speakers[sp])}) `
      +`<audio controls src="/api/sample/${jobId}/${sp}" style="vertical-align:middle;height:32px"></audio> `
      +`<label><input type="checkbox" name="presenters" value="${sp}"${defaults.includes(Number(sp))?' checked':''}> 登壇者として扱う（複数可）</label>` + note;
    wrap.appendChild(card);
  });
  wrap.querySelectorAll('input[name=presenters]').forEach(cb=>{ cb.onchange=updateStartFromPresenters; });
  updateStartFromPresenters();
}
let cutCandidates=[];
function renderCuts(){
  const wrap=$('cutList'); wrap.innerHTML='';
  $('cutWrap').classList.remove('hidden');
  if(!cutCandidates.length){ $('cutMsg').textContent='カット候補はありません'; return; }
  $('cutMsg').textContent='';
  cutCandidates.forEach((c,i)=>{
    const row=document.createElement('div'); row.className='row';
    row.innerHTML=`<label><input type="checkbox" class="cutchk" data-i="${i}"${c.enabled?' checked':''}> 切る</label>`
      +`<span class="msg" style="cursor:pointer;text-decoration:underline">${fmt(c.start)} 〜 ${fmt(c.end)}（無音 ${(c.end-c.start).toFixed(1)}秒）</span>`;
    row.querySelector('span').onclick=()=>{ $('player').currentTime=c.start; $('player').play(); };
    wrap.appendChild(row);
  });
  wrap.querySelectorAll('.cutchk').forEach(chk=>{
    chk.onchange=()=>{ cutCandidates[Number(chk.dataset.i)].enabled=chk.checked; };
  });
}
async function loadSilences(){
  if(!jobId) return;
  $('cutWrap').classList.remove('hidden');
  $('cutMsg').textContent='カット候補を検出中…';
  try{
    await fetch(`/api/silences/${jobId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({silence_sec:parseFloat($('silenceSec').value)||3})});
    while(true){
      await new Promise(r=>setTimeout(r,1500));
      const {st}=await pollStatus();
      if(st.silence_stage==='done'){ cutCandidates=st.silences||[]; break; }
      if(st.silence_stage==='error'){ throw new Error(st.error||'検出に失敗'); }
    }
    renderCuts();
  }catch(e){ $('cutMsg').textContent=e.message; }
}
$('refetchCutsBtn').onclick=()=>loadSilences();
$('runBtn').onclick=async()=>{
  const btn=$('runBtn'); btn.disabled=true; $('runMsg').textContent='準備中…';
  try{
    await ensureUploaded();
    if(thumbFile) await xhrUpload(`/api/upload/${jobId}/thumb`, thumbFile, $('upProg'));
    const s=parseT($('startInput').value), e=parseT($('endInput').value);
    if(s==null||e==null||e<=s) throw new Error('開始・終了タイムを確認してください');
    await fetch(`/api/process/${jobId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      trim_start:s, end:e, intro_sec:parseFloat($('introSec').value), outro_sec:parseFloat($('outroSec').value), dissolve:parseFloat($('dissolve').value),
      asset_type:document.querySelector('input[name=assetType]:checked').value,
      silence_cut:$('silenceCut').checked, silence_sec:parseFloat($('silenceSec').value)||3,
      presenters:selectedPresenters(),
      disabled_cuts:cutCandidates.filter(c=>!c.enabled).map(c=>[c.start,c.end])})});
    const bar=$('procProg'); bar.classList.remove('hidden'); bar.value=0;
    $('runMsg').textContent='編集中… 0%（この間、黒い画面は閉じないでください）';
    while(true){
      await new Promise(r=>setTimeout(r,2000));
      const {st}=await pollStatus();
      if(typeof st.progress==='number'){ bar.value=st.progress; $('runMsg').textContent=`編集中… ${st.progress}%（この間、黒い画面は閉じないでください）`; }
      if(st.stage==='done'){ bar.value=100; break; }
      if(st.stage==='error'){ throw new Error(st.error||'編集に失敗'); }
    }
    $('runMsg').innerHTML='<span class="ok">完成しました</span>';
    const rp=$('resultPlayer'); rp.src=`/api/download/${jobId}`; rp.classList.remove('hidden');
    const dl=$('dlLink'); dl.href=`/api/download/${jobId}`; dl.classList.remove('hidden');
  }catch(e){ $('runMsg').innerHTML='<span class="err">'+e.message+'</span>'; }
  btn.disabled=false;
};

// パイプライン連携: ?job=<id> で開かれたら、サーバーに取り込み済みの
// ジョブ (動画・文字起こし・カット案) を読み込んで微修正できる状態にする
(async function initFromQuery(){
  const jid=new URLSearchParams(location.search).get('job');
  if(!jid) return;
  try{
    const info=await (await fetch(`/api/jobinfo/${jid}`)).json();
    if(info.error||!info.has_video) return;
    jobId=jid; uploaded=true;
    const title=(info.meta&&info.meta.title)||'';
    $('videoName').textContent='パイプラインから取り込み済みの動画です'+(title?`（${title}）`:'')+'。内容を確認して、必要なら微修正して「編集する」を押してください。';
    const p=$('player'); p.src=`/api/video/${jid}`; p.classList.remove('hidden');
    p.onloadedmetadata=()=>{ videoDur=p.duration; if(info.plan) videoDur=info.plan.video.duration||p.duration; updateDur(); };
    if(info.has_thumb){
      const at=(info.plan&&info.plan.asset&&info.plan.asset.type)||'image';
      document.querySelector(`input[name=assetType][value="${at}"]`).checked=true;
      syncAssetType();
      if(at==='video'){ const vm=$('thumbVideoPreview'); vm.src=`/api/thumb/${jid}`; vm.classList.remove('hidden'); }
      else { const im=$('thumbPreview'); im.src=`/api/thumb/${jid}`; im.classList.remove('hidden'); }
    }
    if(info.diarization){ diar=info.diarization; videoDur=(info.plan&&info.plan.video.duration)||0; renderDiar(info.plan && info.plan.presenters); }
    if(info.plan){
      videoDur=info.plan.video.duration||videoDur;
      $('startInput').value=fmt(info.plan.trim_start);
      $('endInput').value=fmt(info.plan.end);
      const r=info.plan.render||{};
      if(r.intro_sec!=null) $('introSec').value=r.intro_sec;
      if(r.outro_sec!=null) $('outroSec').value=r.outro_sec;
      if(r.dissolve!=null) $('dissolve').value=r.dissolve;
      const sil=info.plan.silence||{};
      $('silenceCut').checked=!!sil.enabled;
      if(sil.min_sec!=null) $('silenceSec').value=sil.min_sec;
      updateDur();
      $('analyzeMsg').innerHTML='<span class="ok">カット案を読み込みました</span> 開始点・無音カットは自動判定済みです。違うと思ったら直してください。';
      if(sil.candidates){ cutCandidates=sil.candidates; renderCuts(); }
    }
    $('analyzeBtn').disabled=false;
    $('runBtn').disabled=false;
    if(info.has_output){
      $('runMsg').innerHTML='<span class="ok">書き出し済みです</span>（直す場合はもう一度「編集する」を押してください）';
      const rp=$('resultPlayer'); rp.src=`/api/download/${jid}`; rp.classList.remove('hidden');
      const dl=$('dlLink'); dl.href=`/api/download/${jid}`; dl.classList.remove('hidden');
    }
  }catch(e){ /* 通常フローにフォールバック */ }
})();
</script></body></html>"""


def job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def disk_job(job_id: str):
    """メモリ上に無いジョブ (パイプラインが事前投入したジョブ等) を
    ディスクの状態から復元する。無ければ None。"""
    d = JOBS_DIR / job_id
    if not d.is_dir():
        return None
    job = {"stage": "created"}
    diar_file = d / "diarization.json"
    if diar_file.exists():
        try:
            job["diarization"] = json.loads(diar_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    if (d / "input.mp4").exists():
        job["stage"] = "uploaded"
    if "diarization" in job:
        job["stage"] = "diarized"
    if (d / "output.mp4").exists():
        job["stage"] = "done"
    return job


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "api":
            job_id = parts[2] if parts[1] in ("status", "download") else (parts[2] if len(parts) > 2 else None)
        if path.startswith("/api/status/"):
            job_id = path.split("/")[3]
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                job = disk_job(job_id)
            if not job:
                return self._json({"error": "job not found"}, 404)
            return self._json({k: job.get(k) for k in ("stage", "error", "diarization", "progress", "silences", "silence_stage")})
        if path.startswith("/api/jobinfo/"):
            job_id = path.split("/")[3]
            d = JOBS_DIR / job_id
            if not d.is_dir():
                return self._json({"error": "job not found"}, 404)
            info = {"job_id": job_id,
                    "has_video": (d / "input.mp4").exists(),
                    "has_transcript": (d / "transcript.vtt").exists(),
                    "has_thumb": (d / "thumb_asset").exists(),
                    "has_output": (d / "output.mp4").exists()}
            for key, fname in (("plan", "plan.json"), ("meta", "jobmeta.json")):
                f = d / fname
                if f.exists():
                    try:
                        info[key] = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        info[key] = None
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                job = disk_job(job_id)
            if job:
                info["stage"] = job.get("stage")
                if job.get("diarization"):
                    info["diarization"] = job["diarization"]
            return self._json(info)
        if path.startswith("/api/video/"):
            job_id = path.split("/")[3]
            f = job_dir(job_id) / "input.mp4"
            if not f.exists():
                return self._json({"error": "video not found"}, 404)
            return self._file(f, "video/mp4")
        if path.startswith("/api/thumb/"):
            job_id = path.split("/")[3]
            d = job_dir(job_id)
            f = d / "thumb_asset"
            if not f.exists():
                return self._json({"error": "thumb not found"}, 404)
            import mimetypes
            ctype = "application/octet-stream"
            meta_f = d / "jobmeta.json"
            if meta_f.exists():
                try:
                    name = json.loads(meta_f.read_text(encoding="utf-8")).get("asset_name") or ""
                    ctype = mimetypes.guess_type(name)[0] or ctype
                except Exception:  # noqa: BLE001
                    pass
            return self._file(f, ctype)
        if path.startswith("/api/sample/"):
            _, _, _, job_id, sp = path.split("/")
            f = job_dir(job_id) / "samples" / f"speaker{sp}.mp3"
            if not f.exists():
                return self._json({"error": "sample not found"}, 404)
            return self._file(f, "audio/mpeg")
        if path.startswith("/api/download/"):
            job_id = path.split("/")[3]
            f = job_dir(job_id) / "output.mp4"
            if not f.exists():
                return self._json({"error": "not ready"}, 404)
            return self._file(f, "video/mp4", download=True)
        return self._json({"error": "not found"}, 404)

    def _file(self, f: Path, ctype: str, download=False):
        size = f.stat().st_size
        start, end = 0, size - 1
        status = 200
        range_hdr = self.headers.get("Range")
        if range_hdr and range_hdr.startswith("bytes="):
            try:
                first, _, last = range_hdr[6:].partition("-")
                if first:
                    start = int(first)
                if last:
                    end = min(int(last), size - 1)
                if start > end:
                    raise ValueError
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="edited.mp4"')
        self.end_headers()
        with f.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_PUT(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        # /api/upload/<job_id>/<kind>
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "upload":
            job_id, kind = parts[2], parts[3]
            dest_name = {"video": "input.mp4", "transcript": "transcript.vtt",
                         "thumb": "thumb_asset"}.get(kind, "thumb_asset")
            dest = job_dir(job_id) / dest_name
            n = int(self.headers.get("Content-Length") or 0)
            left = n
            with dest.open("wb") as fh:
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    fh.write(chunk)
                    left -= len(chunk)
            with LOCK:
                JOBS.setdefault(job_id, {"stage": "uploaded"})
            return self._json({"ok": True, "bytes": n})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/start":
            self._body_json()
            job_id = uuid.uuid4().hex[:12]
            with LOCK:
                JOBS[job_id] = {"stage": "created"}
            job_dir(job_id)
            return self._json({"job_id": job_id})
        if path.startswith("/api/diarize/"):
            job_id = path.split("/")[3]
            params = self._body_json()
            self._run_async(job_id, "diarizing", lambda: self._do_diarize(job_id, params))
            return self._json({"ok": True})
        if path.startswith("/api/silences/"):
            job_id = path.split("/")[3]
            params = self._body_json()
            self._run_silences(job_id, params)
            return self._json({"ok": True})
        if path.startswith("/api/process/"):
            job_id = path.split("/")[3]
            params = self._body_json()
            self._run_async(job_id, "processing", lambda: self._do_process(job_id, params))
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _run_async(self, job_id, stage, fn):
        def wrap():
            with LOCK:
                JOBS.setdefault(job_id, {})["stage"] = stage
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                with LOCK:
                    JOBS[job_id]["stage"] = "error"
                    JOBS[job_id]["error"] = str(e)[:500]
        threading.Thread(target=wrap, daemon=True).start()

    def _run_silences(self, job_id, params):
        """無音カット候補の検出を非同期で走らせる (stage は触らない)。"""
        def wrap():
            with LOCK:
                JOBS.setdefault(job_id, {})["silence_stage"] = "running"
            try:
                import sys
                sys.path.insert(0, str(BASE))
                from edit import detect_silences
                d = job_dir(job_id)
                min_sec = float(params.get("silence_sec", 3.0))
                db = int(params.get("silence_db", -38))
                found = detect_silences(d / "input.mp4", min_sec, db)
                cands = [{"start": round(s, 3), "end": round(e, 3),
                          "kind": "silence", "enabled": True}
                         for s, e in found if e - s >= min_sec - 0.05]
                with LOCK:
                    JOBS[job_id]["silences"] = cands
                    JOBS[job_id]["silence_stage"] = "done"
            except Exception as e:  # noqa: BLE001
                with LOCK:
                    JOBS[job_id]["silence_stage"] = "error"
                    JOBS[job_id]["error"] = str(e)[:500]
        threading.Thread(target=wrap, daemon=True).start()

    def _do_diarize(self, job_id, params):
        import sys
        sys.path.insert(0, str(BASE))
        from diarize import diarize, export_samples
        d = job_dir(job_id)
        result = None
        tpath = d / "transcript.vtt"
        if tpath.exists() and tpath.stat().st_size > 0:
            try:
                from transcript import result_from_transcript
                result = result_from_transcript(tpath)
            except Exception:  # noqa: BLE001
                result = None  # 文字起こしが使えなければ音声解析にフォールバック
        if result is None:
            result = diarize(d / "input.mp4", n_speakers=int(params.get("speakers", 2)))
        result["samples"] = export_samples(d / "input.mp4", result, d / "samples")
        with LOCK:
            JOBS[job_id]["diarization"] = result
            JOBS[job_id]["stage"] = "diarized"

    def _do_process(self, job_id, params):
        import sys
        sys.path.insert(0, str(BASE))
        from edit import build_and_run
        d = job_dir(job_id)
        cfg = {
            "trim_start": float(params.get("trim_start", 0)),
            "end": float(params["end"]),
            "intro_sec": float(params.get("intro_sec", 3)),
            "outro_sec": float(params.get("outro_sec", 3)),
            "dissolve": float(params.get("dissolve", 0.8)),
            "asset_type": params.get("asset_type", "image"),
            "silence_cut": bool(params.get("silence_cut", False)),
            "silence_sec": float(params.get("silence_sec", 3.0)),
        }
        if params.get("presenters") is not None:
            cfg["presenters"] = [int(p) for p in params["presenters"]]
        if params.get("disabled_cuts") is not None:
            cfg["disabled_cuts"] = [[float(a), float(b)]
                                    for a, b in params["disabled_cuts"]]
        def cb(pct):
            with LOCK:
                JOBS[job_id]["progress"] = pct
        with LOCK:
            JOBS[job_id]["progress"] = 0
        build_and_run(d / "input.mp4", d / "thumb_asset", d / "output.mp4", cfg, progress_cb=cb)
        with LOCK:
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["stage"] = "done"


def main():
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"起動しました: http://localhost:{args.port}  (LANからはこのPCのIP:{args.port})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
