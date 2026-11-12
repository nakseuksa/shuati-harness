/* ==========================================================================
 * 刷题 Harness · 本地 Web 工作台（P4-B）
 * 零构建 Vanilla ES Module：无静态 import（保证 node --check 以脚本模式通过），
 * marked 经 CDN 动态 import，KaTeX 走全局脚本；两者缺失时优雅降级不破坏核心交互。
 *
 * 对接契约：docs/tasks/P4-A_api.md（7 端点 + /assets CAS）
 * DTO 依据：GateCardDTO(ingest_service) / SessionStepResponse(P2-E) /
 *           BlindDrillItemDTO·BlindDrillOutcomeDTO(P3-C) /
 *           GraphMetricsDTO(knowledge_graph) / DualEloState(elo_engine)
 * knowledge-graph 与 elo/ledger 的响应信封由 P4-A 并行定义，
 * 本文件用 normalizeGraph / normalizeElo 做容错归一（兼容数组/映射/扁平账本行）。
 * ========================================================================== */
'use strict';

/* ---------------------------------------------------------------- 常量 */
const API = '/api/v1';
const MARKED_CDN = 'https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js';
const LS_SESSION = 'shuati:session:v1';
const LS_THEME = 'shuati:theme';

const STAGE_LABELS = {
  STAGE_0_CONFIRM_GATE: '确认门禁',
  STAGE_1_BLIND_PROBE: '一阶 · 条件遮罩盲探',
  STAGE_2_MICRO_SCAFFOLD: '二阶 · 脱壳微母题',
  STAGE_3_REDO_ORIGINAL: '三阶 · 原题贯通',
  STAGE_4_DECRUTCH_VARIANT: '四阶 · 拆拐杖变式',
  STAGE_COOLDOWN_DEFERRED: '冷却顺延（次日重测）',
  SESSION_COMPLETED: '会话完成',
};
const STAGE_ORDER = [
  'STAGE_1_BLIND_PROBE',
  'STAGE_2_MICRO_SCAFFOLD',
  'STAGE_3_REDO_ORIGINAL',
  'STAGE_4_DECRUTCH_VARIANT',
];
const MASTERY_BADGE = {
  ACTIVE: ['badge-red', '🔴 ACTIVE 攻坚中'],
  PENDING_RETEST: ['badge-amber', '🟡 PENDING_RETEST 待复测'],
  MASTERED: ['badge-green', '🟢 MASTERED 已掌握'],
};
const PREWARM_BADGE = {
  IDLE: ['badge-gray', '预热待命'],
  RUNNING: ['badge-blue badge-pulse', '⏳ 思考窗口后台预热中'],
  READY_SYMPY: ['badge-green', '🛡️ SymPy 沙盒强校验通过'],
  READY_FALLBACK: ['badge-amber', '⚠️ 双盲 LLM 降级校验'],
  FAILED: ['badge-red', '预热失败'],
};
const CATEGORY_LABELS = {
  CONCEPT_BOTTLENECK: '🧠 思路断层',
  TRAP_OR_CALC_ERROR: '⚠️ 审题/计算粗心',
  ALREADY_MASTERED: '🟢 已掌握',
};
const TRAP_LABELS = {
  TRAP_DOMAIN_BOUNDARY: '定义域/临界边界遗漏',
  TRAP_SIGN_DIRECTION: '矢量方向/正负号反转',
  TRAP_EXTRANEOUS_ROOT: '增根与物理非解未舍',
  TRAP_FRAME_SHIFT: '参考系/研究对象暗换',
  TRAP_APPROX_CONDITION: '近似/守恒前提越界',
  TRAP_ARITHMETIC_SLIP: '移项通分/符号抄写失误',
};
const TRIGGER_META = {
  MASTERED_INIT: ['badge-green', '已掌握入库'],
  PARTIAL_DRAFT_SUBGOAL_FLOOR: ['badge-blue', '草稿子步保底'],
  SCAFFOLD_COVER_GAIN: ['badge-magenta', '覆盖分结算'],
  BLIND_TEST_REALIZE: ['badge-green', '盲测兑现'],
  ESCROW_TIMEOUT_DECAY: ['badge-amber', '托管超时衰减'],
};
const ESCROW_META = {
  FLOOR_ONLY: ['badge-blue', '仅保底'],
  FROZEN_PENDING_BLIND: ['badge-amber', '冻结待盲测'],
  REALIZED_MASTERED: ['badge-green', '已兑现'],
  FORFEITED_RESET: ['badge-red', '已清空退回'],
};

/* ---------------------------------------------------------------- 状态 */
const state = {
  tab: 'ingest',
  marked: null,
  // 录入
  initialState: '错题',
  previewOn: false,
  slots: { stem: [], draft: [] },
  lastSlot: null,
  // 门禁
  gate: null,            // GateCardDTO
  gateSel: 1,            // 用户微调后的卡点步骤（1-based）
  gateCategory: 'CONCEPT_BOTTLENECK',
  gateConfirmed: false,
  gateParams: null,      // {session_id, stalled_step_index, error_category}
  // 训练
  session: null,         // SessionStepResponse
  feedback: null,        // 提交后同阶段停留的反馈
  // 盲测
  drill: { date: todayISO(), items: [], outcomes: {}, loaded: false },
  // 看板
  graph: null,
  elo: null,
  eloModule: '',
  eloModuleOptions: [],
  graphLoaded: false,
  // 重试条
  lastRetry: null,
};

/* ---------------------------------------------------------------- 小工具 */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function todayISO() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function pick(obj, ...keys) {
  if (!obj || typeof obj !== 'object') return undefined;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  }
  return undefined;
}
function asArray(v) {
  if (v == null) return [];
  if (Array.isArray(v)) return v;
  if (typeof v === 'string') {
    try { const p = JSON.parse(v); return Array.isArray(p) ? p : []; } catch { return []; }
  }
  if (typeof v === 'object') return Object.values(v);
  return [];
}
function parseMaybeJson(v, fallback) {
  if (v == null) return fallback;
  if (typeof v !== 'string') return v;
  try { return JSON.parse(v); } catch { return fallback; }
}
function shortUid(u) { return String(u || '').slice(0, 8); }
function fmtTime(s) {
  if (!s) return '—';
  return String(s).replace('T', ' ').replace(/\.\d+/, '').slice(0, 16);
}
function fmtNum(v, digits = 1) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : '—';
}
function isEditable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/* ---------------------------------------------------------------- 富文本渲染
 * cas://([a-f0-9]{64}) → /assets/$1（渲染前统一重写，NFR Token 经济学）
 * $...$ / $$...$$ / \(...\) / \[...\] → KaTeX；CDN 缺失时降级为原文代码片。
 * ---------------------------------------------------------------- */
const MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$((?:\\.|[^\\$\n])+?)\$/g;

function rewriteCas(text) {
  return String(text).replace(/cas:\/\/([a-f0-9]{64})/g, '/assets/$1');
}

function miniMarkdown(text) {
  // marked CDN 不可用时的降级渲染（保持核心可读，不阻塞交互）
  const inline = (s) => esc(s)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img alt="$1" src="$2" />')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  const lines = String(text).split('\n');
  const out = [];
  let para = [];
  let list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join('')}</${list.tag}>`); list = null; } };
  let fence = null;
  for (const line of lines) {
    if (/^```/.test(line)) {
      if (fence == null) { flushPara(); flushList(); fence = []; }
      else { out.push(`<pre><code>${esc(fence.join('\n'))}</code></pre>`); fence = null; }
      continue;
    }
    if (fence) { fence.push(line); continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); continue; }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.、]\s+(.*)$/.exec(line);
    if (li || ol) {
      flushPara();
      const tag = li ? 'ul' : 'ol';
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push((li || ol)[1]);
      continue;
    }
    if (/^\s*$/.test(line)) { flushPara(); flushList(); continue; }
    if (/^>\s?/.test(line)) { flushPara(); flushList(); out.push(`<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`); continue; }
    para.push(line);
  }
  if (fence) out.push(`<pre><code>${esc(fence.join('\n'))}</code></pre>`);
  flushPara(); flushList();
  return out.join('\n');
}

function markdownToHtml(text) {
  if (state.marked) {
    try { return state.marked.parse(text, { gfm: true, breaks: true, async: false }); }
    catch { /* 落入降级 */ }
  }
  return miniMarkdown(text);
}

function renderMath(tex, displayMode) {
  const katex = window.katex;
  if (katex && typeof katex.renderToString === 'function') {
    try {
      return katex.renderToString(tex, { displayMode, throwOnError: false, strict: false, trust: false });
    } catch { /* 落入降级 */ }
  }
  const body = esc(displayMode ? `$$${tex}$$` : `$${tex}$`);
  return displayMode
    ? `<div class="math-fallback" style="display:block;text-align:center;margin:.5em 0">${body}</div>`
    : `<code class="math-fallback">${body}</code>`;
}

function renderRich(md) {
  if (md == null || md === '') return '';
  let text = rewriteCas(String(md));
  const mathStore = [];
  text = text.replace(MATH_RE, (match, dd, bracket, paren, inline) => {
    let tex = null; let display = false;
    if (dd !== undefined) { tex = dd; display = true; }
    else if (bracket !== undefined) { tex = bracket; display = true; }
    else if (paren !== undefined) { tex = paren; }
    else if (inline !== undefined) {
      if (/^\s|\s$/.test(inline)) return match; // 疑似货币符号，不渲染
      tex = inline;
    }
    if (tex == null) return match;
    mathStore.push({ tex: tex.trim(), display });
    return `@@MATH${mathStore.length - 1}@@`;
  });
  let html = markdownToHtml(text);
  html = html.replace(/@@MATH(\d+)@@/g, (m, i) => {
    const item = mathStore[Number(i)];
    return item ? renderMath(item.tex, item.display) : m;
  });
  return html;
}

function mountRich(el, md) {
  if (!el) return;
  el.classList.add('rich');
  el.innerHTML = renderRich(md);
}

/* ---------------------------------------------------------------- 统一 fetch 封装
 * 所有 API 调用经 apiFetch；错误统一抛 ApiError，
 * 由 runTask 捕获并弹出重试条（502 → 「模型网关暂时不可用，点击重试」）。
 * ---------------------------------------------------------------- */
