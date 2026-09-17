/**
 * 前端配置。
 *
 * 后端与前端完全分离部署：本文件里的 API_BASE 指向后端服务地址，
 * 前端代码里不出现任何后端实现细节（数据库、Dify、密钥等一概不感知）。
 */

const KEYS = {
  token: 'uas.token',
  user: 'uas.user',
  apiBase: 'uas.api_base',
  session: 'uas.session_id',
  theme: 'uas.theme',
};

/** 默认后端地址。可用登录页的「接口地址」输入框或 localStorage 覆盖。 */
export const DEFAULT_API_BASE = 'http://127.0.0.1:8010';

export const STORAGE_KEYS = KEYS;

/**
 * 静态服务器注入的后端地址（见 frontend/runtime-config.js）。
 * 优先级：localStorage（用户手填）> 运行时注入 > DEFAULT_API_BASE。
 */
function runtimeApiBase() {
  try {
    return String(window.__UAS_API_BASE__ || '').trim();
  } catch {
    return '';
  }
}

export function getApiBase() {
  const saved = localStorage.getItem(KEYS.apiBase);
  const base = (saved || '').trim() || runtimeApiBase() || DEFAULT_API_BASE;
  return base.replace(/\/+$/, '');
}

export function setApiBase(value) {
  const v = (value || '').trim();
  if (v) {
    localStorage.setItem(KEYS.apiBase, v.replace(/\/+$/, ''));
  } else {
    localStorage.removeItem(KEYS.apiBase);
  }
}

export function getToken() {
  return localStorage.getItem(KEYS.token) || '';
}

export function setToken(token) {
  if (token) localStorage.setItem(KEYS.token, token);
  else localStorage.removeItem(KEYS.token);
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(KEYS.user) || 'null');
  } catch {
    return null;
  }
}

export function setUser(user) {
  if (user) localStorage.setItem(KEYS.user, JSON.stringify(user));
  else localStorage.removeItem(KEYS.user);
}

export function getSessionId() {
  let sid = localStorage.getItem(KEYS.session);
  if (!sid) {
    sid = 'web-' + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(KEYS.session, sid);
  }
  return sid;
}

export function clearAuth() {
  setToken('');
  setUser(null);
}

/** 角色 → 中文名 */
export const ROLE_LABELS = {
  visitor: '访客',
  student: '学员',
  employee: '员工',
  manager: '管理层',
  admin: '管理员',
};
