// エディタUI (app.py 内蔵スクリプト) の動作テスト。
// ブラウザの無い環境向けに最小DOMシムを用意し、app.py から <script> を
// 抜き出して実コードを実行する。検証対象:
//   - 統合タイムラインの分割描画 (話者色 × カット状態が同時に出る)
//   - 候補ブロックのクリックで 切る⇔残す が切り替わる
//   - addUserCut で手動カットが追加され、タイムラインに反映される
//   - 「編集する」の送信内容に disabled_cuts / user_cuts が正しく入る
// 実行: node test/test_timeline.js
const fs = require('fs');
const path = require('path');

const appPy = fs.readFileSync(path.join(__dirname, '..', 'app.py'), 'utf8');
const m = appPy.match(/<script>([\s\S]*)<\/script>/);
if (!m) { console.error('script not found in app.py'); process.exit(1); }
const scriptSrc = m[1];

const prelude = `
const assert = require('assert');
class El {
  constructor(id){ this.id=id||''; this.children=[]; this.style={}; this._cls=new Set();
    this.value=''; this.checked=false; this.textContent=''; this.title='';
    this.disabled=false; this.currentTime=0; this.duration=0; this.paused=true;
    this.clientWidth=800; this._html=''; }
  get classList(){ const s=this._cls; return {
    add:(...c)=>c.forEach(x=>s.add(x)),
    remove:(...c)=>c.forEach(x=>s.delete(x)),
    toggle:(c,f)=>{ if(f===undefined) f=!s.has(c); if(f) s.add(c); else s.delete(c); return f; },
    contains:c=>s.has(c) }; }
  set className(v){ this._cls=new Set(String(v).split(/\\s+/).filter(Boolean)); }
  get className(){ return [...this._cls].join(' '); }
  set innerHTML(v){ this._html=v; if(v==='') this.children=[]; }
  get innerHTML(){ return this._html; }
  appendChild(c){ this.children.push(c); return c; }
  addEventListener(){}
  querySelectorAll(){ return []; }
  closest(){ return null; }
  getBoundingClientRect(){ return {left:0, width:800}; }
  play(){ this.paused=false; }
}
const _els = {};
const document = {
  getElementById: id => _els[id] || (_els[id] = new El(id)),
  createElement: () => new El(),
  querySelectorAll: () => [],
  querySelector: () => new El('q'),
};
const window = { addEventListener(){} };
const location = { search: '' };
class URLSearchParams { constructor(){} get(){ return null; } }
const _fetchCalls = [];
const fetch = async (url, opts) => {
  _fetchCalls.push({ url, opts });
  const body = String(url).includes('/api/status') ? { stage:'done', progress:100 } : {};
  return { json: async () => body };
};
`;

const tests = `
;(async () => {
  const lane = () => document.getElementById('tlLane');
  const segs = () => lane().children.filter(b => b._cls.has('tl-seg'));
  const byRange = (a, b) => segs().find(x =>
    Math.abs(parseFloat(x.style.left) / tlPxPerSec() - a) < 0.01 &&
    Math.abs(parseFloat(x.style.width) / tlPxPerSec() - (b - a)) < 0.05);

  // --- 準備: 100秒動画、話者0=0-40s / 話者1=40-100s、開始10s 終了90s ---
  videoDur = 100;
  diar = { segments: [ {speaker:0, start:0, end:40}, {speaker:1, start:40, end:100} ],
           speakers: {0:40, 1:60}, names: {0:'運営', 1:'登壇者'},
           presenter_guess: 1, main_speaker_guess: 1 };
  document.getElementById('startInput').value = '0:10';
  document.getElementById('endInput').value = '1:30';
  cutCandidates = [
    { start:20, end:30, kind:'silence', source:'silence', enabled:true },
    { start:50, end:55, kind:'silence', source:'silence', enabled:false },
  ];
  renderTimeline();

  // --- 統合レーンの分割: [10,90] が境界で欠けなく6区間に分かれる ---
  assert.strictEqual(segs().length, 6, 'segment blocks = 6, got ' + segs().length);
  assert.strictEqual(lane().children.length, 8, 'blocks incl. out-range = 8, got ' + lane().children.length);
  const b2030 = byRange(20, 30), b4045 = byRange(40, 50), b5055 = byRange(50, 55), b5560 = byRange(55, 90);
  assert(b2030 && b2030._cls.has('sp0') && b2030._cls.has('is-cut'), '[20,30] speaker0 + cut');
  assert(b4045 && b4045._cls.has('sp1') && !b4045._cls.has('is-cut'), '[40,50] speaker1 + keep');
  assert(b5055 && b5055._cls.has('sp1') && b5055._cls.has('is-cand') && !b5055._cls.has('is-cut'), '[50,55] kept candidate');
  assert(b5560 && !b5560._cls.has('is-cand'), '[55,90] plain keep');
  console.log('OK   lane partition (speaker x cut state in one lane)');

  // --- 候補クリックで 切る→残す に切り替わり、その位置を再生する ---
  b2030.onclick({ stopPropagation(){} });
  assert.strictEqual(cutCandidates[0].enabled, false, 'candidate disabled after click');
  assert.strictEqual(document.getElementById('player').currentTime, 20, 'seek to candidate start');
  assert(byRange(20, 30)._cls.has('is-cand') && !byRange(20, 30)._cls.has('is-cut'), 'redrawn as kept candidate');
  console.log('OK   candidate click toggles cut -> keep');

  // --- 手動カット追加 (ドラッグ選択と時刻指定の共通パス) ---
  assert.strictEqual(addUserCut(60, 70), true);
  assert.strictEqual(cutCandidates.length, 3);
  const b6070 = byRange(60, 70);
  assert(b6070 && b6070._cls.has('is-cut') && b6070._cls.has('is-user'), '[60,70] user cut drawn');
  // 手動カットもクリックで解除 (残す) できる
  b6070.onclick({ stopPropagation(){} });
  assert.strictEqual(cutCandidates.find(c => c.source === 'user').enabled, false, 'user cut toggled off');
  b6070.onclick({ stopPropagation(){} });
  assert.strictEqual(cutCandidates.find(c => c.source === 'user').enabled, true, 'user cut toggled back on');
  console.log('OK   user cut add / toggle on the timeline');

  // --- 「編集する」の送信内容: disabled_cuts と user_cuts が分かれて入る ---
  jobId = 'testjob'; uploaded = true;
  await document.getElementById('runBtn').onclick();
  const call = _fetchCalls.find(c => c.url === '/api/process/testjob');
  assert(call, 'process API called');
  const body = JSON.parse(call.opts.body);
  assert.deepStrictEqual(body.disabled_cuts, [[20, 30], [50, 55]], 'disabled_cuts: ' + JSON.stringify(body.disabled_cuts));
  assert.deepStrictEqual(body.user_cuts, [[60, 70]], 'user_cuts: ' + JSON.stringify(body.user_cuts));
  console.log('OK   process payload (disabled_cuts / user_cuts)');

  console.log('TIMELINE JS TEST OK');
})().catch(e => { console.error(e); process.exit(1); });
`;

eval(prelude + scriptSrc + tests);