class ApiError extends Error {
  constructor(status, message, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function apiFetch(path, opts = {}) {
  const { method = 'GET', json, form } = opts;
  const init = { method };
  if (form) init.body = form;
  else if (json !== undefined) {
    init.body = JSON.stringify(json);
    init.headers = { 'Content-Type': 'application/json' };
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, '无法连接本地服务：请确认 uvicorn 已启动（backend.main:app）', null);
  }
  const ct = res.headers.get('content-type') || '';
  let data = null;
  if (ct.includes('json')) { try { data = await res.json(); } catch { data = null; } }
  else { try { data = await res.text(); } catch { data = null; } }
  if (!res.ok) {
    let detail = data && typeof data === 'object' ? pick(data, 'detail', 'message') : data;
    if (detail && typeof detail !== 'string') detail = JSON.stringify(detail);
    throw new ApiError(res.status, detail || res.statusText || `HTTP ${res.status}`, data);
  }
  return data;
}

function errorMessage(err) {
  if (err instanceof ApiError) {
    if (err.status === 502) return '模型网关暂时不可用，点击重试';
    if (err.status === 0) return err.message;
    return `请求失败（HTTP ${err.status}）：${err.message}`;
  }
  return `前端异常：${err && err.message ? err.message : String(err)}`;
}

function showRetryBar(err, retryFn) {
  state.lastRetry = retryFn || null;
  const msg = $('#retry-msg');
  const main = errorMessage(err);
  const sub = err instanceof ApiError && err.status === 502
    ? '后端已自动重试 2 次仍失败（指数退避）；稍后再试或检查网关配置'
    : (err instanceof ApiError && err.status ? `端点返回 ${err.status}` : '本地网络 / 服务异常');
  msg.innerHTML = `${esc(main)}<small>${esc(sub)}</small>`;
  $('#retry-bar').hidden = false;
}
function hideRetryBar() { $('#retry-bar').hidden = true; state.lastRetry = null; }

/** 按钮级任务包装：aria-busy 加载态 + 失败重试条。 */
async function runTask(btn, fn) {
  if (btn) { btn.setAttribute('aria-busy', 'true'); btn.disabled = true; }
  try {
    await fn();
  } catch (err) {
    showRetryBar(err, () => runTask(btn, fn));
  } finally {
    if (btn) { btn.removeAttribute('aria-busy'); btn.disabled = false; }
  }
}

/* ---------------------------------------------------------------- Toast */
function toast(message, kind = 'info', ms = 3400) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 220);
  }, ms);
}

/* ---------------------------------------------------------------- 页签 */
function switchTab(name) {
  state.tab = name;
  $$('.tab-btn').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
  $$('.view').forEach((v) => { v.hidden = v.id !== `view-${name}`; });
  if (name === 'training') renderTraining();
  if (name === 'drill' && !state.drill.loaded) loadDrill();
  if (name === 'graph' && !state.graphLoaded) { state.graphLoaded = true; loadGraphAndElo(); }
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/* ---------------------------------------------------------------- 主题 */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#theme-toggle').textContent = theme === 'dark' ? '☀️' : '🌙';
  try { localStorage.setItem(LS_THEME, theme); } catch { /* 隐私模式忽略 */ }
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(LS_THEME); } catch { /* ignore */ }
  const prefer = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(saved === 'dark' || saved === 'light' ? saved : prefer);
}

/* ================================================================
 * ① 录入（CUJ 1）：双槽 paste 隔离 + GateCard 微确认条
 * ================================================================ */
function setupSlot(key) {
  const el = $(`#slot-${key}`);
  const fileInput = $('[data-file-input]', el);

  const markActive = () => {
    state.lastSlot = key;
    $$('.drop-slot').forEach((s) => s.classList.toggle('slot-active', s === el));
  };
  el.addEventListener('focus', markActive);
  el.addEventListener('click', (e) => {
    if (e.target.closest('.thumb-x') || e.target.closest('.thumb img')) return;
    markActive();
    fileInput.click();
  });
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); markActive(); fileInput.click(); }
  });
  fileInput.addEventListener('change', () => {
    addSlotFiles(key, Array.from(fileInput.files || []));
    fileInput.value = '';
  });
  // 槽内 paste 拦截：图片只进本槽，严禁混槽
  el.addEventListener('paste', (e) => { markActive(); handleSlotPaste(key, e); });
  el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('slot-drag'); });
  el.addEventListener('dragleave', () => el.classList.remove('slot-drag'));
  el.addEventListener('drop', (e) => {
    e.preventDefault();
    el.classList.remove('slot-drag');
    markActive();
    addSlotFiles(key, Array.from(e.dataTransfer?.files || []));
  });
}

