/**
 * 系统状态：探针 / 身份 / 改密 / Dify 工具清单 / 部署说明。
 */
import { api, API_V1 } from '../api.js';
import { getApiBase, ROLE_LABELS } from '../config.js';
import {
  badge, card, compact, emptyBox, esc, fmtDateTime, jsonBlock, kvList, loading,
  openModal, reportError, statCard, table, toastOk, toastErr, closeModal,
} from '../ui.js';

export default {
  key: 'system',
  title: '系统状态',
  desc: '前后端分离部署 · 探针 · 身份 · Dify 工具回调',
  roles: ['visitor', 'student', 'employee', 'manager', 'admin'],

  async mount(root, ctx) {
    root.innerHTML = `
      <div class="grid grid-4" id="sy-stats">${loading()}</div>

      <div class="grid grid-2" style="margin-top:16px">
        <div class="card">
          <div class="card-head">
            <div><h2>服务探针</h2><div class="card-sub" id="sy-target"></div></div>
            <div><button class="btn btn-sm" id="sy-refresh">重新探测</button></div>
          </div>
          <div class="card-body" id="sy-health">${loading()}</div>
        </div>

        <div class="card">
          <div class="card-head"><h2>当前身份</h2></div>
          <div class="card-body" id="sy-me">${loading()}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>Dify 内部工具回调</h2>
            <div class="card-sub">Dify 的 Agent / Workflow 通过 HMAC-SHA256 签名反向调用这些工具</div></div>
        </div>
        <div class="card-body" id="sy-tools">${loading()}</div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>前后端分离部署说明</h2>
            <div class="card-sub">前端零构建、零依赖，仅通过 HTTP 契约对接后端</div></div>
        </div>
        <div class="card-body" id="sy-deploy"></div>
      </div>`;

    /* ---------------- 探针 ---------------- */
    async function reloadHealth() {
      const box = root.querySelector('#sy-health');
      const stats = root.querySelector('#sy-stats');
      root.querySelector('#sy-target').textContent = `后端地址：${getApiBase()}`;
      box.innerHTML = loading('探测中…');
      const t0 = performance.now();
      try {
        const [{ data: health, costMs }, { data: ready }] = await Promise.all([
          api.health(), api.ready(),
        ]);
        const rtt = (performance.now() - t0).toFixed(0);
        stats.innerHTML = [
          statCard('服务状态', health.status === 'ok' ? '正常' : '降级'),
          statCard('数据库', health.database === 'up' ? '已连通' : '不可用'),
          statCard('方言', health.dialect),
          statCard('往返延迟', `${rtt} ms`),
        ].join('');
        box.innerHTML = kvList([
          ['应用名称', health.app],
          ['版本', health.version],
          ['服务状态', health.status],
          ['数据库', `${health.database}（${health.dialect}）`],
          ['Dify 模式', health.dify_mode],
          ['ASR 提供方', health.asr_provider],
          ['就绪探针', ready.ready ? '就绪' : '未就绪'],
          ['Dify 直连', ready.dify_live ? '是（live）' : '否（mock）'],
          ['后端耗时头', `${costMs.toFixed(0)} ms`],
        ]);
        ctx.setHealth && ctx.setHealth(health);
      } catch (err) {
        stats.innerHTML = [
          statCard('服务状态', '不可达'),
          statCard('数据库', '—'), statCard('方言', '—'), statCard('往返延迟', '—'),
        ].join('');
        box.innerHTML = `<div class="empty">无法连接 ${esc(getApiBase())}<br>
          ${esc(err.message)}</div>`;
        ctx.setHealth && ctx.setHealth(null);
      }
    }
    root.querySelector('#sy-refresh').addEventListener('click', reloadHealth);

    /* ---------------- 身份 ---------------- */
    try {
      const { data: me } = await api.me();
      root.querySelector('#sy-me').innerHTML = `
        ${kvList([
          ['账号', me.username], ['角色', ROLE_LABELS[me.role] || me.role],
          ['显示名', me.display_name], ['绑定业务 ID', me.ref_id],
        ])}
        <button class="btn btn-primary" id="sy-pwd" style="margin-top:14px">修改密码</button>`;
      root.querySelector('#sy-pwd').addEventListener('click', () => {
        const h = openModal({
          title: '修改密码',
          width: '420px',
          body: `
            <label class="field"><span>原密码</span><input type="password" id="pw-old"></label>
            <label class="field"><span>新密码（≥6 位）</span><input type="password" id="pw-new"></label>`,
          footer: `<button class="btn" data-modal-close>取消</button>
                   <button class="btn btn-primary" id="pw-save">提交</button>`,
        });
        h.root.querySelector('#pw-save').addEventListener('click', async () => {
          const oldPwd = h.root.querySelector('#pw-old').value;
          const newPwd = h.root.querySelector('#pw-new').value;
          if (newPwd.length < 6) { toastErr('新密码至少 6 位'); return; }
          try {
            await api.changePassword(oldPwd, newPwd);
            toastOk('密码已修改');
            closeModal();
          } catch (err) {
            reportError(err, '修改失败');
          }
        });
      });
    } catch (err) {
      root.querySelector('#sy-me').innerHTML = emptyBox('身份获取失败');
      reportError(err, '身份获取');
    }

    /* ---------------- 工具清单 ---------------- */
    try {
      const { data } = await api.tools();
      root.querySelector('#sy-tools').innerHTML = `
        ${table([
          { title: '工具名', render: (r) => `<span class="mono">${esc(r.name)}</span>` },
          { title: '说明', cls: 'wrap', key: 'desc' },
          { title: '参数', cls: 'wrap', render: (r) => `<span class="mono">${esc(JSON.stringify(r.params))}</span>` },
          {
            title: '调用地址',
            render: () => '<span class="mono">POST /internal/tools/{name}</span>',
          },
        ], data.items)}
        <p class="field-hint" style="margin-top:12px">
          调用必须带 <span class="mono">X-Tool-Timestamp</span> 与
          <span class="mono">X-Tool-Signature</span>（HMAC-SHA256，签名内容 timestamp + "." + raw_body），
          时间窗 ±300s；签名不合法直接 403。
        </p>`;
    } catch (err) {
      root.querySelector('#sy-tools').innerHTML = emptyBox('工具清单获取失败');
      reportError(err, '工具清单');
    }

    /* ---------------- 部署说明 ---------------- */
    root.querySelector('#sy-deploy').innerHTML = `
      <div class="grid grid-2">
        <div>
          <div class="card-sub" style="margin-bottom:8px">前端</div>
          ${jsonBlock({
            stack: '原生 ES Module + CSS 变量主题，零构建零依赖',
            entry: 'frontend/index.html',
            api_base: getApiBase(),
            routing: 'hash 路由（#/chat、#/crm …），刷新不丢上下文',
            auth: 'Bearer Token 存 localStorage，401 自动清理并回到登录屏',
          })}
        </div>
        <div>
          <div class="card-sub" style="margin-bottom:8px">后端</div>
          ${jsonBlock({
            stack: 'Python + FastAPI + SQLAlchemy 2.0 + Dify + MySQL/SQLite',
            prefix: API_V1,
            envelope: '{ code, message, data, trace_id }',
            headers: ['Authorization: Bearer <jwt>', 'x-trace-id', 'x-process-time-ms'],
            cors: '由后端 CORS_ORIGINS 白名单控制，前端跨域直连',
          })}
        </div>
      </div>
      <p class="field-hint" style="margin-top:14px">
        换后端地址有两种方式：登录页的「接口地址」输入框，或在浏览器控制台执行
        <span class="mono">localStorage.setItem('uas.api_base','http://host:port')</span> 后刷新。
      </p>`;

    await reloadHealth();
  },
};
