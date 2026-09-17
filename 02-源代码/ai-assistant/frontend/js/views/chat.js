/**
 * 对话助手视图：统一对话入口 + 意图路由可视化 + 语音录入。
 */
import { api, streamChat } from '../api.js';
import {
  badge, card, compact, emptyBox, esc, jsonBlock, kvList, reportError, show,
  toastOk, toastErr, toastWarn, uid, fmtClock, today, toInt,
} from '../ui.js';

const AGENT_LABELS = {
  customer_service: '客服 Agent',
  student_helper: '学员助手',
  enterprise_assistant: '企业助手',
  mental_care: '心理关怀',
  reporter: '报表 Agent',
  screener: '材料研判',
  router: '路由工作流',
};

const ROLE_AGENTS = {
  visitor: ['customer_service'],
  student: ['student_helper', 'mental_care'],
  employee: ['enterprise_assistant', 'student_helper'],
  manager: ['enterprise_assistant', 'reporter', 'student_helper'],
  admin: ['customer_service', 'student_helper', 'enterprise_assistant', 'mental_care', 'reporter', 'screener'],
};

const QUICK = [
  '留学需要准备哪些材料？',
  '我想申请英国硕士，预算大概多少？',
  '我要请假三天',
  '我要投诉课程安排',
  '帮我查一下最近的意向客户跟进情况',
];

/**
 * 主动待办提醒横幅（需求：企业助手「主动待办推送」）。
 *
 * 刻意不 await：横幅是加分项，拉不到就安静地不显示 ——
 * 不能让一个待办接口把对话主链路拖慢或拖挂。
 */
