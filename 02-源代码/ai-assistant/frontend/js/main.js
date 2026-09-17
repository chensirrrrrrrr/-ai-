/**
 * 应用入口：登录 / 导航 / hash 路由 / 主题 / 顶栏。
 */
import {
  DEFAULT_API_BASE, ROLE_LABELS, getApiBase, getToken, getUser, setApiBase,
  setToken, setUser, clearAuth, getSessionId,
} from './config.js';
import { api, ApiError } from './api.js';
import { esc, toastOk, toastErr } from './ui.js';

import chatView from './views/chat.js';
import crmView from './views/crm.js';
import studentView from './views/student.js';
import orgView from './views/org.js';
import opsView from './views/ops.js';
import dataView from './views/data.js';
import systemView from './views/system.js';

/* 路由表：顺序即导航顺序 */
const VIEWS = [chatView, crmView, studentView, orgView, opsView, dataView, systemView];

const DEMO_ACCOUNTS = [
  { username: 'admin', password: 'admin123', label: '系统管理员', role: 'admin' },
  { username: 'manager', password: 'manager123', label: '陈总（管理层）', role: 'manager' },
  { username: 'advisor', password: 'advisor123', label: '王敏（顾问）', role: 'employee' },
  { username: 'teacher', password: 'teacher123', label: '李强（带教老师）', role: 'employee' },
  { username: 'student', password: 'student123', label: '张三（学生）', role: 'student' },
];

const ICONS = {
  chat: '<path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.5A8 8 0 1 1 21 12z"/>',
  crm: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/>',
  student: '<path d="M22 10 12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5"/>',
  org: '<rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/><path d="M12 7v4M5 17v-3h14v3"/>',
  ops: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
  data: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  system: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 13.7H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 3v-.1a2 2 0 1 1 4 0V3a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
};

const state = {
  role: 'visitor',
  user: null,
  current: null,
  health: null,
};

/* ======================================================================== */
/* 主题                                                                      */
/* ======================================================================== */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('uas.theme', theme);
}

function initTheme() {
  const saved = localStorage.getItem('uas.theme');
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(saved || (prefersDark ? 'dark' : 'light'));
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(next);
}

/* ======================================================================== */
/* 访客落地页 / 登录屏 / 控制台 —— 三选一显示                                */
/* ======================================================================== */
const landingEl = document.getElementById('landing');
const loginScreen = document.getElementById('login-screen');
const consoleEl = document.getElementById('console');
const loginForm = document.getElementById('login-form');
const loginError = document.getElementById('login-error');

/** 默认入口：无需登录即可访问的落地页 */
function showLanding() {
  landingEl.hidden = false;
  loginScreen.hidden = true;
  consoleEl.hidden = true;
  window.scrollTo({ top: 0 });
}

function showLogin(message = '') {
  landingEl.hidden = true;
  consoleEl.hidden = true;
  loginScreen.hidden = false;
  loginError.hidden = !message;
  loginError.textContent = message;
  document.getElementById('login-api-base').value = localStorage.getItem('uas.api_base') || '';
}

function showConsole() {
  landingEl.hidden = true;
  loginScreen.hidden = true;
  consoleEl.hidden = false;
}

function renderDemoAccounts() {
  const box = document.getElementById('demo-accounts');
  box.innerHTML = DEMO_ACCOUNTS.map((a) => `
    <button type="button" class="demo-item" data-user="${esc(a.username)}"
            data-pass="${esc(a.password)}">
      <strong>${esc(a.username)}</strong>
      <small>${esc(a.label)} · ${esc(ROLE_LABELS[a.role] || a.role)}</small>
    </button>`).join('');
  box.querySelectorAll('[data-user]').forEach((b) =>
    b.addEventListener('click', () => {
      document.getElementById('login-username').value = b.dataset.user;
      document.getElementById('login-password').value = b.dataset.pass;
      document.getElementById('login-submit').focus();
    }));
}

async function doLogin(username, password) {
  const submit = document.getElementById('login-submit');
  submit.disabled = true;
  submit.textContent = '登录中…';
  try {
    const { data } = await api.login(username, password);
    setToken(data.access_token);
    setUser({ username, role: data.role, display_name: data.display_name });
    state.role = data.role;
    state.user = getUser();
    toastOk(`已登录：${data.display_name || username}（${ROLE_LABELS[data.role] || data.role}）`);
    await enterConsole();
  } catch (err) {
    showLogin(err.message || '登录失败');
  } finally {
    submit.disabled = false;
    submit.textContent = '登录';
  }
}

async function doVisitor() {
  try {
    const { data } = await api.visitor();
    setToken(data.access_token);
    setUser({ username: '访客', role: 'visitor', display_name: '访客' });
    state.role = 'visitor';
    state.user = getUser();
    toastOk('已以访客身份进入（仅可触达客服 Agent）');
    await enterConsole();
  } catch (err) {
    showLogin(err.message || '访客登录失败');
  }
}

function doLogout(message = '') {
  clearAuth();
  state.role = 'visitor';
  state.user = null;
  state.current = null;
  // 主动退出 → 回落地页；会话失效 → 回登录屏并说明原因
  if (message) showLogin(message); else showLanding();
  location.hash = '';
}

