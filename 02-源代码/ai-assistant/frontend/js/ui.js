/**
 * 轻量 UI 工具：转义 / Toast / 弹窗 / 表格 / 状态徽标。
 * 不引入任何框架，保证前端零构建、零依赖。
 */

/* ---------------- 转义 ---------------- */
const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

/** 把任意值渲染成可读文本（对象走 JSON） */
export function show(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  if (value === '') return '—';
  return String(value);
}

/** 取整字段的安全访问 */
export const pick = (obj, path, fallback = '—') => {
  const v = path.split('.').reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
  return v === null || v === undefined || v === '' ? fallback : v;
};

/* ---------------- 时间 ---------------- */
export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtClock(iso) {
  const d = iso ? new Date(iso) : new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function today() {
  return fmtDate(new Date().toISOString());
}

export function uid(prefix = 'k') {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/* ---------------- Toast ---------------- */
export function toast(message, kind = 'info', meta = '') {
  const stack = document.getElementById('toast-stack');
  if (!stack) return;
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.innerHTML = `<div>${esc(message)}</div>`
    + (meta ? `<span class="toast-meta">${esc(meta)}</span>` : '');
  stack.appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .2s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 220);
  }, kind === 'err' ? 5200 : 3200);
}

export const toastOk = (m, meta) => toast(m, 'ok', meta);
export const toastErr = (m, meta) => toast(m, 'err', meta);
export const toastWarn = (m, meta) => toast(m, 'warn', meta);

/** 统一处理 ApiError：401 提示重新登录，403 提示权限，其余显示原因 */
export function reportError(err, prefix = '') {
  const msg = (err && err.message) || '未知错误';
  const meta = err && err.traceId ? `trace=${err.traceId}` : '';
  toastErr(prefix ? `${prefix}：${msg}` : msg, meta);
}

/* ---------------- 弹窗 ---------------- */
let modalStack = [];