function handleSlotPaste(key, e) {
  const items = Array.from(e.clipboardData?.items || []);
  const images = items
    .filter((it) => it.kind === 'file' && /^image\//.test(it.type))
    .map((it) => it.getAsFile())
    .filter(Boolean);
  if (images.length) {
    e.preventDefault();
    e.stopPropagation();
    addSlotFiles(key, images);
    toast(`已粘贴 ${images.length} 张图片到「${key === 'stem' ? '题干与配图槽' : '手写草稿截图槽'}」`, 'ok');
    return;
  }
  const text = e.clipboardData?.getData('text/plain') || '';
  if (key === 'stem' && text) {
    // 题干槽的纯文本并入题干输入框
    e.preventDefault();
    e.stopPropagation();
    const ta = $('#stem-input');
    const start = ta.selectionStart ?? ta.value.length;
    const end = ta.selectionEnd ?? ta.value.length;
    ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + text.length;
    onStemInput();
    toast('粘贴文本已并入题干', 'ok');
  } else if (key === 'draft') {
    e.preventDefault();
    e.stopPropagation();
    toast('手写草稿槽仅接受图片（截图/拍照）', 'warn');
  }
}

let slotSeq = 0;
function addSlotFiles(key, files) {
  const accepted = files.filter((f) => f && /^image\//.test(f.type));
  if (!accepted.length) { if (files.length) toast('仅支持图片文件', 'warn'); return; }
  for (const file of accepted) {
    state.slots[key].push({ id: `img-${++slotSeq}`, file, url: URL.createObjectURL(file) });
  }
  renderSlotThumbs(key);
}
function removeSlotFile(key, id) {
  const list = state.slots[key];
  const idx = list.findIndex((f) => f.id === id);
  if (idx >= 0) { URL.revokeObjectURL(list[idx].url); list.splice(idx, 1); }
  renderSlotThumbs(key);
}
function renderSlotThumbs(key) {
  const el = $(`#slot-${key}`);
  const list = state.slots[key];
  $('[data-slot-count]', el).textContent = `${list.length} 张`;
  $('[data-thumbs]', el).innerHTML = list.map((f) => `
    <span class="thumb">
      <img src="${f.url}" alt="已选图片 ${esc(f.file.name || '剪贴板图片')}" data-action="thumb-zoom" data-url="${f.url}" />
      <button type="button" class="thumb-x" data-action="thumb-remove" data-slot="${key}" data-id="${f.id}"
              aria-label="移除该图片">✕</button>
    </span>`).join('');
}

function onStemInput() {
  const v = $('#stem-input').value;
  $('#stem-count').textContent = `${v.length} 字`;
  if (state.previewOn) mountRich($('#stem-preview'), v || '');
}
function togglePreview() {
  state.previewOn = !state.previewOn;
  const box = $('#stem-preview');
  box.hidden = !state.previewOn;
  if (state.previewOn) {
    const v = $('#stem-input').value;
    if (v) mountRich(box, v);
    else box.innerHTML = '<span class="preview-empty">暂无题干内容可预览</span>';
  }
}

async function submitIngest(btn) {
  const stemMd = $('#stem-input').value.trim();
  const grade = $('#grade-input').value.trim() || '未指定学段';
  const errEl = $('#ingest-error');
  errEl.hidden = true;
  if (!stemMd && !state.slots.stem.length) {
    errEl.textContent = '请粘贴题干文本，或在「题干与配图槽」上传原题截图后再提交。';
    errEl.hidden = false;
    return;
  }
  const form = new FormData();
  form.append('stem_md', stemMd);
  form.append('target_grade', grade);
  form.append('initial_state', state.initialState);
  for (const f of state.slots.stem) form.append('stem_images', f.file, f.file.name || 'stem.png');
  for (const f of state.slots.draft) form.append('draft_images', f.file, f.file.name || 'draft.png');
  await runTask(btn, async () => {
    const dto = await apiFetch(`${API}/questions/ingest`, { method: 'POST', form });
    state.gate = dto;
    state.gateConfirmed = false;
    const blocked = (dto.steps || []).find((s) => s.status === 'blocked');
    state.gateSel = blocked ? blocked.step_index : Math.max(1, (dto.steps || []).length);
    state.gateCategory = dto.error_category || 'CONCEPT_BOTTLENECK';
    renderGate();
    toast(dto.is_reimport ? '♻️ 幂等重入库：历史盲测进度与 Elo 账本已保留' : '入库成功，请核对步骤对齐确认卡', 'ok');
    $('#gate-slot').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
}

/* ---------------- 步骤对齐微确认条（GateCardDTO） ---------------- */
function gateFloorElo() {
  const steps = state.gate?.steps || [];
  if (state.gateCategory === 'ALREADY_MASTERED') return null;
  return state.gateSel > 1 ? (steps[state.gateSel - 2]?.sub_elo ?? null) : null;
}

function renderGate() {
  const slot = $('#gate-slot');
  const g = state.gate;
  if (!g) return;
  const steps = g.steps || [];
  const mastered = state.gateCategory === 'ALREADY_MASTERED';
  const floor = gateFloorElo();
  const statusOf = (s) => {
    if (mastered) return 'completed';
    if (s.step_index < state.gateSel) return 'completed';
    if (s.step_index === state.gateSel) return 'blocked';
    return 'untouched';
  };
  const statusView = {
    completed: ['is-completed', '✅ 已独立完成'],
    blocked: ['is-blocked', '❌ 思路中断'],
    untouched: ['is-untouched', '⬜ 未触及'],
  };
  slot.innerHTML = `
  <div class="card gate-card">
    <div class="card-title">🧭 步骤对齐微确认条
      <span class="hint">Step-Skeleton Diff Gate · 点击步骤行可微调卡点</span>
    </div>
    <div class="gate-meta">
      <span class="chip">难度 ${esc(g.base_elo)} · ${esc(g.tier)}</span>
      ${g.is_reimport ? '<span class="badge badge-blue">♻️ 幂等重入库</span>' : ''}
      ${g.trap_taxonomy_tag ? `<span class="badge badge-magenta">🪤 ${esc(TRAP_LABELS[g.trap_taxonomy_tag] || g.trap_taxonomy_tag)}</span>` : ''}
      <span class="badge badge-gray mono" style="font-size:11px">uid ${esc(shortUid(g.question_uid))}</span>
    </div>
    ${mastered ? `
      <div class="gate-floor" style="background:var(--ok-bg);border-color:color-mix(in srgb,var(--ok) 35%,transparent)">
        🟢 初始状态为「已掌握」：确认后直接归档结算（水位线门控），不进入脚手架训练。
      </div>` : `
      <div class="seg ${state.gateCategory === 'TRAP_OR_CALC_ERROR' ? 'seg-warn' : 'seg-danger'}" role="group" aria-label="错因归因一键切换" style="margin-bottom:10px">
        <button type="button" data-action="set-category" data-value="CONCEPT_BOTTLENECK"
                aria-pressed="${state.gateCategory === 'CONCEPT_BOTTLENECK'}" ${state.gateConfirmed ? 'disabled' : ''}>🧠 思路断层</button>
        <button type="button" data-action="set-category" data-value="TRAP_OR_CALC_ERROR"
                aria-pressed="${state.gateCategory === 'TRAP_OR_CALC_ERROR'}" ${state.gateConfirmed ? 'disabled' : ''}>⚠️ 审题/计算粗心</button>
      </div>`}
    <div class="gate-steps" role="list">
      ${steps.map((s) => {
        const st = statusOf(s);
        const [cls, label] = statusView[st];
        return `
        <button type="button" role="listitem" class="gate-step ${cls}" data-action="gate-pick-step"
                data-step="${s.step_index}" ${state.gateConfirmed || mastered ? 'disabled' : ''}
                title="点击将卡点微调到 Step ${s.step_index}">
          <span class="step-no">Step ${s.step_index}</span>
          <span class="step-goal">${renderRich(s.step_goal_desc)}</span>
          <span class="step-status">${label}</span>
          <span class="step-elo">保底 ${esc(s.sub_elo)}</span>
        </button>`;
      }).join('')}
    </div>
    ${floor != null ? `
      <div class="gate-floor">🛟 已完成前缀保底水位 <strong>${esc(floor)}</strong>
        <span class="field-help">确认后若模块实战分低于该水位，将发放 PARTIAL_DRAFT_SUBGOAL_FLOOR 保底修正</span>
      </div>` : (!mastered ? '<div class="gate-floor" style="background:var(--surface-2);border-color:var(--line)">🛟 第 1 步即卡壳：无前缀保底水位</div>' : '')}
    ${state.gateConfirmed ? `
      <div class="btn-row">
        <span class="badge badge-green">✅ 门禁已确认</span>
        <button class="btn btn-primary" type="button" data-action="goto-training">🧗 进入训练视图</button>
      </div>` : `
      <div class="btn-row">
        <button class="btn btn-primary btn-lg" type="button" data-action="confirm-gate" id="gate-confirm-btn">
          ✅ 确认无误，开始训练
        </button>
        <span class="gate-confirm-hint">或按 <kbd>Enter</kbd> 一键确认 · 归因：${esc(CATEGORY_LABELS[state.gateCategory] || state.gateCategory)} · 卡点 Step ${state.gateSel}</span>
      </div>`}
  </div>`;
}

async function confirmGate(btn) {
  const g = state.gate;
  if (!g || state.gateConfirmed) return;
  const params = {
    session_id: g.session_id,
    stalled_step_index: state.gateSel,
    error_category: state.gateCategory,
  };
  await runTask(btn, async () => {
    const resp = await apiFetch(`${API}/sessions/${encodeURIComponent(params.session_id)}/confirm-gate`, {
      method: 'POST',
      json: { stalled_step_index: params.stalled_step_index, error_category: params.error_category },
    });
    state.gateParams = params;
    state.gateConfirmed = true;
    state.session = resp;
    state.feedback = null;
    persistSession();
    renderGate();
    renderResumeBanner();
    switchTab('training');
    toast('门禁已确认，后台预热已启动', 'ok');
  });
}

/* 回车确认门禁：仅在录入页、确认卡可见、焦点不在可编辑控件时生效 */
function onGlobalKeydown(e) {
  if (e.key === 'Escape') { closeLightbox(); return; }
  if (e.key !== 'Enter' || e.isComposing || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
  if (state.tab !== 'ingest' || !state.gate || state.gateConfirmed) return;
  if (!(e.target instanceof Element) || isEditable(e.target) || e.target.closest('button')) return;
  if (!$('#lightbox').hidden) return;
  const btn = $('#gate-confirm-btn');
  if (!btn) return;
  e.preventDefault();
  confirmGate(btn);
}

/* ================================================================
 * ② 训练（CUJ 2 / 3）：SessionStepResponse 驱动的四阶视图
 * ================================================================ */
function persistSession() {
  try {
    if (state.session) {
      localStorage.setItem(LS_SESSION, JSON.stringify({
        resp: state.session, gateParams: state.gateParams, savedAt: Date.now(),
      }));
    } else {
      localStorage.removeItem(LS_SESSION);
    }
  } catch { /* 存储不可用时仅内存态 */ }
}
function loadPersistedSession() {
  try {
    const raw = localStorage.getItem(LS_SESSION);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (data && data.resp && data.resp.session_id) return data;
  } catch { /* ignore */ }
  return null;
}

function renderResumeBanner() {
  const slot = $('#resume-slot');
  const persisted = loadPersistedSession();
  const sess = state.session || (persisted && persisted.resp);
  if (!sess || sess.micro_stage === 'SESSION_COMPLETED') { slot.innerHTML = ''; return; }
  slot.innerHTML = `
    <div class="resume-banner">
      <span aria-hidden="true">⏸️</span>
      <span>有暂存的训练进度：<strong>${esc(STAGE_LABELS[sess.micro_stage] || sess.micro_stage)}</strong>
        <span class="mono" style="font-size:11px">uid ${esc(shortUid(sess.question_uid))}</span></span>
      <span class="spacer"></span>
      <button class="btn btn-sm btn-primary" type="button" data-action="resume-training">▶️ 继续训练（断点续传）</button>
      <button class="btn btn-sm btn-ghost" type="button" data-action="discard-session">丢弃</button>
    </div>`;
}

function stageTracker(stage) {
  let currentIdx = STAGE_ORDER.indexOf(stage);
  if (stage === 'STAGE_COOLDOWN_DEFERRED') currentIdx = 3;
  if (stage === 'SESSION_COMPLETED') currentIdx = 4;
  if (stage === 'STAGE_0_CONFIRM_GATE') currentIdx = -1;
  const nodes = STAGE_ORDER.map((s, i) => {
    const cls = i < currentIdx ? 'done' : i === currentIdx ? 'current' : '';
    const dot = i < currentIdx ? '✓' : String(i + 1);
    return `<span class="stage-node ${cls}"><span class="stage-dot">${dot}</span>${esc(STAGE_LABELS[s].split(' · ')[1] || STAGE_LABELS[s])}</span>`;
  });
  return `<div class="stage-track" aria-label="训练阶段进度">${nodes.join('<span class="stage-link" aria-hidden="true"></span>')}</div>`;
}

function badgeHtml(map, key, fallbackLabel) {
  const meta = map[key];
  if (meta) return `<span class="badge ${meta[0]}">${esc(meta[1])}</span>`;
  return `<span class="badge badge-gray">${esc(fallbackLabel || key || '未知')}</span>`;
}

function fallbackBannerHtml(resp) {
  const v = resp.view_payload || {};
  const isFallback = resp.prewarm_status === 'READY_FALLBACK'
    || pick(v, 'verification_status') === 'DUAL_LLM_FALLBACK';
  if (!isFallback) return '';
  return `
    <div class="fallback-banner" role="status">
      <span>⚠️ 未经符号沙盒强校验</span>
      <span class="field-help" style="color:inherit;font-weight:400">沙盒超时/非代数题，已降级双盲 LLM 交叉求解核对</span>
      <span class="spacer"></span>
      <button class="btn btn-sm btn-warn" type="button" data-action="fallback-skip">跳过此题</button>
      <button class="btn btn-sm btn-warn" type="button" data-action="fallback-swap">🔄 换一题（重新热备）</button>
    </div>`;
}

function explainBarHtml(text) {
  if (!text) return '';
  return `<div class="explain-bar"><span class="explain-icon" aria-hidden="true">📈</span><div class="rich">${renderRich(text)}</div></div>`;
}

function answerZoneHtml(resp) {
  const v = resp.view_payload || {};
  const mode = pick(v, 'verification_mode', 'mode')
    || (asArray(pick(v, 'allowed_symbols', 'symbols')).length ? 'equation_skeleton' : 'final_answer');
  const symbols = asArray(pick(v, 'allowed_symbols', 'symbols'));
  const isEq = mode === 'equation_skeleton';
  return `
    <div class="answer-zone">
      <div class="answer-label">${isEq ? '🧮 核心列式验证模式（L≥2 · SymPy 代数等价，免化简）' : '✍️ 你的作答'}</div>
      ${isEq && symbols.length ? `
        <div class="field-help" style="margin-bottom:4px">受控符号表（点击插入光标处）：</div>
        <div class="symbol-row" role="group" aria-label="allowed_symbols 符号芯片">
          ${symbols.map((s) => `<button type="button" class="symbol-chip" data-action="insert-symbol" data-symbol="${esc(s)}" data-target="stage-answer">${esc(s)}</button>`).join('')}
        </div>` : ''}
      ${isEq
        ? `<textarea id="stage-answer" class="mono-input" rows="4" data-eq="1"
             placeholder="只列决定临界状态的核心方程组，如：R + R*sin(theta) = d ; q*v*B = m*v**2/R&#10;多个方程用 ; 或换行分隔，无需化简求值"></textarea>
           <div class="field-help">支持移项反号 / 常数倍等价判定；代数式用 SymPy 语法（** 幂、sqrt()、sin() 等）</div>`
        : `<textarea id="stage-answer" rows="3" placeholder="输入最终答案或关键表达式（支持 LaTeX / SymPy 语法）"></textarea>`}
      ${state.feedback ? `<div class="feedback-line bad">${esc(state.feedback)}</div>` : ''}
      <div class="btn-row" style="margin-top:12px">
        <button class="btn btn-primary" type="button" data-action="stage-submit">📤 提交作答</button>
        ${resp.micro_stage === 'STAGE_1_BLIND_PROBE'
          ? '<button class="btn" type="button" data-action="stage-noidea">🪜 仍无思路，请求二阶微母题铺垫</button>'
          : ''}
      </div>
    </div>`;
}

/** view_payload 中未被消费的字符串字段 → 附加信息卡（不丢信息） */
function extraPayloadHtml(v, consumed) {
  const entries = Object.entries(v || {}).filter(([k, val]) =>
    !consumed.includes(k) && typeof val === 'string' && val.trim() && !/^https?:/.test(val));
  if (!entries.length) return '';
  return `<div class="subcard" style="margin-top:12px">
    <h4 style="font-size:12.5px;color:var(--ink-faint)">附加信息</h4>
    ${entries.map(([k, val]) => `<div style="margin:6px 0"><span class="chip" style="font-size:11px">${esc(k)}</span><div class="rich" style="margin-top:4px">${renderRich(val)}</div></div>`).join('')}
  </div>`;
}

function renderTraining() {
  const root = $('#training-root');
  const resp = state.session;
  if (!resp) {
    const persisted = loadPersistedSession();
    root.innerHTML = `
      <div class="card empty-state">
        <div class="big" aria-hidden="true">🧗</div>
        <h3>暂无进行中的训练会话</h3>
        <p>先在「录入」页提交错题并通过步骤对齐微确认门禁；确认后将自动切入一阶条件遮罩盲探。</p>
        ${persisted ? '<button class="btn btn-primary" type="button" data-action="resume-training">▶️ 恢复暂存进度</button>'
          : '<button class="btn" type="button" data-action="goto-ingest">📥 去录入一道错题</button>'}
      </div>`;
    return;
  }
  const v = resp.view_payload || {};
  const stage = resp.micro_stage;
  const stem = pick(v, 'stem_md', 'question_stem_md', 'original_stem_md', 'variant_stem_md', 'scaffold_stem_md', 'stem');
  const [mCls, mLabel] = MASTERY_BADGE[resp.mastery_state] || ['badge-gray', resp.mastery_state];
  let body = '';

  if (stage === 'STAGE_1_BLIND_PROBE') {
    const conditions = asArray(parseMaybeJson(pick(v, 'masked_conditions', 'masked_conditions_json', 'conditions'), []));
    const probe = pick(v, 'probe_question_md', 'probe_md', 'probe');
    body = `
      <div class="mask-banner">🕶️ 遮罩视图：原题最终求解目标已隐藏 —— 仅高亮卡点步所需的局部已知条件</div>
      <div class="masked-conditions">
        ${conditions.length
          ? conditions.map((c, i) => `<div class="masked-cond" style="animation-delay:${i * 60}ms"><span class="field-help" style="font-weight:800">已知条件 ${i + 1}</span><div class="rich">${renderRich(typeof c === 'string' ? c : JSON.stringify(c))}</div></div>`).join('')
          : '<div class="masked-cond">（后端未提供遮罩条件，请直接阅读下方盲探反问）</div>'}
      </div>
      <div class="probe-box">
        <div class="probe-label">盲探反问 · PROBE</div>
        <div class="rich">${renderRich(probe || '（缺少 probe_question_md）')}</div>
      </div>
      ${answerZoneHtml(resp)}
      ${extraPayloadHtml(v, ['masked_conditions', 'masked_conditions_json', 'conditions', 'probe_question_md', 'probe_md', 'probe', 'verification_mode', 'mode', 'allowed_symbols', 'symbols'])}`;
  } else if (stage === 'STAGE_2_MICRO_SCAFFOLD') {
    body = `
      ${fallbackBannerHtml(resp)}
      <div class="mask-banner" style="border-left:5px solid var(--magenta)">🐣 二阶 · 脱壳微母题：已剥离原题背景，只练卡点那一步的原子转化</div>
      <div class="drill-stem rich">${renderRich(stem || '（微母题题干缺失：预热可能仍在途，提交后将同步等待）')}</div>
      ${answerZoneHtml(resp)}
      ${extraPayloadHtml(v, ['stem_md', 'scaffold_stem_md', 'stem', 'verification_mode', 'mode', 'allowed_symbols', 'symbols', 'verification_status'])}`;
  } else if (stage === 'STAGE_3_REDO_ORIGINAL') {
    body = `
      <div class="mask-banner" style="border-left:5px solid var(--green)">🔓 三阶 · 无提示回原题：所有遮罩与微母题已撤去，请独立贯通完整原题</div>
      <div class="drill-stem rich">${renderRich(stem || '（原题题干缺失）')}</div>
      ${answerZoneHtml(resp)}
      ${extraPayloadHtml(v, ['stem_md', 'original_stem_md', 'question_stem_md', 'stem', 'verification_mode', 'mode', 'allowed_symbols', 'symbols'])}`;
  } else if (stage === 'STAGE_4_DECRUTCH_VARIANT') {
    const mode = pick(v, 'verification_mode', 'mode');
    body = `
      ${fallbackBannerHtml(resp)}
      <div class="mask-banner" style="border-left:5px solid var(--amber)">🏗️ 四阶 · 当场拆拐杖同构变式${mode === 'equation_skeleton' ? '：半程立式，只考核心方程组列式' : '：提交最终答案'}</div>
      <div class="drill-stem rich">${renderRich(stem || '（变式题干缺失：提交后将同步等待在途预热）')}</div>
      ${answerZoneHtml(resp)}
      ${extraPayloadHtml(v, ['stem_md', 'variant_stem_md', 'stem', 'verification_mode', 'mode', 'allowed_symbols', 'symbols', 'verification_status'])}`;
  } else if (stage === 'STAGE_COOLDOWN_DEFERRED') {
    const mine = pick(v, 'user_answer', 'user_expr', 'user_equation', 'your_answer', 'submitted_answer');
    const std = pick(v, 'standard_answer_latex', 'standard_expr', 'standard_equation', 'expected_answer', 'answer_latex', 'standard_answer');
    body = `
      <div class="mask-banner" style="border-left:5px solid var(--info)">🧊 当场变式未通过：进入冷却顺延，次日冷却期满后可重测（不惩罚实战分基线）</div>
      <div class="skeleton-compare">
        <div class="subcard mine"><h4>你的列式 / 作答</h4><div class="rich">${renderRich(mine || '（未提供）')}</div></div>
        <div class="subcard std"><h4>标准骨架</h4><div class="rich">${renderRich(std || '（未提供）')}</div></div>
      </div>
      <div class="field-help">对照骨架找出列式差异；明日再来点击下面按钮回到四阶重测。</div>
      <div class="btn-row" style="margin-top:12px">
        <button class="btn btn-primary" type="button" data-action="cooldown-resume">⏭️ 冷却期满，返回拆拐杖重测</button>
      </div>
      ${extraPayloadHtml(v, ['user_answer', 'user_expr', 'user_equation', 'your_answer', 'submitted_answer', 'standard_answer_latex', 'standard_expr', 'standard_equation', 'expected_answer', 'answer_latex', 'standard_answer'])}`;
  } else if (stage === 'SESSION_COMPLETED') {
    const unlock = pick(v, 'blind_test_unlock_at', 'unlock_at');
    const due = pick(v, 'blind_test_due_at', 'due_at');
    const escrow = pick(v, 'escrow_combat_delta');
    const mastered = resp.mastery_state === 'MASTERED';
    body = `
      <div class="complete-hero">
        <div class="big" aria-hidden="true">${mastered ? '🏆' : '🎉'}</div>
        <h2>${mastered ? '本题已掌握归档' : '当场拆拐杖通过！'}</h2>
        <p class="sub">${mastered
          ? '水位线结算完成，直接归档为 🟢MASTERED。'
          : '覆盖分已结算，实战分差额已 100% 冻结进单题托管池，等待延迟盲测兑现。'}</p>
        <div class="window-chips">
          ${unlock ? `<span class="badge badge-blue">🔓 盲测解锁 ${esc(fmtTime(unlock))}</span>` : ''}
          ${due ? `<span class="badge badge-amber">⏳ 盲测截止 ${esc(fmtTime(due))}</span>` : ''}
          ${escrow != null ? `<span class="badge badge-magenta">🏦 托管差额 +${esc(fmtNum(escrow))}</span>` : ''}
        </div>
      </div>
      ${explainBarHtml(resp.explanation_text)}
      <div class="btn-row" style="justify-content:center">
        <button class="btn btn-primary" type="button" data-action="goto-drill">🎯 前往每日盲测</button>
        <button class="btn" type="button" data-action="goto-ingest">📥 录入下一题</button>
      </div>
      ${extraPayloadHtml(v, ['blind_test_unlock_at', 'unlock_at', 'blind_test_due_at', 'due_at', 'escrow_combat_delta'])}`;
  } else {
    body = `
      <div class="mask-banner">当前阶段：${esc(STAGE_LABELS[stage] || stage)}</div>
      ${stem ? `<div class="drill-stem rich">${renderRich(stem)}</div>` : ''}
      ${stage === 'STAGE_0_CONFIRM_GATE'
        ? '<div class="btn-row"><button class="btn btn-primary" type="button" data-action="goto-ingest">🧭 回到录入页确认门禁</button></div>'
        : answerZoneHtml(resp)}
      ${extraPayloadHtml(v, ['stem_md', 'stem'])}`;
  }

  root.innerHTML = `
    <div class="session-head">
      ${badgeHtml(MASTERY_BADGE, resp.mastery_state)}
      ${badgeHtml(PREWARM_BADGE, resp.prewarm_status)}
      <span class="badge badge-blue">${esc(STAGE_LABELS[stage] || stage)}</span>
      <span class="uid">session ${esc(shortUid(resp.session_id))} · uid ${esc(shortUid(resp.question_uid))}</span>
      <span class="spacer"></span>
      ${stage !== 'SESSION_COMPLETED' ? '<button class="btn btn-sm" type="button" data-action="stash-progress">⏸️ 暂存进度，次日继续</button>' : ''}
    </div>
    ${stageTracker(stage)}
    <div class="card">
      ${stage !== 'SESSION_COMPLETED' ? explainBarHtml(resp.explanation_text) : ''}
      ${body}
    </div>`;
}

async function submitStage(btn, selfAssessment) {
  const resp = state.session;
  if (!resp) return;
  const input = $('#stage-answer');
  const answer = input ? input.value.trim() : '';
  if (!selfAssessment && !answer) {
    toast('请先输入作答内容', 'warn');
    if (input) input.focus();
    return;
  }
  const prevStage = resp.micro_stage;
  await runTask(btn, async () => {
    const next = await apiFetch(`${API}/sessions/${encodeURIComponent(resp.session_id)}/step-submit`, {
      method: 'POST',
      json: { user_answer: answer, self_assessment: selfAssessment || null },
    });
    state.session = next;
    state.feedback = next.micro_stage === prevStage
      && ['STAGE_1_BLIND_PROBE', 'STAGE_2_MICRO_SCAFFOLD', 'STAGE_3_REDO_ORIGINAL', 'STAGE_4_DECRUTCH_VARIANT'].includes(next.micro_stage)
      ? '❌ 未通过等价校验：再检查一下思路 / 列式，或请求铺垫。'
      : null;
    persistSession();
    renderTraining();
    renderResumeBanner();
    if (next.micro_stage !== prevStage) {
      toast(next.micro_stage === 'SESSION_COMPLETED' ? '🎉 会话完成' : `进入 ${STAGE_LABELS[next.micro_stage] || next.micro_stage}`, 'ok');
    }
  });
}

async function swapFallbackQuestion(btn) {
  // 换题 = 重新提交门禁确认 → 再次触发热备（P2-E：confirm 总是启动 prewarm）
  const p = state.gateParams;
  if (!p) {
    toast('缺少门禁参数：请回录入页重新确认后再换题', 'warn');
    return;
  }
  await runTask(btn, async () => {
    const next = await apiFetch(`${API}/sessions/${encodeURIComponent(p.session_id)}/confirm-gate`, {
      method: 'POST',
      json: { stalled_step_index: p.stalled_step_index, error_category: p.error_category },
    });
    state.session = next;
    state.feedback = null;
    persistSession();
    renderTraining();
    toast('🔄 已重新提交并触发热备，回到盲探起点', 'ok');
  });
}

/* ================================================================
 * ③ 每日盲测（CUJ 3）：剥标签题单 + 双重核验 + 销账结果
 * ================================================================ */
function normalizeDrillItems(raw) {
  const list = Array.isArray(raw) ? raw : asArray(pick(raw || {}, 'items', 'queue', 'drills', 'data'));
  return list.map((it) => ({
    question_uid: pick(it, 'question_uid', 'uid') || '',
    drill_question_uid: pick(it, 'drill_question_uid') || '',
    stem_md: pick(it, 'stem_md', 'stem') || '',
    verification_mode: pick(it, 'verification_mode', 'mode') || 'final_answer',
    allowed_symbols: asArray(pick(it, 'allowed_symbols', 'symbols')),
    probe_question_md: pick(it, 'probe_question_md', 'probe_md') ?? null,
    requires_intermediate: Boolean(pick(it, 'requires_intermediate')),
  }));
}

async function loadDrill(btn) {
  const dateInput = $('#drill-date');
  if (dateInput && dateInput.value) state.drill.date = dateInput.value;
  await runTask(btn || null, async () => {
    const raw = await apiFetch(`${API}/drills/daily?active_date=${encodeURIComponent(state.drill.date)}`);
    state.drill.items = normalizeDrillItems(raw);
    state.drill.outcomes = {};
    state.drill.loaded = true;
    renderDrill();
  });
}

function drillCardHtml(item, idx) {
  const uid = item.question_uid;
  const outcome = state.drill.outcomes[uid];
  const isEq = item.verification_mode === 'equation_skeleton';
  if (outcome) {
    const passed = outcome.passed;
    const retrain = outcome.retrain_stage;
    const retrainHint = retrain === 'STAGE_1_BLIND_PROBE'
      ? '中间子目标也未通过：重训将从「一阶条件遮罩盲探」重新开始。'
      : retrain === 'STAGE_4_DECRUTCH_VARIANT'
        ? '中间子目标已掌握、仅最终计算错误：重训直接从「四阶拆拐杖变式」开始。'
        : retrain ? `重训起点：${STAGE_LABELS[retrain] || retrain}` : '';
    const cascades = asArray(outcome.cascaded_question_uids);
    return `
    <div class="card drill-card ${passed ? 'passed' : 'failed'}">
      <div class="card-title"><span class="drill-no">盲测题 #${idx + 1}</span>
        ${passed ? '<span class="badge badge-green">✅ 通过</span>' : '<span class="badge badge-red">❌ 未通过</span>'}
      </div>
      <div class="outcome-box">
        <div class="big" aria-hidden="true">${passed ? '🎯' : '🧊'}</div>
        <h3>${passed ? '无提示一次做对，正式销账' : '盲测未通过，退回重训'}</h3>
        <p class="sub" style="color:var(--ink-soft);font-size:13.5px">
          ${passed
            ? `宏观状态 → ${esc(outcome.mastery_state || 'MASTERED')}（🟢），托管差额已解冻合入实战分，水分区收窄。`
            : `宏观状态 → ${esc(outcome.mastery_state || 'ACTIVE')}（🔴），托管差额已清空。${esc(retrainHint)}`}
        </p>
        ${cascades.length ? `
          <div class="cascade-list">
            <strong>🔗 上位母题连带销账 ${cascades.length} 道前置子题：</strong>
            <ul style="margin:6px 0 0;padding-left:18px">
              ${cascades.map((u) => `<li><span class="mono">${esc(shortUid(u))}…</span> → 🟢MASTERED（各自托管独立兑现）</li>`).join('')}
            </ul>
          </div>` : ''}
      </div>
      ${explainBarHtml(outcome.explanation_text)}
    </div>`;
  }
  return `
  <div class="card drill-card">
    <div class="card-title">
      <span class="drill-no">盲测题 #${idx + 1}</span>
      <span class="badge badge-gray">${isEq ? '🧮 核心列式核验' : '✍️ 最终答案核验'}</span>
      ${item.requires_intermediate ? '<span class="badge badge-amber">🔒 原题回退 · 中间量+最终答案双重核验</span>' : ''}
      <span class="hint">来源与知识点标签已剥离</span>
    </div>
    <div class="drill-stem rich">${renderRich(item.stem_md)}</div>
    <div class="drill-inputs">
      ${item.requires_intermediate ? `
        <div>
          <div class="probe-box" style="margin-bottom:8px">
            <div class="probe-label">曾卡壳的中间子目标（必答）</div>
            <div class="rich">${renderRich(item.probe_question_md || '（中间子目标描述缺失）')}</div>
          </div>
          <label class="field-label" for="drill-mid-${esc(uid)}">中间子目标表达式</label>
          <textarea id="drill-mid-${esc(uid)}" class="mono-input" rows="2" data-eq="1"
                    placeholder="输入曾卡壳步骤的中间关系式（SymPy / LaTeX）"></textarea>
        </div>` : ''}
      <div>
        <label class="field-label" for="drill-fin-${esc(uid)}">${isEq ? '核心方程组（无需化简）' : '最终答案'}</label>
        ${isEq && item.allowed_symbols.length ? `
          <div class="symbol-row" role="group" aria-label="allowed_symbols 符号芯片">
            ${item.allowed_symbols.map((s) => `<button type="button" class="symbol-chip" data-action="insert-symbol" data-symbol="${esc(s)}" data-target="drill-fin-${esc(uid)}">${esc(s)}</button>`).join('')}
          </div>` : ''}
        ${isEq
          ? `<textarea id="drill-fin-${esc(uid)}" class="mono-input" rows="3" data-eq="1"
               placeholder="多个方程用 ; 或换行分隔"></textarea>`
          : `<input type="text" id="drill-fin-${esc(uid)}" placeholder="输入最终答案（数值 / 表达式）" />`}
      </div>
    </div>
    <div class="btn-row" style="margin-top:14px">
      <button class="btn btn-primary" type="button" data-action="drill-submit" data-uid="${esc(uid)}">📤 提交盲测答案</button>
      ${item.requires_intermediate ? '<span class="field-help">两项均正确方可销账为 🟢MASTERED</span>' : ''}
    </div>
  </div>`;
}

function renderDrill() {
  const root = $('#drill-root');
  const { items, outcomes } = state.drill;
  if (!items.length) {
    root.innerHTML = `
      <div class="card empty-state">
        <div class="big" aria-hidden="true">🌙</div>
        <h3>${esc(state.drill.date)} 暂无到期盲测题</h3>
        <p>题目通过当场拆拐杖后进入 🟡PENDING_RETEST，需等待 2～4 天（粗心）或 3～7 天（思路断层）解锁窗口。可调整 active_date 时间旅行调试。</p>
        <button class="btn" type="button" data-action="goto-ingest">📥 去录入新错题</button>
      </div>`;
    return;
  }
  const answered = Object.keys(outcomes).length;
  const passed = Object.values(outcomes).filter((o) => o.passed).length;
  root.innerHTML = `
    <div class="drill-summary">
      <span class="chip">📋 今日题单 ${items.length} / 3</span>
      ${answered ? `<span class="badge badge-green">✅ 通过 ${passed}</span><span class="badge badge-red">❌ 未过 ${answered - passed}</span>` : '<span class="badge badge-gray">尚未作答</span>'}
      <span class="badge badge-blue">📅 ${esc(state.drill.date)}</span>
    </div>
    <div class="stack">${items.map((it, i) => drillCardHtml(it, i)).join('')}</div>`;
}

async function submitDrill(btn, uid) {
  const item = state.drill.items.find((it) => it.question_uid === uid);
  if (!item) return;
  const finEl = document.getElementById(`drill-fin-${uid}`);
  const midEl = document.getElementById(`drill-mid-${uid}`);
  const finalAnswer = (finEl?.value || '').trim();
  const midAnswer = (midEl?.value || '').trim();
  if (!finalAnswer) { toast('请填写最终答案', 'warn'); finEl?.focus(); return; }
  if (item.requires_intermediate && !midAnswer) {
    toast('回退原题：必须同时填写曾卡壳的中间子目标与最终答案', 'warn');
    midEl?.focus();
    return;
  }
  await runTask(btn, async () => {
    const outcome = await apiFetch(`${API}/drills/daily`, {
      method: 'POST',
      json: {
        question_uid: uid,
        final_answer: finalAnswer,
        intermediate_sub_answer: item.requires_intermediate ? midAnswer : null,
        active_date: state.drill.date,
      },
    });
    state.drill.outcomes[uid] = outcome || { passed: false, mastery_state: 'ACTIVE' };
    renderDrill();
    toast(outcome && outcome.passed ? '🎯 盲测通过，销账兑现！' : '盲测未通过，已退回重训流', outcome && outcome.passed ? 'ok' : 'warn');
  });
}

/* ================================================================
 * ④ 知识网与 Elo 看板（CUJ 4）
 * ================================================================ */
function normState(s) {
  const v = String(s || '').toUpperCase();
  if (v === 'ACTIVE') return 'active';
  if (v === 'PENDING_RETEST' || v === 'PENDING') return 'pending';
  if (v === 'MASTERED') return 'mastered';
  return 'unknown';
}
const STATE_EMOJI = { active: '🔴', pending: '🟡', mastered: '🟢', unknown: '⚪' };

function normalizeGraph(raw) {
  const data = raw || {};
  const treeRaw = Array.isArray(data) ? data
    : asArray(pick(data, 'tree', 'node_tree', 'nodes', 'modules', 'l1_modules'));
  const tree = treeRaw.map((m) => ({
    l1: pick(m, 'l1_module', 'l1', 'module', 'name') || '未命名模块',
    state: normState(pick(m, 'mastery_state', 'state', 'status')),
    children: asArray(pick(m, 'children', 'l2_nodes', 'entities', 'l2', 'l2_groups')).map((e) => ({
      l2: pick(e, 'l2_entity', 'l2', 'name') || '未命名实体',
      state: normState(pick(e, 'mastery_state', 'state', 'status')),
      children: asArray(pick(e, 'children', 'l3_nodes', 'operators', 'l3', 'nodes')).map((n) => ({
        id: pick(n, 'node_id', 'id') || '',
        name: pick(n, 'l3_operator', 'l3', 'name', 'operator') || '未命名算子',
        state: normState(pick(n, 'mastery_state', 'state', 'status')),
        aliases: asArray(parseMaybeJson(pick(n, 'aliases', 'aliases_json'), [])),
      })),
    })),
  }));
  const nodeIndex = new Map();
  for (const m of tree) for (const e of m.children) for (const n of e.children) {
    nodeIndex.set(n.id, { name: n.name, l1: m.l1, l2: e.l2 });
  }
  const edges = asArray(pick(data, 'co_error_edges', 'edges', 'red_edges', 'concept_co_errors')).map((e) => ({
    u: pick(e, 'node_u_id', 'u', 'source') || '',
    v: pick(e, 'node_v_id', 'v', 'target') || '',
    l1u: pick(e, 'l1_module_u') || '',
    l1v: pick(e, 'l1_module_v') || '',
    active: Number(pick(e, 'active_error_count', 'active_count') ?? 0),
    historical: Number(pick(e, 'total_historical_count', 'historical_count', 'total_count') ?? 0),
  }));
  const metricsRaw = pick(data, 'metrics', 'module_metrics', 'graph_metrics');
  let metrics = [];
  if (Array.isArray(metricsRaw)) {
    metrics = metricsRaw.map((m) => ({
      l1: pick(m, 'l1_module', 'module', 'name') || '未知模块',
      mastery_rate: Number(pick(m, 'mastery_rate') ?? 0),
      convergence: Number(pick(m, 'convergence') ?? 0),
      active_nodes: Number(pick(m, 'active_nodes') ?? 0),
    }));
  } else if (metricsRaw && typeof metricsRaw === 'object') {
    metrics = Object.entries(metricsRaw).map(([l1, m]) => ({
      l1,
      mastery_rate: Number(pick(m, 'mastery_rate') ?? 0),
      convergence: Number(pick(m, 'convergence') ?? 0),
      active_nodes: Number(pick(m, 'active_nodes') ?? 0),
    }));
  }
  // PM 集成修复：API 将 metrics 内嵌于每模块（L1ModuleView.metrics），顶层无 metrics 时从模块收集
  if (!metrics.length) {
    metrics = treeRaw.filter((m) => m && m.metrics).map((m) => ({
      l1: pick(m, 'l1_module', 'l1', 'module', 'name') || '未知模块',
      mastery_rate: Number(pick(m.metrics, 'mastery_rate') ?? 0),
      convergence: Number(pick(m.metrics, 'convergence') ?? 0),
      active_nodes: Number(pick(m.metrics, 'active_nodes') ?? 0),
    }));
  }
  // 批次：兼容「批次对象列表」与「扁平审计账本行」两种信封
  const batchRaw = asArray(pick(data, 'compaction_batches', 'batches', 'compaction_history', 'merge_history', 'compaction_audit_ledger'));
  const batchMap = new Map();
  for (const row of batchRaw) {
    const bid = pick(row, 'batch_id') || '';
    if (!bid) continue;
    if (!batchMap.has(bid)) {
      batchMap.set(bid, {
        batch_id: bid,
        l1: pick(row, 'l1_module') || '',
        l2: pick(row, 'l2_entity') || '',
        created_at: pick(row, 'created_at') || '',
        is_reverted: false,
        merges: [],
        _flags: [],
      });
    }
    const b = batchMap.get(bid);
    b._flags.push(Boolean(pick(row, 'is_reverted')));
    if (Array.isArray(row.merges)) {
      for (const mg of row.merges) {
        b.merges.push({ from: pick(mg, 'from_node_id', 'from') || '', to: pick(mg, 'to_node_id', 'to') || '', evidence: pick(mg, 'ai_judge_evidence', 'evidence') || '' });
      }
      if (row.is_reverted !== undefined) b.is_reverted = Boolean(row.is_reverted);
    } else if (pick(row, 'from_node_id')) {
      b.merges.push({ from: pick(row, 'from_node_id') || '', to: pick(row, 'to_node_id') || '', evidence: pick(row, 'ai_judge_evidence') || '' });
    }
    if (pick(row, 'created_at') && !b.created_at) b.created_at = pick(row, 'created_at');
  }
  const batches = Array.from(batchMap.values()).map((b) => {
    b.is_reverted = b.is_reverted || (b._flags.length > 0 && b._flags.every(Boolean));
    delete b._flags;
    return b;
  });
  return { tree, nodeIndex, edges, metrics, batches };
}

function nodeName(g, id) {
  const hit = g.nodeIndex.get(id);
  return hit ? hit.name : `节点 ${shortUid(id)}`;
}

function gaugeSvg(pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const r = 34;
  const len = Math.PI * r;
  const color = p >= 60 ? 'var(--green)' : p >= 30 ? 'var(--amber)' : 'var(--red)';
  return `
  <svg class="gauge" viewBox="0 0 92 58" role="img" aria-label="收敛度 ${p.toFixed(1)}%">
    <path d="M 12 50 A ${r} ${r} 0 0 1 80 50" fill="none" stroke="var(--surface-3)" stroke-width="9" stroke-linecap="round"/>
    <path d="M 12 50 A ${r} ${r} 0 0 1 80 50" fill="none" stroke="${color}" stroke-width="9" stroke-linecap="round"
          stroke-dasharray="${(len * p / 100).toFixed(2)} ${len.toFixed(2)}"/>
    <text x="46" y="46" text-anchor="middle" font-size="14" font-weight="800" fill="var(--ink)">${p.toFixed(1)}%</text>
    <text x="46" y="57" text-anchor="middle" font-size="7.5" fill="var(--ink-faint)">收敛度</text>
  </svg>`;
}

function renderGraph() {
  const root = $('#graph-root');
  const g = state.graph;
  if (!g) { root.innerHTML = ''; return; }
  const treeHtml = g.tree.length ? g.tree.map((m, mi) => {
    const l3all = m.children.flatMap((e) => e.children);
    const mastered = l3all.filter((n) => n.state === 'mastered').length;
    return `
    <details class="module-block" ${mi < 2 ? 'open' : ''}>
      <summary>
        <span class="caret" aria-hidden="true">▶</span>
        <span class="state-dot ${m.state !== 'unknown' ? m.state : (l3all.some((n) => n.state === 'active') ? 'active' : l3all.some((n) => n.state === 'pending') ? 'pending' : 'mastered')}"></span>
        ${esc(m.l1)}
        <span class="badge badge-gray" style="margin-left:auto">L2 ×${m.children.length} · L3 ×${l3all.length} · 🟢${mastered}</span>
      </summary>
      <div class="module-body">
        ${m.children.length ? m.children.map((e) => `
          <div class="l2-group">
            <div class="l2-name"><span class="state-dot ${e.state}"></span>📐 ${esc(e.l2)}</div>
            <div class="l3-list">
              ${e.children.length ? e.children.map((n) => `
                <span class="l3-node" title="${esc(n.aliases.length ? `别名：${n.aliases.join('、')}` : n.name)}">
                  <span class="state-dot ${n.state}" aria-label="${STATE_EMOJI[n.state]}"></span>
                  ${esc(n.name)}
                  ${n.aliases.length ? `<span class="alias-count">+${n.aliases.length} 别名</span>` : ''}
                </span>`).join('') : '<span class="field-help">（暂无活跃 L3 节点）</span>'}
            </div>
          </div>`).join('') : '<div class="field-help">（该模块暂无 L2 实体）</div>'}
      </div>
    </details>`;
  }).join('') : '<div class="empty-state"><div class="big">🌱</div><h3>知识网尚未生长</h3><p>录入第一道错题后，L1/L2/L3 三层路径将在这里自发汇聚。</p></div>';

  const metricsHtml = g.metrics.length ? g.metrics.map((m) => `
    <div class="metric-row">
      <div>
        <div class="metric-name">${esc(m.l1)}</div>
        <div class="progress-track"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, m.mastery_rate))}%"></div></div>
        <div class="progress-label">版图攻克率 ${fmtNum(m.mastery_rate)}% · 活跃主节点 ${m.active_nodes}</div>
      </div>
      ${gaugeSvg(m.convergence)}
    </div>`).join('') : '<div class="field-help">暂无模块指标（冷启动）</div>';

  const edgesSorted = [...g.edges].sort((a, b) => b.active - a.active || b.historical - a.historical);
  const edgesHtml = edgesSorted.length ? edgesSorted.map((e) => `
    <div class="edge-item ${e.active > 0 ? '' : 'historical'}">
      <span class="edge-node">${esc(nodeName(g, e.u))}</span>
      <span class="edge-link" aria-hidden="true">⚡</span>
      <span class="edge-node">${esc(nodeName(g, e.v))}</span>
      <span class="edge-mod">${esc(e.l1u)}${e.l1v && e.l1v !== e.l1u ? ` ↔ ${esc(e.l1v)}` : ''}</span>
      <span style="margin-left:auto">
        ${e.active > 0 ? `<span class="badge badge-red">活跃 ×${e.active}</span>` : '<span class="badge badge-gray">已销账</span>'}
        <span class="badge badge-gray">历史 ×${e.historical}</span>
      </span>
    </div>`).join('') : '<div class="field-help">暂无跨考点组合错题标红边</div>';

  const batchesHtml = g.batches.length ? g.batches.map((b) => `
    <div class="batch-item ${b.is_reverted ? 'reverted' : ''}">
      <div class="batch-head">
        <span class="mono">${esc(shortUid(b.batch_id))}…</span>
        <span class="chip" style="font-size:11px">${esc(b.l1)} / ${esc(b.l2)}</span>
        <span class="badge badge-gray">合并 ×${b.merges.length}</span>
        ${b.is_reverted ? '<span class="badge badge-gray">↩️ 已回滚</span>' : '<span class="badge badge-green">生效中</span>'}
        <span class="spacer"></span>
        <button class="btn btn-sm btn-danger" type="button" data-action="unmerge" data-batch="${esc(b.batch_id)}"
                ${b.is_reverted ? 'disabled' : ''}>一键解绑回滚 (Un-merge)</button>
      </div>
      <div class="batch-time">${esc(fmtTime(b.created_at))}</div>
      ${b.merges.length ? `<ul class="batch-merges">${b.merges.map((mg) => `<li title="${esc(mg.evidence)}">${esc(nodeName(g, mg.from))} → ${esc(nodeName(g, mg.to))}</li>`).join('')}</ul>` : ''}
    </div>`).join('') : '<div class="field-help">暂无 Compaction 合并批次（同父 L2 新增满 15 个 L3 节点后自动触发）</div>';

  root.innerHTML = `
  <div class="graph-grid">
    <div class="stack">
      <div class="card">
        <div class="card-title">🌳 三层知识网 <span class="hint">L1 模块 → L2 实体定理 → L3 原子算子（resolve 后活跃主节点）</span></div>
        ${treeHtml}
      </div>
      <div class="card">
        <div class="card-title">📊 模块双指标 <span class="hint">版图攻克率 + 分布熵校准收敛度</span></div>
        ${metricsHtml}
      </div>
    </div>
    <div class="stack">
      <div class="card">
        <div class="card-title">⚡ 跨考点标红边 <span class="hint">active / historical</span></div>
        <div class="edge-list">${edgesHtml}</div>
      </div>
      <div class="card">
        <div class="card-title">🧬 合并历史（Compaction 审计） <span class="hint">同域并查集 · 可逆</span></div>
        ${batchesHtml}
      </div>
    </div>
  </div>`;
}

async function loadGraphAndElo(btn) {
  await runTask(btn || null, async () => {
    const [graphRaw, eloRaw] = await Promise.all([
      apiFetch(`${API}/knowledge-graph`),
      apiFetch(`${API}/elo/ledger${state.eloModule ? `?l1_module=${encodeURIComponent(state.eloModule)}` : ''}`),
    ]);
    state.graph = normalizeGraph(graphRaw);
    state.elo = normalizeElo(eloRaw);
    if (!state.eloModule && state.elo.modules.length) {
      state.eloModuleOptions = state.elo.modules.map((m) => m.l1);
    }
    state.graphLoaded = true;
    renderGraph();
    renderElo();
  });
}

async function unmergeBatch(btn, batchId) {
  const ok = window.confirm(
    `确认解绑回滚批次 ${shortUid(batchId)}…？\n\n将按 compaction_audit_ledger 快照解除 redirect_to_id 两跳指针、恢复别名与标红边（Elo 账本正交解耦，无需重算）。`
  );
  if (!ok) return;
  await runTask(btn, async () => {
    await apiFetch(`${API}/knowledge-graph/compaction/${encodeURIComponent(batchId)}/revert`, { method: 'POST' });
    toast('↩️ Un-merge 完成：已精确恢复至合并前快照', 'ok');
    const graphRaw = await apiFetch(`${API}/knowledge-graph`);
    state.graph = normalizeGraph(graphRaw);
    renderGraph();
  });
}

/* ---------------- 双轨 Elo：SVG 曲线 / 托管池 / 解释流水 ---------------- */
function normalizeElo(raw) {
  const data = raw || {};
  let modulesRaw;
  if (Array.isArray(data)) modulesRaw = data;
  else if (Array.isArray(data.modules)) modulesRaw = data.modules;
  else if (data.modules && typeof data.modules === 'object') {
    modulesRaw = Object.entries(data.modules).map(([l1, m]) => ({ l1_module: l1, ...m }));
  } else modulesRaw = asArray(pick(data, 'ledgers', 'data'));
  const modules = modulesRaw.map((m) => {
    const current = pick(m, 'current', 'state', 'elo_state') || m;
    const events = asArray(pick(m, 'events', 'ledger', 'event_log', 'mutations')).map((e) => ({
      event_id: Number(pick(e, 'event_id', 'id') ?? 0),
      trigger: pick(e, 'mutation_trigger', 'trigger') || '',
      question_uid: pick(e, 'question_uid') || '',
      difficulty: pick(e, 'eval_difficulty_elo', 'difficulty'),
      gamma: pick(e, 'watermark_gamma', 'gamma'),
      covBefore: Number(pick(e, 'coverage_elo_before') ?? NaN),
      covAfter: Number(pick(e, 'coverage_elo_after') ?? NaN),
      combatBefore: Number(pick(e, 'combat_elo_before') ?? NaN),
      combatAfter: Number(pick(e, 'combat_elo_after') ?? NaN),
      explanation: pick(e, 'explanation_text', 'explanation') || '',
      created_at: pick(e, 'created_at') || '',
    })).sort((a, b) => a.event_id - b.event_id);
    const escrow = asArray(pick(m, 'escrow', 'escrow_pool', 'pending_escrow', 'pending_escrows', 'escrow_items')).map((p) => ({
      question_uid: pick(p, 'question_uid') || '',
      // PM 集成修复：EscrowPendingView 仅返回 FROZEN_PENDING_BLIND 池，无状态字段时按语义缺省
      status: pick(p, 'escrow_status', 'status') || 'FROZEN_PENDING_BLIND',
      delta: Number(pick(p, 'escrow_combat_delta') ?? 0),
      decay: Number(pick(p, 'decay_factor') ?? 1),
      floor: Number(pick(p, 'floor_elo_applied') ?? 0),
      snapshot: pick(p, 'initial_combat_elo_snapshot'),
      covSettled: Boolean(pick(p, 'is_cov_settled') ?? true),
      updated_at: pick(p, 'updated_at') || '',
    }));
    let curve = asArray(pick(m, 'curve', 'curve_points', 'history', 'points')).map((p) => ({
      event_id: Number(pick(p, 'event_id', 'id') ?? 0),
      cov: Number(pick(p, 'cov', 'coverage_elo', 'coverage') ?? NaN),
      combat: Number(pick(p, 'combat', 'combat_elo') ?? NaN),
    })).filter((p) => Number.isFinite(p.cov) && Number.isFinite(p.combat))
      .sort((a, b) => a.event_id - b.event_id);
    if (!curve.length && events.length) {
      curve = events
        .filter((e) => Number.isFinite(e.covAfter) && Number.isFinite(e.combatAfter))
        .map((e) => ({ event_id: e.event_id, cov: e.covAfter, combat: e.combatAfter }));
    }
    return {
      l1: pick(m, 'l1_module', 'module', 'name') || '未知模块',
      cov: Number(pick(current, 'coverage_elo', 'cov') ?? pick(m, 'coverage_elo') ?? 1200),
      combat: Number(pick(current, 'combat_elo', 'combat') ?? pick(m, 'combat_elo') ?? 1200),
      events, escrow, curve,
    };
  });
  return { modules };
}

/** 原生 SVG 双轨折线：覆盖分（电光蓝）/ 实战分（酸绿）+ 水分区（琥珀着色） */
function eloChartSvg(mod) {
  const pts = [...mod.curve];
  if (!pts.length || pts[0].event_id !== 0) {
    pts.unshift({ event_id: 0, cov: 1200, combat: 1200 });
  }
  const W = 640; const H = 250;
  const padL = 50; const padR = 84; const padT = 18; const padB = 30;
  const iw = W - padL - padR; const ih = H - padT - padB;
  const all = pts.flatMap((p) => [p.cov, p.combat]);
  let yMin = Math.min(...all); let yMax = Math.max(...all);
  if (yMax - yMin < 60) { const mid = (yMax + yMin) / 2; yMin = mid - 30; yMax = mid + 30; }
  yMin = Math.floor((yMin - 25) / 50) * 50;
  yMax = Math.ceil((yMax + 25) / 50) * 50;
  const x = (i) => padL + (pts.length === 1 ? iw / 2 : (i / (pts.length - 1)) * iw);
  const y = (v) => padT + ih - ((v - yMin) / (yMax - yMin)) * ih;
  const line = (key) => pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
  // 水分区多边形（cov 上沿 + combat 下沿反向闭合）
  const waterPath = pts.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.cov).toFixed(1)}`).join(' ')
    + ' ' + [...pts].map((p, i) => ({ p, i })).reverse().map(({ p, i }) => `L${x(i).toFixed(1)},${y(p.combat).toFixed(1)}`).join(' ') + ' Z';
  const ticks = [];
  const step = Math.max(50, Math.round((yMax - yMin) / 4 / 50) * 50);
  for (let v = yMin; v <= yMax; v += step) ticks.push(v);
  const last = pts[pts.length - 1];
  const lastX = x(pts.length - 1);
  let covLabelY = y(last.cov);
  let combatLabelY = y(last.combat);
  if (Math.abs(covLabelY - combatLabelY) < 15) { covLabelY -= 8; combatLabelY += 12; }
  const dots = pts.map((p, i) => `
    <circle cx="${x(i).toFixed(1)}" cy="${y(p.cov).toFixed(1)}" r="3" fill="var(--electric)"><title>事件 #${p.event_id} · 覆盖 ${fmtNum(p.cov, 0)}</title></circle>
    <circle cx="${x(i).toFixed(1)}" cy="${y(p.combat).toFixed(1)}" r="3" fill="var(--green)"><title>事件 #${p.event_id} · 实战 ${fmtNum(p.combat, 0)}</title></circle>`).join('');
  return `
  <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(mod.l1)} 双轨 Elo 历史曲线：覆盖分 ${fmtNum(mod.cov, 0)}，实战分 ${fmtNum(mod.combat, 0)}">
    ${ticks.map((v) => `
      <line x1="${padL}" y1="${y(v).toFixed(1)}" x2="${W - padR}" y2="${y(v).toFixed(1)}" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 4"/>
      <text x="${padL - 7}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--ink-faint)" style="font-family:var(--font-mono)">${v}</text>`).join('')}
    ${yMin <= 1200 && yMax >= 1200 ? `<line x1="${padL}" y1="${y(1200).toFixed(1)}" x2="${W - padR}" y2="${y(1200).toFixed(1)}" stroke="var(--ink-faint)" stroke-width="1" stroke-dasharray="6 4" opacity="0.55"/>` : ''}
    <path d="${waterPath}" fill="var(--amber)" opacity="0.16" stroke="none"/>
    <path d="${line('cov')}" fill="none" stroke="var(--electric)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>
    <path d="${line('combat')}" fill="none" stroke="var(--green)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}
    <text x="${(lastX + 8).toFixed(1)}" y="${(covLabelY + 3).toFixed(1)}" font-size="11" font-weight="800" fill="var(--electric)" style="font-family:var(--font-mono)">覆盖 ${fmtNum(last.cov, 0)}</text>
    <text x="${(lastX + 8).toFixed(1)}" y="${(combatLabelY + 3).toFixed(1)}" font-size="11" font-weight="800" fill="var(--green)" style="font-family:var(--font-mono)">实战 ${fmtNum(last.combat, 0)}</text>
    <text x="${padL}" y="${H - 8}" font-size="10" fill="var(--ink-faint)" style="font-family:var(--font-mono)">#${pts[0].event_id}</text>
    <text x="${W - padR}" y="${H - 8}" text-anchor="end" font-size="10" fill="var(--ink-faint)" style="font-family:var(--font-mono)">#${last.event_id}（event_id）</text>
  </svg>`;
}

function renderElo() {
  const root = $('#elo-root');
  const elo = state.elo;
  if (!elo) { root.innerHTML = ''; return; }
  const options = state.eloModuleOptions.length ? state.eloModuleOptions : elo.modules.map((m) => m.l1);
  const selector = `
    <div class="btn-row" style="margin-bottom:12px">
      <label class="field-label" for="elo-module-select" style="margin:0">模块过滤</label>
      <select id="elo-module-select" style="width:auto;min-width:180px">
        <option value="" ${state.eloModule === '' ? 'selected' : ''}>全部模块</option>
        ${options.map((m) => `<option value="${esc(m)}" ${state.eloModule === m ? 'selected' : ''}>${esc(m)}</option>`).join('')}
      </select>
      <span class="field-help">GET /elo/ledger${state.eloModule ? `?l1_module=${esc(state.eloModule)}` : ''}</span>
    </div>`;
  const cards = elo.modules.length ? elo.modules.map((mod) => {
    const water = Math.max(0, mod.cov - mod.combat);
    const escrowRows = mod.escrow.length ? mod.escrow.map((p) => {
      const meta = ESCROW_META[p.status] || ['badge-gray', p.status || '未知'];
      return `<tr>
        <td class="num">${esc(shortUid(p.question_uid))}…</td>
        <td><span class="badge ${meta[0]}">${esc(meta[1])}</span></td>
        <td class="num">+${fmtNum(p.delta)}</td>
        <td class="num">${fmtNum(p.decay, 2)}</td>
        <td class="num" style="color:var(--warn);font-weight:800">+${fmtNum(p.delta * p.decay)}</td>
        <td class="num">+${fmtNum(p.floor)}</td>
        <td class="num">${esc(fmtTime(p.updated_at))}</td>
      </tr>`;
    }).join('') : '<tr><td colspan="7" style="color:var(--ink-faint)">暂无托管记录</td></tr>';
    const eventsHtml = mod.events.length ? [...mod.events].reverse().map((e) => {
      const meta = TRIGGER_META[e.trigger] || ['badge-gray', e.trigger || '事件'];
      const dCombat = Number.isFinite(e.combatAfter) && Number.isFinite(e.combatBefore) ? e.combatAfter - e.combatBefore : null;
      const dCov = Number.isFinite(e.covAfter) && Number.isFinite(e.covBefore) ? e.covAfter - e.covBefore : null;
      return `
      <div class="ledger-event">
        <div class="ledger-head">
          <span class="badge ${meta[0]}">${esc(meta[1])}</span>
          <span class="mono" style="font-size:10.5px">#${e.event_id}</span>
          ${e.difficulty != null ? `<span class="chip" style="font-size:10.5px">D=${esc(e.difficulty)}</span>` : ''}
          ${e.gamma != null && Number(e.gamma) < 1 ? `<span class="badge badge-gray">γ=${fmtNum(e.gamma, 2)}</span>` : ''}
          ${dCombat != null ? `<span class="delta ${dCombat > 0.005 ? 'up' : 'flat'}">实战 ${dCombat >= 0 ? '+' : ''}${fmtNum(dCombat)}</span>` : ''}
          ${dCov != null && Math.abs(dCov) > 0.005 ? `<span class="delta up" style="color:var(--electric)">覆盖 +${fmtNum(dCov)}</span>` : ''}
          <span class="ledger-time">${esc(fmtTime(e.created_at))}</span>
        </div>
        <div class="ledger-explain rich">${renderRich(e.explanation)}</div>
      </div>`;
    }).join('') : '<div class="field-help">暂无账本事件（初始水位 1200 / 1200）</div>';
    return `
    <div class="card elo-chart-card">
      <div class="card-title">📈 ${esc(mod.l1)} · 双轨 Elo
        <span class="hint">覆盖分 = 借助脚手架上限 · 实战分 = 盲测兑现水位</span>
      </div>
      <div class="elo-now">
        <div><div class="lbl">覆盖分 COVERAGE</div><div class="big-num" style="color:var(--electric)">${fmtNum(mod.cov, 0)}</div></div>
        <div><div class="lbl">实战分 COMBAT</div><div class="big-num" style="color:var(--green)">${fmtNum(mod.combat, 0)}</div></div>
        <div><div class="lbl">水分区带宽</div><div class="big-num" style="color:var(--amber)">Δ${fmtNum(water, 0)}</div></div>
      </div>
      <div class="chart-legend">
        <span><span class="swatch" style="background:var(--electric)"></span>覆盖分</span>
        <span><span class="swatch" style="background:var(--green)"></span>实战分</span>
        <span><span class="swatch" style="background:var(--amber);opacity:.45"></span>水分区（待盲测兑现）</span>
      </div>
      ${eloChartSvg(mod)}
      <h3 style="margin-top:16px">🏦 待兑现托管池明细</h3>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>题目</th><th>托管状态</th><th>冻结差额</th><th>衰减系数</th><th>待兑现</th><th>已发保底</th><th>更新时间</th></tr></thead>
          <tbody>${escrowRows}</tbody>
        </table>
      </div>
      <h3 style="margin-top:16px">🧾 算分解释流水 <span class="hint" style="font-weight:400;font-size:12px">事件溯源 · 不可变账本</span></h3>
      <div class="ledger-list">${eventsHtml}</div>
    </div>`;
  }).join('') : '<div class="card empty-state"><div class="big">📉</div><h3>暂无 Elo 账本</h3><p>录入题目并产生结算事件后，这里会出现分模块双轨曲线。</p></div>';
  root.innerHTML = `${selector}<div class="stack">${cards}</div>`;
}

/* ================================================================
 * 事件接线：统一 delegation（重渲染不丢监听）
 * ================================================================ */
function insertSymbol(chip) {
  const target = document.getElementById(chip.dataset.target || '');
  if (!target) return;
  const sym = chip.dataset.symbol || '';
  const start = target.selectionStart ?? target.value.length;
  const end = target.selectionEnd ?? target.value.length;
  target.value = target.value.slice(0, start) + sym + target.value.slice(end);
  target.selectionStart = target.selectionEnd = start + sym.length;
  target.focus();
  target.dispatchEvent(new Event('input', { bubbles: true }));
}

function openLightbox(src) {
  $('#lightbox-img').src = src;
  $('#lightbox').hidden = false;
}
function closeLightbox() {
  const lb = $('#lightbox');
  if (!lb.hidden) { lb.hidden = true; $('#lightbox-img').src = ''; }
}

function onDocumentClick(e) {
  const img = e.target.closest('.rich img, .thumb img');
  if (img && img.src) { openLightbox(img.src); return; }
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  switch (action) {
    case 'toggle-theme':
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
      break;
    case 'retry-last': {
      const fn = state.lastRetry;
      hideRetryBar();
      if (fn) fn();
      break;
    }
    case 'retry-dismiss': hideRetryBar(); break;
    case 'close-lightbox': closeLightbox(); break;
    case 'goto-drill': switchTab('drill'); break;
    case 'goto-ingest': switchTab('ingest'); break;
    case 'goto-training': switchTab('training'); break;
    case 'toggle-preview': togglePreview(); break;
    case 'set-initial-state': {
      state.initialState = el.dataset.value;
      $$('#initial-state-seg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.value === state.initialState)));
      break;
    }
    case 'thumb-remove': removeSlotFile(el.dataset.slot, el.dataset.id); break;
    case 'thumb-zoom': openLightbox(el.dataset.url); break;
    case 'gate-pick-step': {
      if (state.gateConfirmed) break;
      state.gateSel = Number(el.dataset.step) || 1;
      renderGate();
      break;
    }
    case 'set-category': {
      if (state.gateConfirmed) break;
      state.gateCategory = el.dataset.value;
      renderGate();
      toast(`归因已切换为 ${CATEGORY_LABELS[state.gateCategory] || state.gateCategory}`, 'info', 2000);
      break;
    }
    case 'confirm-gate': confirmGate(el); break;
    case 'resume-training': {
      if (!state.session) {
        const persisted = loadPersistedSession();
        if (persisted) { state.session = persisted.resp; state.gateParams = persisted.gateParams || null; }
      }
      switchTab('training');
      break;
    }
    case 'discard-session': {
      state.session = null;
      state.gateParams = null;
      persistSession();
      renderResumeBanner();
      renderTraining();
      toast('已丢弃暂存进度', 'info');
      break;
    }
    case 'stash-progress': {
      persistSession();
      renderResumeBanner();
      switchTab('ingest');
      toast('⏸️ 进度已暂存：会话保留，可随时断点续传', 'ok');
      break;
    }
    case 'stage-submit': submitStage(el, null); break;
    case 'stage-noidea': submitStage(el, 'NO_IDEA'); break;
    case 'cooldown-resume': submitStage(el, 'RESUME'); break;
    case 'fallback-skip': submitStage(el, 'SKIP'); break;
    case 'fallback-swap': swapFallbackQuestion(el); break;
    case 'insert-symbol': insertSymbol(el); break;
    case 'drill-refresh': loadDrill(el); break;
    case 'drill-submit': submitDrill(el, el.dataset.uid); break;
    case 'graph-refresh': loadGraphAndElo(el); break;
    case 'unmerge': unmergeBatch(el, el.dataset.batch); break;
    default: break;
  }
}

function bindStaticEvents() {
  document.addEventListener('click', onDocumentClick);
  document.addEventListener('keydown', onGlobalKeydown);
  $$('.tab-btn').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));

  $('#ingest-form').addEventListener('submit', (e) => {
    e.preventDefault();
    submitIngest($('#ingest-submit'));
  });
  $('#stem-input').addEventListener('input', onStemInput);

  const dateInput = $('#drill-date');
  dateInput.value = state.drill.date;
  dateInput.addEventListener('change', () => {
    state.drill.date = dateInput.value || todayISO();
    state.drill.loaded = false;
    loadDrill();
  });

  // Elo 模块过滤（select 用 change 事件）
  document.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'elo-module-select') {
      state.eloModule = e.target.value || '';
      loadGraphAndElo();
    }
  });

  // 全局 paste 路由：焦点不在槽内/可编辑控件时，路由到最后聚焦的槽（严格不混槽）
  document.addEventListener('paste', (e) => {
    if (e.defaultPrevented) return;
    if (e.target.closest && e.target.closest('.drop-slot')) return;
    if (isEditable(e.target)) return;
    const hasImage = Array.from(e.clipboardData?.items || []).some((it) => it.kind === 'file' && /^image\//.test(it.type));
    if (!hasImage) return;
    if (state.lastSlot) {
      handleSlotPaste(state.lastSlot, e);
    } else {
      e.preventDefault();
      toast('请先点击「题干与配图槽」或「手写草稿截图槽」，再粘贴图片', 'warn');
    }
  });

  // 图片加载失败兜底（CAS 资产缺失时不出现破图）
  document.addEventListener('error', (e) => {
    const t = e.target;
    if (t && t.tagName === 'IMG' && !t.dataset.errHandled) {
      t.dataset.errHandled = '1';
      const span = document.createElement('span');
      span.className = 'img-missing';
      span.textContent = '🖼️ 配图缺失（CAS 资产未找到或已清理）';
      t.replaceWith(span);
    }
  }, true);
}

/* ---------------------------------------------------------------- 启动 */
async function loadMarked() {
  try {
    const mod = await import(MARKED_CDN);
    state.marked = mod.marked || mod.default || null;
  } catch {
    state.marked = null; // 离线降级：miniMarkdown
  }
}

function init() {
  initTheme();
  setupSlot('stem');
  setupSlot('draft');
  bindStaticEvents();
  renderResumeBanner();
  renderTraining();
  loadMarked().then(() => {
    // marked 就绪后刷新已渲染的富文本区域（当前可见视图）
    if (state.previewOn) {
      const v = $('#stem-input').value;
      if (v) mountRich($('#stem-preview'), v);
    }
    if (state.gate) renderGate();
    if (state.tab === 'training') renderTraining();
    if (state.tab === 'drill' && state.drill.loaded) renderDrill();
    if (state.tab === 'graph' && state.graphLoaded) { renderGraph(); renderElo(); }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}