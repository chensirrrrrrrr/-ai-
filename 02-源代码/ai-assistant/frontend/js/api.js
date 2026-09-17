/**
 * 后端 API 客户端 —— 前端与后端唯一的耦合点。
 *
 * 前端不感知后端的任何实现细节（数据库方言 / Dify 应用 / 密钥 / 模板清单），
 * 只依赖 HTTP 契约：统一响应信封 { code, message, data, trace_id }。
 * 换域名只需要改 config.js 里的 API_BASE。
 */
import { getApiBase, getToken, clearAuth } from './config.js';

export const API_V1 = '/api/v1';

export class ApiError extends Error {
  constructor(message, ctx = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = ctx.code ?? -1;
    this.status = ctx.status ?? 0;
    this.traceId = ctx.traceId || '';
    this.payload = ctx.payload || null;
  }

  get isAuth() { return this.status === 401 || this.code === 40100; }
  get isForbidden() { return this.status === 403 || this.code === 40300; }
  get isNotFound() { return this.status === 404 || this.code === 40400; }
  get isConflict() { return this.status === 409 || this.code === 40900; }
}

function buildUrl(path, query) {
  const base = getApiBase();
  const full = base + (path.startsWith('/') ? path : `/${path}`);
  const url = new URL(full);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === '') continue;
      url.searchParams.set(k, v);
    }
  }
  return url.toString();
}

function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function extractMessage(status, json) {
  if (json && typeof json.message === 'string' && json.message) return json.message;
  if (json && json.detail) {
    return typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail);
  }
  if (status === 0) return '无法连接后端服务';
  if (status === 401) return '登录已失效，请重新登录';
  if (status === 403) return '当前角色无权执行该操作';
  if (status === 404) return '资源不存在';
  if (status === 422) return '请求参数不合法';
  return `请求失败（HTTP ${status}）`;
}

/**
 * 发起请求。成功返回 { data, traceId, costMs }，失败抛 ApiError。
 */