export function openModal({ title, body = '', footer = '', width, onMount }) {
  const root = document.getElementById('modal-root');
  root.hidden = false;
  root.innerHTML = `
    <div class="modal"${width ? ` style="max-width:${width}"` : ''}>
      <div class="modal-head">
        <h2>${esc(title)}</h2>
        <button class="btn btn-ghost btn-sm" data-modal-close>关闭</button>
      </div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;

  const closer = () => closeModal();
  root.querySelectorAll('[data-modal-close]').forEach((b) => b.addEventListener('click', closer));
  root.addEventListener('click', (e) => { if (e.target === root) closeModal(); });

  const handle = { root, close: closeModal, body: root.querySelector('.modal-body') };
  modalStack.push(handle);
  if (onMount) onMount(handle);
  return handle;
}

export function closeModal() {
  modalStack = [];
  const root = document.getElementById('modal-root');
  root.hidden = true;
  root.innerHTML = '';
}

export function confirmDialog({ title = '请确认', message, confirmText = '确认', danger = false }) {
  return new Promise((resolve) => {
    const h = openModal({
      title,
      width: '420px',
      body: `<p style="margin:0">${esc(message)}</p>`,
      footer: `<button class="btn" data-modal-close>取消</button>
               <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-ok>${esc(confirmText)}</button>`,
    });
    h.root.querySelector('[data-ok]').addEventListener('click', () => { closeModal(); resolve(true); });
    h.root.querySelectorAll('[data-modal-close]').forEach((b) =>
      b.addEventListener('click', () => resolve(false)));
  });
}

/* ---------------- 徽标 ---------------- */
const STATUS_KIND = {
  // 客户
  NEW: 'badge-info', FOLLOWING: 'badge-warn', SIGNED: 'badge-ok', LOST: 'badge-neutral',
  // 申请 / 工单 / 预警
  PENDING: 'badge-warn', APPROVED: 'badge-ok', REJECTED: 'badge-bad',
  OPEN: 'badge-warn', PROCESSING: 'badge-info', RESOLVED: 'badge-ok', CLOSED: 'badge-neutral',
  // 活动 / 报告
  DONE: 'badge-ok', RUNNING: 'badge-info', FAILED: 'badge-bad',
  ENROLLED: 'badge-ok', CANCELLED: 'badge-neutral',
  // 风险等级
  LOW: 'badge-neutral', MEDIUM: 'badge-warn', HIGH: 'badge-bad', CRITICAL: 'badge-bad',
  // 文档
  INDEXED: 'badge-ok', DRAFT: 'badge-neutral',
  // 角色（借 role 字段渲染审计日志 / 顶栏）
  visitor: 'badge-neutral', student: 'badge-info', employee: 'badge-info',
  manager: 'badge-warn', admin: 'badge-purple',
};

const STATUS_TEXT = {
  NEW: '新线索', FOLLOWING: '跟进中', SIGNED: '已签约', LOST: '已流失',
  PENDING: '待审批', APPROVED: '已通过', REJECTED: '已驳回',
  OPEN: '待处理', PROCESSING: '处理中', RESOLVED: '已解决', CLOSED: '已关闭',
  DONE: '已完成', RUNNING: '生成中', FAILED: '失败',
  ENROLLED: '已报名', CANCELLED: '已取消',
  LOW: '低', MEDIUM: '中', HIGH: '高', CRITICAL: '紧急',
  INDEXED: '已索引', DRAFT: '草稿',
  PREPARING: '材料准备', APPLYING: '申请中', OFFERED: '已获录取', VISA: '签证阶段', ENROLLED_STAGE: '已入读',
  LEAVE: '请假', EXAM: '考务', OTHER: '其他',
  TEXT: '文本', PDF: 'PDF', EXCEL: 'Excel',
};

/**
 * 渲染状态徽标。
 * 有些状态值在不同业务域含义不同（例如 OPEN 在工单是「待处理」、在活动是「报名中」），
 * 这种时候用第二个参数显式覆盖文案。
 */
export function badge(value, label) {
  if (value === null || value === undefined || value === '') return '<span class="badge badge-neutral">—</span>';
  const kind = STATUS_KIND[value] || 'badge-neutral';
  const text = label || STATUS_TEXT[value] || value;
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

export function statusText(value) {
  return STATUS_TEXT[value] || value || '—';
}

/* ---------------- 通用片段 ---------------- */
export function statCard(label, value, hint = '') {
  return `<div class="stat">
    <div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${esc(value)}</div>
    ${hint ? `<div class="stat-label" style="margin-top:4px">${esc(hint)}</div>` : ''}
  </div>`;
}

export function card(title, body, { sub = '', actions = '', tight = false } = {}) {
  return `<div class="card">
    <div class="card-head">
      <div><h2>${esc(title)}</h2>${sub ? `<div class="card-sub">${esc(sub)}</div>` : ''}</div>
      <div>${actions}</div>
    </div>
    <div class="card-body${tight ? ' tight' : ''}">${body}</div>
  </div>`;
}

export function table(columns, rows, { empty = '暂无数据' } = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const head = columns.map((c) => `<th>${esc(c.title)}</th>`).join('');
  const body = rows.map((row) => `<tr>${
    columns.map((c) => {
      const v = typeof c.render === 'function' ? c.render(row) : show(row[c.key]);
      return `<td class="${c.cls || ''}">${v}</td>`;
    }).join('')
  }</tr>`).join('');
  return `<div class="table-wrap"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function loading(text = '加载中…') {
  return `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;
}

export function emptyBox(text) {
  return `<div class="empty">${esc(text)}</div>`;
}

export function kvList(pairs) {
  const items = pairs
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `<li><span class="k">${esc(k)}</span><span class="v">${esc(show(v))}</span></li>`)
    .join('');
  return `<ul class="kv">${items}</ul>`;
}

export function jsonBlock(obj) {
  return `<pre class="code">${esc(typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2))}</pre>`;
}

/** 简易柱状图（纯 CSS，避免引入图表库） */
export function barChart(series, { height = 120 } = {}) {
  if (!series || !series.length) return emptyBox('暂无数据');
  const max = Math.max(...series.map((s) => s.value), 1);
  const bars = series.map((s) => {
    const h = Math.max(2, Math.round((s.value / max) * height));
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:5px">
      <div style="font-size:11px;color:var(--text-muted)">${esc(s.value)}</div>
      <div style="width:100%;max-width:38px;height:${h}px;border-radius:5px 5px 0 0;background:var(--primary);opacity:.85"></div>
      <div style="font-size:11px;color:var(--text-muted);white-space:nowrap">${esc(s.label)}</div>
    </div>`;
  }).join('');
  return `<div style="display:flex;align-items:flex-end;gap:8px;height:${height + 46}px">${bars}</div>`;
}

/** 表单值收集：<form> → 普通对象（空字符串转 null） */
export function formValues(formEl) {
  const out = {};
  new FormData(formEl).forEach((v, k) => {
    const val = typeof v === 'string' ? v.trim() : v;
    out[k] = val === '' ? null : val;
  });
  return out;
}

export function toInt(v, fallback = null) {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

export function toFloat(v, fallback = null) {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

/** 去掉对象里的 null/undefined/空串，避免后端 min_length 校验被空值打回 */
export function compact(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'string' && v === '') continue;
    out[k] = v;
  }
  return out;
}

export function debounce(fn, wait = 260) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
