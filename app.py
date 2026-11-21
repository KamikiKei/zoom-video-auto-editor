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
  #tlScroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:#fff;margin-top:6px}
  #tlCanvas{position:relative;height:66px;min-width:100%;cursor:pointer}
  .tl-ruler{position:absolute;top:0;left:0;right:0;height:16px;border-bottom:1px solid #e3e6ea;overflow:hidden}
  .tl-tick{position:absolute;top:1px;height:14px;font-size:10px;color:#888;border-left:1px solid #ccc;padding-left:3px;white-space:nowrap}
  .tl-lane{position:absolute;left:0;right:0;top:20px;height:38px;background:#dfe3e8}
  .tl-blk{position:absolute;top:0;height:100%;font-size:9px;color:#444;line-height:1.2;padding:2px 0 0 3px;overflow:hidden;white-space:nowrap}
  .tl-seg{background:#c2cbd4}
  .tl-seg.sp0{background:#5b8dd9;color:#fff}.tl-seg.sp1{background:var(--acc2);color:#fff}.tl-seg.sp2{background:#63b06a;color:#fff}.tl-seg.sp3{background:#b06ab0;color:#fff}
  .tl-seg.sp-off{opacity:.38}
  .tl-seg.is-cut{background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.38) 0 6px,rgba(0,0,0,.10) 6px 12px);filter:brightness(.75)}
  .tl-seg.is-cand{box-shadow:inset 0 0 0 2px #e6a817}
  .tl-seg.is-user{box-shadow:inset 0 -3px 0 rgba(20,40,80,.55)}
  .tl-seg.is-cand.is-user{box-shadow:inset 0 0 0 2px #e6a817,inset 0 -3px 0 rgba(20,40,80,.55)}
  /* 候補の出所は上端の帯で区別する (title属性に理由も出る) */
  .tl-seg.is-llm{box-shadow:inset 0 3px 0 #8e44ad}
  .tl-seg.is-ops{box-shadow:inset 0 3px 0 #14406e}
  .tl-seg.is-other{box-shadow:inset 0 3px 0 #5a5a5a}
  .tl-seg.is-cand.is-llm{box-shadow:inset 0 0 0 2px #e6a817,inset 0 3px 0 #8e44ad}
  .tl-seg.is-cand.is-ops{box-shadow:inset 0 0 0 2px #e6a817,inset 0 3px 0 #14406e}
  .tl-seg.is-cand.is-other{box-shadow:inset 0 0 0 2px #e6a817,inset 0 3px 0 #5a5a5a}
  .tl-seg{cursor:pointer}
  .tl-out{background:rgba(0,0,0,.28)}
  #tlPlayhead{position:absolute;top:0;bottom:0;width:2px;background:#e02424;z-index:5;pointer-events:none}
  #tlStartMarker{position:absolute;top:0;bottom:0;width:12px;margin-left:-6px;z-index:6;cursor:ew-resize}
  #tlStartMarker::before{content:'';position:absolute;top:0;bottom:0;left:5px;width:2px;background:#1d7a35}
  #tlStartMarker::after{content:'開始';position:absolute;top:0;left:8px;background:#1d7a35;color:#fff;font-size:10px;padding:1px 4px;border-radius:3px;white-space:nowrap}
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
  <div id="tlWrap" class="hidden">
    <div class="row" style="justify-content:space-between;margin:10px 0 0">
      <span class="msg"><b>タイムライン</b>　<span id="tlCurTime"></span></span>
      <span>
        <button class="sub" id="tlZoomOutBtn">− 縮小</button>
        <input type="range" id="tlZoomSlider" min="0" max="100" value="0" style="width:120px;vertical-align:middle">
        <button class="sub" id="tlZoomInBtn">＋ 拡大</button>
        <button class="sub" id="tlFitBtn">全体表示</button>
      </span>
    </div>
    <div id="tlScroll"><div id="tlCanvas">
      <div class="tl-ruler" id="tlRuler"></div>
      <div class="tl-lane" id="tlLane"></div>
      <div id="tlPlayhead"></div>
      <div id="tlStartMarker"></div>
    </div></div>
    <div class="msg" style="margin-top:6px">タイムライン（1本）：色＝話者（灰色＝話者なし）、斜線＋暗い区間＝カット、黄色枠＝カット候補だけど残す。候補の出所は上端の帯の色で区別します（紫＝AI検出、濃青＝運営の発話、灰＝登壇者以外の発話）。区間にマウスを乗せると候補の理由が表示されます。区間をタップ（クリック）すると 消す⇔残す が切り替わり、その位置を再生します。残す区間もタップでカットにできます（もう一度タップで戻ります）。緑＝開始点（ドラッグで移動）。ボタン／スライダー／Ctrl+ホイールで拡大縮小。</div>
  </div>
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
    <div class="msg">カット候補は上のタイムラインに表示されます。斜線の区間が「切る」、黄色枠は「候補だけど残す」。候補には無音のほか、文字起こしがある場合は「運営など登壇者以外の発話」と「AI検出（もたつき・トラブル）」も最初から入ります（デフォルトで残すのは登壇者の発話のみ）。区間をタップ（クリック）すると 消す⇔残す が切り替わり、その位置を再生して確認できます（画面共有中の音声など、消したくない区間はタップして残してください）。残す区間もタップすればカットにできます。</div>
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
  $('startInput').value=fmt(seg.start); updateDur(); renderTimeline();
  $('analyzeMsg').innerHTML=`<span class="ok">開始を ${fmt(seg.start)} にセットしました</span>（選んだ登壇者の最初の長い発言の頭）。プレビューで確認して、違うと思ったら手で直してください。`;
}
function renderDiar(presenters){
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
  wrap.querySelectorAll('input[name=presenters]').forEach(cb=>{ cb.onchange=()=>{
    updateStartFromPresenters();
    // 登壇者を変えたら、保持ポリシーの候補 (登壇者以外の発話) も作り直す
    if(diar&&diar.backend==='zoom-transcript') loadSilences();
  }; });
  updateStartFromPresenters();
  renderTimeline();
}
let cutCandidates=[];
let llmDetectInfo=null; // AI検出のステータス {status: ok/skipped/failed, count, detail}
const SRC_LABEL={silence:'無音', ops:'運営の発話', other:'登壇者以外の発話', llm:'AI検出', user:'手動カット'};
function renderCuts(){
  $('cutWrap').classList.remove('hidden');
  let llmNote='';
  if(llmDetectInfo){
    if(llmDetectInfo.status==='ok') llmNote=`AI検出: ${llmDetectInfo.count||0}件`;
    else if(llmDetectInfo.status==='failed') llmNote=`AI検出: 失敗（${llmDetectInfo.detail||'不明なエラー'}）`;
    else llmNote=`AI検出: スキップ（${llmDetectInfo.detail||'キー未設定'}）`;
  }
  if(!cutCandidates.length){ $('cutMsg').textContent='カット候補はありません（タイムラインの区間をタップするとカットにできます）'+(llmNote?` — ${llmNote}`:''); }
  else{
    const on=cutCandidates.filter(c=>c.enabled).length;
    const parts=Object.keys(SRC_LABEL)
      .map(s=>[SRC_LABEL[s], cutCandidates.filter(c=>c.source===s).length])
      .filter(([,n])=>n>0).map(([l,n])=>`${l} ${n}件`).join(' / ');
    $('cutMsg').textContent=`カット候補 ${cutCandidates.length}件（うち ${on}件を切る）内訳: ${parts} — タイムラインの区間タップで切り替えてください`+(llmNote?` / ${llmNote}`:'');
  }
  renderTimeline();
}
async function loadSilences(){
  if(!jobId) return;
  $('cutWrap').classList.remove('hidden');
  $('cutMsg').textContent='カット候補を検出中…';
  try{
    await fetch(`/api/silences/${jobId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({silence_sec:parseFloat($('silenceSec').value)||3, presenters:selectedPresenters()})});
    while(true){
      await new Promise(r=>setTimeout(r,1500));
      const {st}=await pollStatus();
      if(st.silence_stage==='done'){
        llmDetectInfo=st.llm_detection||null;
        // 再検出しても手動カットは消さない。AI検出候補は、再検出で返って
        // きたものと範囲が重なる分は新しい方を優先し (重複防止)、返って
        // こなかった分 (キー未設定で検出し直した場合など) は残す。
        // 「残す」にした候補の無効化状態も、範囲が一致すれば引き継ぐ
        const prevOff=cutCandidates.filter(c=>!c.enabled&&c.source!=='user');
        const fetched=(st.silences||[]).map(c=>{
          const n={...c, source:c.source||'silence'};
          if(prevOff.some(p=>Math.abs(p.start-n.start)<0.6&&Math.abs(p.end-n.end)<0.6)) n.enabled=false;
          return n;
        });
        const keep=cutCandidates.filter(c=>c.source==='user'
          ||(c.source==='llm'&&!fetched.some(n=>n.source==='llm'&&Math.abs(n.start-c.start)<0.6&&Math.abs(n.end-c.end)<0.6)));
        cutCandidates=fetched.concat(keep);
        break;
      }
      if(st.silence_stage==='error'){ throw new Error(st.error||'検出に失敗'); }
    }
    renderCuts();
  }catch(e){ $('cutMsg').textContent=e.message; }
}
// ===== タイムライン (1レーン統合: 色=話者 / 斜線+減光=カット) =====
// [開始, 終了] の全区間を話者区間とカット候補の境界で欠けなく分割し、
// 各区間に「話者は誰か (塗り色)」と「残す/切る (斜線・減光)」を同時に出す。
// 操作はタップ (クリック) 切替のみ: ブロックをタップすると 消す⇔残す が
// 切り替わる。候補の無い保持ブロックをタップすると、そのブロック範囲が
// 手動カット (source: "user") として追加され、再タップで解除される。
// 「編集する」時、無効化した無音候補は disabled_cuts に、有効な手動カット
// は user_cuts として process に渡る。
let tlPx=0; // 0 = 全体表示 (fit)。>0 は px/秒
const TL_MAXPX=120; // 最大拡大 (1秒=120px まで寄れる)
let tlDragMovedAt=0;
function tlFitPx(){ const w=$('tlScroll').clientWidth||800; return videoDur>0? w/videoDur : 1; }
function tlPxPerSec(){ return tlPx>0? tlPx : tlFitPx(); }
function tlAddBlock(parent,a,b,cls){
  if(b<=a) return null;
  const px=tlPxPerSec();
  const d=document.createElement('div'); d.className='tl-blk '+cls;
  d.style.left=(a*px)+'px'; d.style.width=Math.max(2,(b-a)*px)+'px';
  parent.appendChild(d); return d;
}
function seekPlay(t){ const p=$('player'); p.currentTime=t; p.play(); }
function normCand(c){ return {...c, source:c.source||(c.kind==='user'?'user':'silence')}; }
function renderTimeline(){
  if(!videoDur) return;
  const has = !!diar || cutCandidates.length>0;
  $('tlWrap').classList.toggle('hidden', !has);
  if(!has) return;
  const px=tlPxPerSec();
  $('tlCanvas').style.width=Math.max(Math.ceil(videoDur*px), $('tlScroll').clientWidth)+'px';
  const ruler=$('tlRuler'); ruler.innerHTML='';
  const steps=[1,2,5,10,15,30,60,120,300,600,1800];
  const step=steps.find(s=>s*px>=70)||1800;
  for(let t=0;t<=videoDur;t+=step){
    const d=document.createElement('div'); d.className='tl-tick';
    d.style.left=(t*px)+'px'; d.textContent=fmt(t); ruler.appendChild(d);
  }
  renderLane();
  positionPlayhead(); positionStartMarker(); syncZoomSlider();
}
function speakerAt(t){
  if(!diar) return null;
  const sg=diar.segments.find(x=>t>=x.start&&t<x.end);
  return sg? sg.speaker : null;
}
function renderLane(){
  const lane=$('tlLane'); lane.innerHTML='';
  const sv=parseT($('startInput').value), ev=parseT($('endInput').value);
  const s=sv==null?0:sv, e=ev==null?videoDur:ev;
  tlAddBlock(lane,0,s,'tl-out'); tlAddBlock(lane,e,videoDur,'tl-out');
  // [s, e] を話者区間とカット候補の境界で欠けなく分割する
  const pts=new Set([s,e]);
  const clip=t=>Math.max(s,Math.min(e,t));
  if(diar) diar.segments.forEach(sg=>{ pts.add(clip(sg.start)); pts.add(clip(sg.end)); });
  cutCandidates.forEach(c=>{ pts.add(clip(c.start)); pts.add(clip(c.end)); });
  const arr=[...pts].sort((a,b)=>a-b);
  const sel=selectedPresenters();
  for(let i=0;i+1<arr.length;i++){
    const a=arr[i], b=arr[i+1];
    if(b-a<0.02) continue;
    const mid=(a+b)/2;
    const sp=speakerAt(mid);
    const cand=cutCandidates.find(c=>mid>=c.start&&mid<c.end);
    let cls='tl-seg'+(sp==null?'':' sp'+sp);
    if(sp!=null && sel.length && !sel.includes(sp)) cls+=' sp-off';
    if(cand&&cand.enabled) cls+=' is-cut';
    if(cand&&!cand.enabled) cls+=' is-cand';
    if(cand&&cand.source==='user') cls+=' is-user';
    if(cand&&cand.source==='llm') cls+=' is-llm';
    if(cand&&cand.source==='ops') cls+=' is-ops';
    if(cand&&cand.source==='other') cls+=' is-other';
    if(cand) cls+=' tl-candblk';
    const d=tlAddBlock(lane,a,b,cls);
    if(!d) continue;
    const nm=sp==null?'話者なし':((diar.names&&diar.names[sp])||('話者'+(Number(sp)+1)));
    const state=!cand?'残す':(cand.enabled?'カット':'候補だけど残す');
    const kindTxt=cand?(cand.source==='user'?'タップで追加したカット'
      : cand.source==='llm'?('AI検出候補'+(cand.kind?': '+({trouble:'トラブル対応',fumbling:'もたつき',offtopic:'本題外'})[cand.kind]||cand.kind:''))
      : cand.source==='ops'?'運営の発話（デフォルトでカット候補）'
      : cand.source==='other'?'登壇者以外の発話（デフォルトでカット候補）'
      : '無音候補'):'';
    const reasonTxt=cand&&cand.reason?(' / 理由: '+cand.reason):'';
    d.title=`${nm} ${fmt(a)}〜${fmt(b)}（${state}${kindTxt?' / '+kindTxt:''}${reasonTxt}） タップで切替`;
    if((b-a)*tlPxPerSec()>52) d.textContent=nm;
    d.onclick=ev2=>{
      ev2.stopPropagation();
      if(Date.now()-tlDragMovedAt<300) return;
      if(cand){
        cand.enabled=!cand.enabled; renderCuts(); seekPlay(cand.start);
      }else{
        // 候補の無い保持ブロック: タップでその範囲をカット対象にする
        cutCandidates.push({start:Math.round(a*1000)/1000, end:Math.round(b*1000)/1000,
                            kind:'user', source:'user', enabled:true});
        cutCandidates.sort((x,y)=>x.start-y.start);
        renderCuts(); seekPlay(a);
      }
    };
  }
}
function positionPlayhead(){
  const p=$('player'); if(!p) return;
  $('tlPlayhead').style.left=((p.currentTime||0)*tlPxPerSec())+'px';
  const ct=$('tlCurTime'); if(ct) ct.textContent=`再生位置 ${fmt(p.currentTime||0)} / ${fmt(videoDur)}`;
}
function positionStartMarker(){
  const s=parseT($('startInput').value); const m=$('tlStartMarker');
  if(s==null){ m.style.display='none'; return; }
  m.style.display=''; m.style.left=(s*tlPxPerSec())+'px';
}
function syncZoomSlider(){
  const fit=tlFitPx(), maxr=TL_MAXPX/fit, sl=$('tlZoomSlider');
  if(maxr<=1){ sl.value=0; return; }
  sl.value=Math.round(100*Math.log(tlPxPerSec()/fit)/Math.log(maxr));
}
function tlZoomTo(px, centerTime){
  const sc=$('tlScroll');
  const ct = centerTime!=null? centerTime : (sc.scrollLeft+sc.clientWidth/2)/tlPxPerSec();
  tlPx=Math.min(TL_MAXPX, Math.max(tlFitPx(), px));
  renderTimeline();
  sc.scrollLeft=ct*tlPx - sc.clientWidth/2;
}
$('tlZoomInBtn').onclick=()=>tlZoomTo(tlPxPerSec()*1.5);
$('tlZoomOutBtn').onclick=()=>tlZoomTo(tlPxPerSec()/1.5);
$('tlFitBtn').onclick=()=>{ tlPx=0; renderTimeline(); $('tlScroll').scrollLeft=0; };
$('tlZoomSlider').oninput=e=>{
  const fit=tlFitPx(), maxr=TL_MAXPX/fit; if(maxr<=1) return;
  tlZoomTo(fit*Math.pow(maxr, Number(e.target.value)/100));
};
$('tlScroll').addEventListener('wheel', e=>{
  if(!e.ctrlKey) return;
  e.preventDefault();
  const cv=$('tlCanvas').getBoundingClientRect();
  const t=(e.clientX-cv.left)/tlPxPerSec();
  const sc=$('tlScroll');
  tlZoomTo(tlPxPerSec()*(e.deltaY<0?1.25:1/1.25), t);
  sc.scrollLeft = t*tlPx - (e.clientX - sc.getBoundingClientRect().left);
},{passive:false});
$('tlCanvas').onclick=e=>{
  if(Date.now()-tlDragMovedAt<300) return;
  const r=$('tlCanvas').getBoundingClientRect();
  const t=(e.clientX-r.left)/tlPxPerSec();
  if(t>=0&&t<=videoDur) $('player').currentTime=t;
};
$('player').addEventListener('timeupdate', ()=>{
  positionPlayhead();
  const sc=$('tlScroll'), x=($('player').currentTime||0)*tlPxPerSec();
  if(!$('player').paused && tlPx>0 && (x<sc.scrollLeft||x>sc.scrollLeft+sc.clientWidth)) sc.scrollLeft=x-40;
});
// 開始点マーカーのドラッグ (1秒単位)
(function(){
  const m=$('tlStartMarker'); let dragging=false;
  m.addEventListener('pointerdown', e=>{ dragging=true; try{m.setPointerCapture(e.pointerId);}catch(_){} e.preventDefault(); e.stopPropagation(); });
  m.addEventListener('pointermove', e=>{
    if(!dragging) return;
    const r=$('tlCanvas').getBoundingClientRect();
    let t=(e.clientX-r.left)/tlPxPerSec();
    t=Math.max(0,Math.min(videoDur,t));
    $('startInput').value=fmt(t); positionStartMarker();
  });
  m.addEventListener('pointerup', ()=>{ if(dragging){ dragging=false; tlDragMovedAt=Date.now(); updateDur(); renderTimeline(); } });
})();
$('startInput').addEventListener('input', ()=>renderTimeline());
$('endInput').addEventListener('input', ()=>renderTimeline());
window.addEventListener('resize', ()=>{ if(tlPx===0) renderTimeline(); });

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
      disabled_cuts:cutCandidates.filter(c=>!c.enabled&&c.source!=='user').map(c=>[c.start,c.end]),
      user_cuts:cutCandidates.filter(c=>c.enabled&&c.source==='user').map(c=>[c.start,c.end]),
      llm_cuts:cutCandidates.filter(c=>c.enabled&&c.source==='llm').map(c=>[c.start,c.end])})});
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
      if(info.plan.llm_detection) llmDetectInfo=info.plan.llm_detection;
      if(sil.candidates){ cutCandidates=sil.candidates.map(normCand); renderCuts(); }
      renderTimeline();
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
            return self._json({k: job.get(k) for k in ("stage", "error", "diarization", "progress", "silences", "silence_stage", "llm_detection")})
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
        """カット候補の検出を非同期で走らせる (stage は触らない)。

        無音候補に加え、文字起こしがあればデフォルト保持ポリシーの候補
        (登壇者以外の発話: source=ops/other) と、LLM検出の候補
        (もたつき・トラブル・本題外: source=llm) も返す。ポリシー候補の
        生成規則は edit.compute_plan と同じ (edit._policy_candidates)。
        LLM検出は APIキーが無ければスキップし、失敗しても他の候補は返す。
        検出ステータスは JOBS[job_id]["llm_detection"] に記録する
        (形式は edit.compute_plan の plan["llm_detection"] と同じ)。
        """
        def wrap():
            with LOCK:
                JOBS.setdefault(job_id, {})["silence_stage"] = "running"
            try:
                import sys
                sys.path.insert(0, str(BASE))
                from edit import detect_silences, probe
                d = job_dir(job_id)
                min_sec = float(params.get("silence_sec", 3.0))
                db = int(params.get("silence_db", -38))
                found = detect_silences(d / "input.mp4", min_sec, db)
                cands = [{"start": round(s, 3), "end": round(e, 3),
                          "kind": "silence", "source": "silence", "enabled": True}
                         for s, e in found if e - s >= min_sec - 0.05]
                llm_status = {"status": "skipped",
                              "detail": "transcript がありません"}
                tpath = d / "transcript.vtt"
                if tpath.exists() and tpath.stat().st_size > 0:
                    cues = None
                    try:
                        from transcript import build_result, parse_transcript
                        from edit import _policy_candidates
                        cues = parse_transcript(tpath)
                        result = build_result(cues)
                        if params.get("presenters") is not None:
                            presenters = [int(p) for p in params["presenters"]]
                        else:
                            guess = result.get(
                                "presenter_guess",
                                result.get("main_speaker_guess"))
                            presenters = [int(guess)] if guess is not None else []
                        dur = float(probe(d / "input.mp4")["format"]["duration"])
                        for cand in _policy_candidates(
                                result, cues, presenters, 0.0, dur):
                            cands.append({**cand, "enabled": True})
                        cands.sort(key=lambda c: c["start"])
                    except Exception:  # noqa: BLE001
                        pass  # ポリシー候補が作れなくても無音候補は返す
                    # LLM検出 (もたつき・トラブル・本題外)。97分級の
                    # transcript では窓分割で複数回のAPIコールになり時間が
                    # かかるが、この関数は非同期スレッドで動くので待ってよい
                    try:
                        import llm_detect
                        client = llm_detect.get_client_from_env()
                        if client is None:
                            llm_status = {
                                "status": "skipped",
                                "detail": "APIキー未設定 (GEMINI_API_KEY)"}
                        elif not cues:
                            llm_status = {
                                "status": "skipped",
                                "detail": "transcript を読めませんでした"}
                        else:
                            detected = llm_detect.detect_ranges(cues, client)
                            for cand in detected:
                                cands.append({**cand, "enabled": True})
                            cands.sort(key=lambda c: c["start"])
                            llm_status = {
                                "status": "ok", "count": len(detected),
                                "client": getattr(client, "name", "unknown")}
                    except Exception as e:  # noqa: BLE001
                        llm_status = {"status": "failed",
                                      "detail": str(e)[:200]}
                with LOCK:
                    JOBS[job_id]["silences"] = cands
                    JOBS[job_id]["llm_detection"] = llm_status
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
        if params.get("user_cuts") is not None:
            cfg["user_cuts"] = [[float(a), float(b)]
                                for a, b in params["user_cuts"]]
        if params.get("llm_cuts") is not None:
            cfg["llm_cuts"] = [[float(a), float(b)]
                               for a, b in params["llm_cuts"]]
        # 文字起こしが使えるジョブでは、書き出し時にも保持ポリシー
        # (登壇者以外の発話カット) が効くよう transcript を渡す。
        # パースできない文字起こしは渡さない (従来動作にフォールバック)。
        tpath = d / "transcript.vtt"
        if tpath.exists() and tpath.stat().st_size > 0:
            try:
                from transcript import result_from_transcript
                result_from_transcript(tpath)
                cfg["transcript"] = str(tpath)
            except Exception:  # noqa: BLE001
                pass
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