export async function request(method, path, opts = {}) {
  const { body, query, formData, auth = true, timeout = 60000 } = opts;

  const headers = {};
  if (auth) Object.assign(headers, authHeaders());

  let payload;
  if (formData) {
    payload = formData;                      // 交给浏览器自动带 boundary
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  let resp;
  try {
    resp = await fetch(buildUrl(path, query), {
      method, headers, body: payload, signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    throw new ApiError(err && err.name === 'AbortError' ? '请求超时，请稍后重试' : '无法连接后端服务',
      { status: 0 });
  }
  clearTimeout(timer);

  const traceId = resp.headers.get('x-trace-id') || '';
  const costMs = Number(resp.headers.get('x-process-time-ms') || 0);

  const text = await resp.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* 非 JSON（如纯文本下载） */ }

  const code = json && typeof json.code === 'number' ? json.code : 0;
  if (!resp.ok || code !== 0) {
    if (resp.status === 401) {
      clearAuth();
      window.dispatchEvent(new CustomEvent('uas:auth-lost'));
    }
    throw new ApiError(extractMessage(resp.status, json),
      { code, status: resp.status, traceId, payload: json });
  }

  return { data: json ? json.data : null, traceId, costMs };
}

const get = (path, query, opts) => request('GET', path, { query, ...opts });
const post = (path, body, opts) => request('POST', path, { body, ...opts });
const patch = (path, body, opts) => request('PATCH', path, { body, ...opts });
const del = (path, opts) => request('DELETE', path, opts);

export const api = {
  request,

  // ---------- 系统 / 认证 ----------
  health: () => get(`${API_V1}/health`, null, { auth: false }),
  ready: () => get(`${API_V1}/ready`, null, { auth: false }),
  login: (username, password) =>
    post(`${API_V1}/auth/token`, { username, password }, { auth: false }),
  visitor: () => post(`${API_V1}/auth/visitor`, {}, { auth: false }),
  me: () => get(`${API_V1}/auth/me`),
  changePassword: (oldPassword, newPassword) =>
    post(`${API_V1}/auth/password`, { old_password: oldPassword, new_password: newPassword }),

  // ---------- 对话 ----------
  chat: (payload) => post(`${API_V1}/chat/message`, payload, { timeout: 90000 }),
  asr: (file, purpose = 'daily_report') => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('purpose', purpose);
    return post(`${API_V1}/chat/asr`, undefined, { formData: fd, timeout: 120000 });
  },

  // ---------- 客户域 ----------
  listLeads: (query) => get(`${API_V1}/leads`, query),
  createLead: (body) => post(`${API_V1}/leads`, body),
  getLead: (id) => get(`${API_V1}/leads/${id}`),
  updateLeadStatus: (id, body) => patch(`${API_V1}/leads/${id}/status`, body),
  addFollowup: (id, body) => post(`${API_V1}/leads/${id}/followups`, body),
  listFollowups: (id) => get(`${API_V1}/leads/${id}/followups`),
  analyzeScreening: (body) => post(`${API_V1}/screening/analyze`, body, { timeout: 90000 }),
  getScreening: (id) => get(`${API_V1}/screening/${id}`),
  listScreenings: (query) => get(`${API_V1}/screening`, query),
  /** 上传材料（PDF / Excel / CSV / TXT）→ 服务端解析 + 抽字段 */
  uploadScreeningMaterial: (file, leadId) => {
    const fd = new FormData();
    fd.append('file', file);
    if (leadId) fd.append('lead_id', leadId);
    return post(`${API_V1}/screening/upload`, undefined, { formData: fd, timeout: 120000 });
  },
  screeningFileUrl: (relPath) => buildUrl(`${API_V1}/screening/files/${relPath}`),
  /**
   * 取材料原件的本地 blob URL。
   * 下载接口要求 Bearer 鉴权，`<a href>` / `window.open` 裸开不会带 Token（必然 401），
   * 所以统一走「带鉴权 fetch → blob URL」。调用方负责在合适时机 revokeObjectURL。
   */
  screeningFileBlobUrl: async (relPath) => {
    const resp = await fetch(buildUrl(`${API_V1}/screening/files/${relPath}`),
      { headers: authHeaders() });
    if (!resp.ok) {
      if (resp.status === 401) clearAuth();
      throw new ApiError(extractMessage(resp.status, null), { status: resp.status });
    }
    return URL.createObjectURL(await resp.blob());
  },
  /** 批量研判：传 items（批量材料）或 lead_ids（勾选客户） */
  batchScreening: (body) => post(`${API_V1}/screening/batch`, body, { timeout: 180000 }),
  /** 人工复核：action=CONFIRM | OVERRIDE */
  reviewScreening: (id, body) => patch(`${API_V1}/screening/${id}/review`, body),
  /** 人工修正清单（回写用于规则优化） */
  screeningCorrections: (query) => get(`${API_V1}/screening/corrections`, query),

  // ---------- 学员域 ----------
  listStudents: (query) => get(`${API_V1}/students`, query),
  getStudent: (id) => get(`${API_V1}/students/${id}`),
  studentProgress: (id) => get(`${API_V1}/students/${id}/progress`),
  createScore: (body) => post(`${API_V1}/scores`, body),
  listScores: (query) => get(`${API_V1}/scores`, query),
  applyLeave: (body) => post(`${API_V1}/leave/apply`, body),
  listRequests: (query) => get(`${API_V1}/requests`, query),
  approveLeave: (id, body) => post(`${API_V1}/leave/${id}/approve`, body),
  createTicket: (body) => post(`${API_V1}/tickets`, body),
  listTickets: (query) => get(`${API_V1}/tickets`, query),
  updateTicket: (id, body) => patch(`${API_V1}/tickets/${id}`, body),
  listAlerts: (query) => get(`${API_V1}/alerts`, query),
  updateAlert: (id, body) => patch(`${API_V1}/alerts/${id}`, body),

  // ---------- 组织域 ----------
  orgTree: (query) => get(`${API_V1}/org/tree`, query),
  orgDepartments: () => get(`${API_V1}/org/departments`),
  listEmployees: (query) => get(`${API_V1}/org/employees`, query),
  getEmployee: (id) => get(`${API_V1}/org/employees/${id}`),
  onboardingGuide: () => get(`${API_V1}/org/onboarding/guide`),
  onboardingChecklist: (stage) => get(`${API_V1}/org/onboarding/checklist`, { stage }),
  onboardingFaq: (keyword, limit) => get(`${API_V1}/org/onboarding/faq`, { keyword, limit }),
  onboardingContacts: (employeeId) =>
    get(`${API_V1}/org/onboarding/contacts`, { employee_id: employeeId }),

  // ---------- 主动待办推送 / 预警触达 ----------
  todoPending: () => get(`${API_V1}/todo/pending`),
  /** 手动触发一轮主动推送（定时任务的同一入口）；categories 不传 = 全部类别 */
  todoPush: (body) => post(`${API_V1}/todo/push`, body || {}),
  todoPushes: (query) => get(`${API_V1}/todo/pushes`, query),
  ackTodoPush: (id, body) => patch(`${API_V1}/todo/pushes/${id}/ack`, body || {}),
  /** 触达心理预警（写 notified_at + 干预建议）；alert_ids 不传 = 全部未触达 */
  notifyAlerts: (body) => post(`${API_V1}/alerts/notify`, body || {}),

  // ---------- 运营域 ----------
  listActivities: (query) => get(`${API_V1}/activities`, query),
  createActivity: (body) => post(`${API_V1}/activities`, body),
  enrollActivity: (id, body) => post(`${API_V1}/activities/${id}/enroll`, body),
  listEnrollments: (id) => get(`${API_V1}/activities/${id}/enrollments`),
  generateReport: (body) => post(`${API_V1}/reports/generate`, body, { timeout: 90000 }),
  listReports: (query) => get(`${API_V1}/reports`, query),
  getReport: (id) => get(`${API_V1}/reports/${id}`),
  reportDownloadUrl: (id) => buildUrl(`${API_V1}/reports/${id}/download`),
  /** 五类业务报告的口径清单（前端下拉用，避免前端写死类型） */
  reportTypes: () => get(`${API_V1}/reports/types`),
  /** 手动跑一轮定时报告（force=true 忽略「今天该不该生成」） */
  runScheduledReports: (query) => post(`${API_V1}/reports/scheduled/run`, undefined, { query }),
  /**
   * 取报告导出件的 blob URL。
   * 导出接口要 Bearer 鉴权，`<a href>` 裸开不会带 Token（必然 401），所以统一走
   * 带鉴权 fetch。`withName=true` 时额外返回从 Content-Disposition 解析出的中文文件名
   * （服务端用 RFC 6266 的 filename* 传参）。
   */
  reportExportBlobUrl: async (id, format, withName = false) => {
    const resp = await fetch(buildUrl(`${API_V1}/reports/${id}/export`, { format }),
      { headers: authHeaders() });
    if (!resp.ok) {
      if (resp.status === 401) clearAuth();
      throw new ApiError(extractMessage(resp.status, null), { status: resp.status });
    }
    const url = URL.createObjectURL(await resp.blob());
    if (!withName) return url;

    const disposition = resp.headers.get('content-disposition') || '';
    const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    let filename = `report.${format}`;
    if (match) {
      try { filename = decodeURIComponent(match[1]); } catch { /* 保持回退名 */ }
    }
    return { url, filename };
  },
  submitDailyReport: (body) => post(`${API_V1}/reports/daily`, body),
  dailySummary: (date) => get(`${API_V1}/reports/daily/summary`, { report_date: date }),
  dailyTrend: (days = 7) => get(`${API_V1}/reports/daily/trend`, { days }),
  listKbDocuments: (query) => get(`${API_V1}/kb/documents`, query),
  createKbDocument: (body) => post(`${API_V1}/kb/documents`, body),

  // ---------- 数据 ----------
  nl2sql: (body) => post(`${API_V1}/nl2sql/query`, body),
  nl2sqlTemplates: () => get(`${API_V1}/nl2sql/templates`),
  auditLogs: (query) => get(`${API_V1}/audit/logs`, query),

  // ---------- 内部工具（供 Dify 侧调用，前端只做只读展示） ----------
  tools: () => get('/internal/tools', null, { auth: false }),
};

/**
 * 流式对话（SSE over POST）。
 * handlers: { onRoute, onMessage, onEnd, onError }
 */
export async function streamChat(payload, handlers = {}) {
  const url = buildUrl(`${API_V1}/chat/stream`);
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError('无法连接后端服务', { status: 0 });
  }
  if (!resp.ok || !resp.body) {
    if (resp.status === 401) clearAuth();
    throw new ApiError(extractMessage(resp.status, null), { status: resp.status });
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);

      const line = raw.split('\n').find((l) => l.startsWith('data:'));
      if (!line) continue;
      const body = line.slice(5).trim();

      if (body === '[DONE]') {
        handlers.onEnd && handlers.onEnd();
        return;
      }
      let evt;
      try { evt = JSON.parse(body); } catch { continue; }

      if (evt.event === 'route') handlers.onRoute && handlers.onRoute(evt);
      else if (evt.event === 'message') handlers.onMessage && handlers.onMessage(evt);
      else if (evt.event === 'message_end') handlers.onEnd && handlers.onEnd(evt);
      else if (evt.event === 'error') handlers.onError && handlers.onError(new ApiError(evt.message || '生成中断'));
    }
  }
  handlers.onEnd && handlers.onEnd();
}