/* ======================================================================== */
/* 控制台                                                                    */
/* ======================================================================== */
function visibleViews() {
  return VIEWS.filter((v) => v.roles.includes(state.role));
}

function renderNav() {
  const nav = document.getElementById('nav');
  const items = visibleViews();
  nav.innerHTML = items.map((v) => `
    <button class="nav-item" data-view="${esc(v.key)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round">${ICONS[v.key] || ''}</svg>
      <span>${esc(v.title)}</span>
    </button>`).join('');
  nav.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => { location.hash = `#/${b.dataset.view}`; }));
}

function renderUserChip() {
  const chip = document.getElementById('user-chip');
  const u = state.user || getUser() || {};
  const name = u.display_name || u.username || '未登录';
  const roleLabel = ROLE_LABELS[state.role] || state.role;
  chip.innerHTML = `
    <span class="user-avatar">${esc(String(name).slice(0, 1))}</span>
    <span>${esc(name)} <small>· ${esc(roleLabel)}</small></span>`;
}

function setModeLine(health) {
  const dot = document.getElementById('mode-line').querySelector('.dot');
  const text = document.getElementById('mode-text');
  if (!health) {
    dot.className = 'dot dot-bad';
    text.textContent = `后端不可达 ${getApiBase()}`;
    return;
  }
  dot.className = health.database === 'up' ? 'dot dot-ok' : 'dot dot-bad';
  text.textContent = `${health.dialect} · dify=${health.dify_mode} · asr=${health.asr_provider}`;
}

async function route() {
  const items = visibleViews();
  const key = (location.hash || '').replace(/^#\/?/, '') || items[0].key;
  const view = items.find((v) => v.key === key) || items[0];

  // 角色变化导致当前页不可见时，跳回首个可见页
  if (!view) return;

  state.current = view;
  document.getElementById('page-title').textContent = view.title;
  document.getElementById('page-desc').textContent = view.desc || '';

  document.querySelectorAll('.nav-item').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === view.key));

  const root = document.getElementById('view');
  root.innerHTML = '';
  try {
    await view.mount(root, {
      role: state.role,
      user: state.user,
      setHealth: (h) => { state.health = h; setModeLine(h); },
    });
  } catch (err) {
    root.innerHTML = `<div class="empty">页面加载失败：${esc(err.message || err)}</div>`;
    if (!(err instanceof ApiError)) console.error(err);
  }
}

async function enterConsole() {
  showConsole();
  renderNav();
  renderUserChip();

  // 探测后端，顺便驱动左下角状态灯
  try {
    const { data } = await api.health();
    state.health = data;
    setModeLine(data);
  } catch (err) {
    setModeLine(null);
    if (err instanceof ApiError && err.status === 0) {
      toastErr(`后端不可达：${getApiBase()}`, '请确认服务已启动，或在登录页修改接口地址');
    }
  }

  const items = visibleViews();
  const currentKey = (location.hash || '').replace(/^#\/?/, '');
  if (!items.some((v) => v.key === currentKey)) {
    location.hash = `#/${items[0].key}`;   // 触发 hashchange → route()
  } else {
    await route();
  }
}

/* ======================================================================== */
/* 事件绑定                                                                  */
/* ======================================================================== */
// 落地页里的所有出口按钮：员工登录 → 登录屏；免费咨询 → 访客进控制台
document.querySelectorAll('[data-landing]').forEach((btn) =>
  btn.addEventListener('click', () => {
    if (btn.dataset.landing === 'login') showLogin(); else doVisitor();
  }));

document.getElementById('landing-theme').addEventListener('click', toggleTheme);

document.getElementById('btn-back-home').addEventListener('click', () => showLanding());

loginForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const apiBase = document.getElementById('login-api-base').value.trim();
  setApiBase(apiBase);
  if (!username || !password) { showLogin('请填写账号与密码'); return; }
  loginError.hidden = true;
  doLogin(username, password);
});

document.getElementById('btn-visitor').addEventListener('click', () => {
  setApiBase(document.getElementById('login-api-base').value.trim());
  doVisitor();
});

document.getElementById('btn-logout').addEventListener('click', () => doLogout());

document.getElementById('btn-theme').addEventListener('click', toggleTheme);

window.addEventListener('hashchange', () => { if (!consoleEl.hidden) route(); });

// 任意请求返回 401 时统一回到登录屏
window.addEventListener('uas:auth-lost', () => doLogout('登录已失效，请重新登录'));

/* ======================================================================== */
/* 启动                                                                      */
/* ======================================================================== */
(async function boot() {
  initTheme();
  renderDemoAccounts();
  document.getElementById('login-api-base').placeholder = DEFAULT_API_BASE;
  getSessionId();                             // 预置一个稳定会话号

  // 无 Token：停在访客落地页（登录屏只由「员工登录」按钮唤起）
  if (!getToken()) { showLanding(); return; }

  // 有 Token：先去后端验一下，避免拿着过期令牌进控制台
  try {
    const { data } = await api.me();
    state.role = data.role;
    state.user = data;
    await enterConsole();
  } catch {
    clearAuth();
    showLogin('登录状态已过期，请重新登录');
  }
})();