async function loadTodoBanner(root) {
  const box = root.querySelector('#todo-banner');
  if (!box) return;
  try {
    const { data } = await api.todoPending();
    if (!data.items.length) return;
    const high = data.highest_severity === 'high';
    const first = data.items[0];
    box.hidden = false;
    box.innerHTML = `
      <div class="todo-banner${high ? ' is-high' : ''}">
        <div class="todo-banner-head">
          <span class="badge ${high ? 'badge-bad' : 'badge-warn'}">主动提醒</span>
          <strong>${esc(first.question)}</strong>
          <span class="card-sub">共 ${data.total} 件待处理 · ${data.category_count} 类</span>
        </div>
        <div class="chips">${data.items.map((t) => `<span class="chip${
          t.severity === 'high' ? ' chip-missing' : ''}">${esc(t.title)} ×${t.count}</span>`).join('')}</div>
        <div class="todo-banner-foot">
          <button class="btn btn-sm btn-primary" id="todo-ask">让助手念一遍</button>
          <button class="btn btn-sm" id="todo-goto">去运营中心处理</button>
          <button class="btn btn-sm" id="todo-dismiss">知道了</button>
        </div>
      </div>`;
    const ask = box.querySelector('#todo-ask');
    if (ask) ask.addEventListener('click', () => {
      const input = root.querySelector('#chat-text');
      if (input) { input.value = '今天有什么待办要处理？'; input.focus(); }
    });
    const go = box.querySelector('#todo-goto');
    if (go) go.addEventListener('click', () => {
      const nav = document.querySelector('.nav-item[data-view="ops"]');
      if (nav) nav.click();
    });
    const dismiss = box.querySelector('#todo-dismiss');
    if (dismiss) dismiss.addEventListener('click', () => { box.hidden = true; });
  } catch {
    box.hidden = true;                  // 拉不到就当作没有提醒
  }
}

export default {
  key: 'chat',
  title: '对话助手',
  desc: '统一入口 → 意图路由 → Dify Agent，角色越权自动降级',
  roles: ['visitor', 'student', 'employee', 'manager', 'admin'],

  mount(root, ctx) {
    const state = {
      messages: [],
      conversationId: null,
      sessionId: uid('web'),
      streaming: false,
      lastRoute: null,
    };

    root.innerHTML = `
      <div id="todo-banner" hidden></div>
      <div class="chat-shell">
        <div class="card chat-panel">
          <div class="card-head">
            <div>
              <h2>对话</h2>
              <div class="card-sub">角色：${esc(ctx.role)} · 会话 ${esc(state.sessionId)}</div>
            </div>
            <div>
              <button class="btn btn-sm" id="chat-clear">清空</button>
            </div>
          </div>
          <div class="chat-log" id="chat-log"></div>
          <div class="chat-input">
            <textarea id="chat-text" placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"></textarea>
            <div class="chat-actions">
              <label class="stream-toggle">
                <input type="checkbox" id="chat-stream" checked> 流式输出
              </label>
              <select id="asr-purpose" style="width:auto">
                <option value="chat">语音输入·对话</option>
                <option value="daily_report">语音输入·日报</option>
                <option value="leave_apply">语音输入·请假</option>
              </select>
              <input type="file" id="asr-file" accept="audio/*" hidden>
              <button class="btn btn-sm" id="asr-btn">语音录入</button>
              <span class="grow"></span>
              <button class="btn btn-primary" id="chat-send">发送</button>
            </div>
          </div>
        </div>

        <div>
          <div class="card">
            <div class="card-head"><h2>路由状态</h2></div>
            <div class="card-body" id="route-panel">${emptyBox('尚未发起对话')}</div>
          </div>

          <div class="card">
            <div class="card-head"><h2>当前角色可及 Agent</h2></div>
            <div class="card-body" id="agent-panel"></div>
          </div>

          <div class="card">
            <div class="card-head"><h2>快捷提问</h2></div>
            <div class="card-body" id="quick-panel"></div>
          </div>
        </div>
      </div>`;

    const log = root.querySelector('#chat-log');
    const textarea = root.querySelector('#chat-text');
    const routePanel = root.querySelector('#route-panel');

    /* ---------- 角色可及 Agent ---------- */
    const allowed = ROLE_AGENTS[ctx.role] || ROLE_AGENTS.visitor;
    root.querySelector('#agent-panel').innerHTML =
      `<div style="display:flex;flex-wrap:wrap;gap:6px">${
        allowed.map((a) => `<span class="badge badge-info">${esc(AGENT_LABELS[a] || a)}</span>`).join('')
      }</div>
      <p class="field-hint" style="margin:10px 0 0">
        越权请求会被服务端自动降级到本角色的兜底 Agent，前端无法绕过。
      </p>`;

    /* ---------- 快捷提问 ---------- */
    root.querySelector('#quick-panel').innerHTML =
      `<div style="display:flex;flex-direction:column;gap:6px">${
        QUICK.map((q) => `<button class="btn btn-sm" data-quick="${esc(q)}"
           style="justify-content:flex-start;text-align:left;white-space:normal">${esc(q)}</button>`).join('')
      }</div>`;
    root.querySelectorAll('[data-quick]').forEach((b) =>
      b.addEventListener('click', () => { textarea.value = b.dataset.quick; send(); }));

    /* ---------- 消息渲染 ---------- */
    function pushMessage(role, text, meta = {}) {
      state.messages.push({ role, text, meta, at: new Date().toISOString() });
      renderLog();
    }

    function bubble(msg) {
      const isMe = msg.role === 'me';
      const head = isMe ? '我' : 'AI';
      const refs = (msg.meta.references || []).length
        ? `<div class="refs">引用：<ul>${msg.meta.references
            .map((r) => `<li>${esc(show(r.doc || r))}${r.section ? ` · ${esc(r.section)}` : ''}</li>`)
            .join('')}</ul></div>`
        : '';
      const metaBits = [];
      if (!isMe) {
        if (msg.meta.agent) metaBits.push(`<span class="badge badge-info">${esc(AGENT_LABELS[msg.meta.agent] || msg.meta.agent)}</span>`);
        if (msg.meta.intent) metaBits.push(`<span class="badge badge-neutral">${esc(msg.meta.intent)}</span>`);
        if (msg.meta.confidence !== undefined && msg.meta.confidence !== null) {
          metaBits.push(`<span>置信度 ${(Number(msg.meta.confidence) * 100).toFixed(0)}%</span>`);
        }
        if (msg.meta.degraded) metaBits.push('<span class="badge badge-warn">已降级</span>');
        if (msg.meta.traceId) metaBits.push(`<span class="mono">trace ${esc(msg.meta.traceId.slice(0, 8))}</span>`);
      } else {
        metaBits.push(`<span>${esc(fmtClock(msg.at))}</span>`);
      }
      const actions = (msg.meta.suggest_actions || []).length
        ? `<div class="refs">建议动作：<ul>${msg.meta.suggest_actions
            .map((a) => `<li>${esc(a)}</li>`).join('')}</ul></div>`
        : '';
      return `<div class="msg ${isMe ? 'me' : 'assistant'}">
        <div class="msg-avatar">${esc(head)}</div>
        <div class="msg-body">
          <div class="msg-bubble">${esc(msg.text)}</div>
          ${metaBits.length ? `<div class="msg-meta">${metaBits.join('')}</div>` : ''}
          ${refs}${actions}
        </div>
      </div>`;
    }

    function renderLog() {
      log.innerHTML = state.messages.length
        ? state.messages.map(bubble).join('')
        : emptyBox('还没有对话，试试右侧的快捷提问。');
      log.scrollTop = log.scrollHeight;
    }

    function renderRoute(extra = {}) {
      if (!state.lastRoute) return;
      const r = state.lastRoute;
      routePanel.innerHTML = kvList([
        ['Agent', AGENT_LABELS[r.agent] || r.agent],
        ['意图', r.intent],
        ['置信度', `${(Number(r.confidence || 0) * 100).toFixed(0)}%`],
        ['路由来源', r.route_source || '—'],
        ['动作', r.action || 'dispatch'],
        ['权限降级', r.permission_denied ? `是（原 ${AGENT_LABELS[r.original_agent] || r.original_agent}）` : '否'],
        ['conversation', state.conversationId || '未建立'],
        ...Object.entries(extra),
      ]);
    }

    /* ---------- 发送 ---------- */
    async function send() {
      const message = textarea.value.trim();
      if (!message) { toastWarn('请输入内容'); return; }
      if (state.streaming) { toastWarn('上一条还在生成中'); return; }

      pushMessage('me', message);
      textarea.value = '';

      // 不传 role：服务端一律以 Token 里的角色为准，前端传了也不作数
      const payload = compact({
        session_id: state.sessionId,
        message,
        channel: 'web',
        conversation_id: state.conversationId,
      });

      const useStream = root.querySelector('#chat-stream').checked;
      const sendBtn = root.querySelector('#chat-send');
      const t0 = performance.now();

      try {
        if (useStream) {
          await sendStream(payload, message, t0);
        } else {
          sendBtn.disabled = true;
          const { data, traceId, costMs } = await api.chat(payload);
          state.conversationId = data.conversation_id || state.conversationId;
          state.lastRoute = {
            agent: data.agent, intent: data.intent, confidence: data.confidence,
            route_source: 'server', action: 'dispatch',
          };
          pushMessage('assistant', data.answer, {
            agent: data.agent, intent: data.intent, confidence: data.confidence,
            references: data.references, suggest_actions: data.suggest_actions,
            degraded: data.degraded, traceId,
          });
          renderRoute({ '耗时': `${costMs.toFixed(0)} ms` });
          sendBtn.disabled = false;
        }
      } catch (err) {
        reportError(err, '对话失败');
        pushMessage('assistant', `【请求失败】${err.message}`, { degraded: true });
      } finally {
        sendBtn.disabled = false;
      }
    }

    async function sendStream(payload, message, t0) {
      state.streaming = true;
      const sendBtn = root.querySelector('#chat-send');
      sendBtn.disabled = true;

      let acc = '';
      let holder = { role: 'assistant', text: '', meta: {}, at: new Date().toISOString() };
      state.messages.push(holder);
      renderLog();
      const last = () => log.querySelector('.msg:last-child .msg-bubble');

      try {
        await streamChat(payload, {
          onRoute(evt) {
            state.lastRoute = {
              agent: evt.agent, intent: evt.intent, confidence: evt.confidence,
              route_source: 'server(prefilter)', action: evt.confidence < 0.5 ? 'clarify' : 'dispatch',
            };
            renderRoute({ '首包': `${(performance.now() - t0).toFixed(0)} ms` });
          },
          onMessage(evt) {
            acc += evt.answer || '';
            holder.text = acc;
            const node = last();
            if (node) node.textContent = acc;
            log.scrollTop = log.scrollHeight;
          },
          onEnd(evt) {
            if (evt && evt.conversation_id) state.conversationId = evt.conversation_id;
            state.streaming = false;
            sendBtn.disabled = false;
            renderRoute({ '总耗时': `${(performance.now() - t0).toFixed(0)} ms` });
          },
          onError(err) {
            throw err;
          },
        });
      } catch (err) {
        reportError(err, '流式对话中断');
        if (!holder.text) holder.text = `【中断】${err.message}`;
        renderLog();
      } finally {
        state.streaming = false;
        sendBtn.disabled = false;
      }
    }

    /* ---------- 语音录入 ---------- */
    root.querySelector('#asr-btn').addEventListener('click', () =>
      root.querySelector('#asr-file').click());

    root.querySelector('#asr-file').addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      if (!file) return;
      const purpose = root.querySelector('#asr-purpose').value;
      try {
        toastOk(`正在转写 ${file.name}…`);
        const { data, costMs } = await api.asr(file, purpose);
        pushMessage('assistant',
          `【语音转写 · ${purpose}】\n${data.transcript}\n\n结构化槽位：${JSON.stringify(data.structured || {}, null, 2)}`,
          { agent: 'asr', intent: `asr:${purpose}` });
        if (purpose === 'chat') textarea.value = data.transcript;
        toastOk('转写完成', `provider=${data.provider} · ${costMs.toFixed(0)} ms`);
      } catch (err) {
        reportError(err, '语音转写失败');
      }
    });

    /* ---------- 绑定 ---------- */
    root.querySelector('#chat-send').addEventListener('click', send);
    root.querySelector('#chat-clear').addEventListener('click', () => {
      state.messages = [];
      state.conversationId = null;
      state.lastRoute = null;
      routePanel.innerHTML = emptyBox('尚未发起对话');
      renderLog();
    });
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });

    renderLog();

    // 暴露给「日报 / 请假」视图复用语音录入能力
    ctx.onAsr = (data) => pushMessage('assistant', `【语音转写】\n${data.transcript}`, { agent: 'asr' });

    // 主动待办提醒：只有员工及以上才有待办；不 await（见 loadTodoBanner 的说明）
    if (ctx && ['employee', 'manager', 'admin'].includes(ctx.role)) {
      loadTodoBanner(root);
    }
  },
};
