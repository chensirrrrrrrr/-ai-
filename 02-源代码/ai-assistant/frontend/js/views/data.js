/**
 * 数据洞察：受控 NL2SQL 查询 + 审计日志。
 */
import { api } from '../api.js';
import {
  badge, card, compact, emptyBox, esc, fmtDateTime, jsonBlock, kvList, loading,
  openModal, reportError, statCard, table, toastOk, toastErr, toInt, show,
} from '../ui.js';

const SCOPES = [
  ['', '自动（按登录角色推导）'],
  ['sales', 'sales 销售域'],
  ['academic', 'academic 教务域'],
  ['manager', 'manager 管理域'],
  ['admin', 'admin 全域'],
];

export default {
  key: 'data',
  title: '数据洞察',
  desc: '自然语言查库（模板 + 表白名单 + 越权拦截）与操作审计',
  roles: ['employee', 'manager', 'admin'],

  async mount(root, ctx) {
    const isManager = ctx.role === 'manager' || ctx.role === 'admin';

    root.innerHTML = `
      <div class="card">
        <div class="card-head">
          <div>
            <h2>自然语言查库</h2>
            <div class="card-sub">
              模型只能选模板 + 抽参数，SQL 由服务端白名单拼装；越权范围直接 403
            </div>
          </div>
        </div>
        <div class="card-body">
          <div class="toolbar">
            <input class="grow" id="nl-q" placeholder="例如：最近 30 天的意向客户跟进情况">
            <select id="nl-scope">${SCOPES
              .map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>
            <input id="nl-rows" type="number" min="1" max="200" value="50" style="width:90px">
            <button class="btn btn-primary" id="nl-run">查询</button>
          </div>

          <div class="card-sub" style="margin-bottom:8px">可用模板（点击自动填充提问）</div>
          <div id="nl-templates" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">
            ${loading()}
          </div>

          <div id="nl-result">${emptyBox('还没有查询结果')}</div>
        </div>
      </div>

      ${isManager ? `
      <div class="card">
        <div class="card-head">
          <div><h2>审计日志</h2>
            <div class="card-sub">每一次写操作都落库并带 trace_id；仅管理层可查</div></div>
          <div><button class="btn btn-sm" id="au-refresh">刷新</button></div>
        </div>
        <div class="card-body">
          <div class="toolbar">
            <input id="au-action" placeholder="action，如 auth.login" class="grow">
            <input id="au-actor" placeholder="操作者">
            <input id="au-trace" placeholder="trace_id">
            <input id="au-limit" type="number" min="1" max="200" value="50" style="width:90px">
            <button class="btn" id="au-search">检索</button>
          </div>
          <div id="au-list">${loading()}</div>
        </div>
      </div>` : ''}`;

    /* ---------------- 模板 ---------------- */
    const tplBox = root.querySelector('#nl-templates');
    try {
      const { data } = await api.nl2sqlTemplates();
      tplBox.innerHTML = data.items.map((t) => `
        <button class="btn btn-sm" data-tpl="${esc(t.title)}" title="范围：${esc(t.scopes.join(' / '))}">
          ${esc(t.title)}
        </button>`).join('') || emptyBox('没有可用模板');
      tplBox.querySelectorAll('[data-tpl]').forEach((b) =>
        b.addEventListener('click', () => {
          root.querySelector('#nl-q').value = b.dataset.tpl;
          root.querySelector('#nl-q').focus();
        }));
    } catch (err) {
      tplBox.innerHTML = emptyBox('模板加载失败');
      reportError(err, '模板加载');
    }

    /* ---------------- 查询 ---------------- */
    root.querySelector('#nl-run').addEventListener('click', runQuery);
    root.querySelector('#nl-q').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') runQuery();
    });

    async function runQuery() {
      const question = root.querySelector('#nl-q').value.trim();
      if (question.length < 2) { toastErr('问题至少 2 个字符'); return; }
      const out = root.querySelector('#nl-result');
      out.innerHTML = loading('解析并执行…');
      const t0 = performance.now();
      try {
        const { data, traceId } = await api.nl2sql(compact({
          question,
          role_scope: root.querySelector('#nl-scope').value,
          max_rows: toInt(root.querySelector('#nl-rows').value, 50),
        }));
        const cols = data.columns.map((c) => ({ title: c, render: (row) => esc(show(row[c])) }));
        out.innerHTML = `
          <div class="grid grid-4" style="margin-bottom:14px">
            ${statCard('命中模板', data.template_id)}
            ${statCard('返回行数', data.row_count)}
            ${statCard('字段数', data.columns.length)}
            ${statCard('耗时', `${(performance.now() - t0).toFixed(0)} ms`)}
          </div>
          <div class="card-sub" style="margin-bottom:6px">结论</div>
          <div class="msg-bubble" style="margin-bottom:14px">${esc(data.answer_text)}</div>
          <div class="card-sub" style="margin-bottom:6px">生成 SQL（只读预览）</div>
          <pre class="code">${esc(data.sql_preview)}</pre>
          <div class="card-sub" style="margin:14px 0 6px">结果集</div>
          ${table(cols, data.rows.map((r) => {
            const o = {}; data.columns.forEach((c, i) => { o[c] = r[i]; }); return o;
          }), { empty: '查询无结果' })}
          <p class="field-hint" style="margin-top:12px">trace_id：<span class="mono">${esc(traceId)}</span></p>`;
        toastOk('查询完成', `模板 ${data.template_id} · ${data.row_count} 行`);
      } catch (err) {
        if (err.isForbidden) {
          out.innerHTML = `<div class="empty">越权被拦截：${esc(err.message)}</div>`;
        } else {
          out.innerHTML = emptyBox('查询失败');
        }
        reportError(err, '查询失败');
      }
    }

    /* ---------------- 审计 ---------------- */
    async function reloadAudit() {
      const box = root.querySelector('#au-list');
      if (!box) return;
      box.innerHTML = loading();
      try {
        const { data } = await api.auditLogs(compact({
          action: root.querySelector('#au-action').value.trim() || undefined,
          actor_id: root.querySelector('#au-actor').value.trim() || undefined,
          trace_id: root.querySelector('#au-trace').value.trim() || undefined,
          limit: toInt(root.querySelector('#au-limit').value, 50),
        }));
        box.innerHTML = table([
          { title: 'ID', key: 'id' },
          { title: '操作者', render: (r) => esc(r.actor_id) },
          { title: '角色', render: (r) => badge(r.actor_role) },
          { title: '动作', render: (r) => `<span class="mono">${esc(r.action)}</span>` },
          { title: '资源', render: (r) => `${esc(r.resource)}${r.resource_id ? `#${esc(r.resource_id)}` : ''}` },
          {
            title: '详情',
            render: (r) => (r.detail
              ? `<button class="btn btn-sm" data-detail="${r.id}">查看</button>`
              : '<span class="field-hint">—</span>'),
          },
          { title: 'trace', render: (r) => `<span class="mono">${esc((r.trace_id || '').slice(0, 10))}</span>` },
          { title: '时间', render: (r) => esc(fmtDateTime(r.created_at)) },
        ], data.items, { empty: '没有匹配的审计记录' });

        box.querySelectorAll('[data-detail]').forEach((b) => {
          b.addEventListener('click', () => {
            const row = data.items.find((x) => x.id === Number(b.dataset.detail));
            openModal({
              title: `审计详情 #${row.id}`,
              width: '560px',
              body: `${kvList([
                ['操作者', row.actor_id], ['角色', row.actor_role], ['动作', row.action],
                ['资源', row.resource], ['资源 ID', row.resource_id],
                ['trace_id', row.trace_id], ['时间', fmtDateTime(row.created_at)],
              ])}
              <div class="card-sub" style="margin:12px 0 6px">detail</div>
              ${jsonBlock(row.detail || {})}`,
            });
          });
        });
      } catch (err) {
        box.innerHTML = emptyBox('加载失败（员工角色访问会返回 403）');
        reportError(err, '审计日志');
      }
    }

    if (isManager) {
      root.querySelector('#au-refresh').addEventListener('click', reloadAudit);
      root.querySelector('#au-search').addEventListener('click', reloadAudit);
      await reloadAudit();
    }
  },
};
