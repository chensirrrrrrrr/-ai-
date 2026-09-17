/**
 * 客户管理视图：意向客户 / 跟进记录（幂等） / 材料研判（上传解析 → 研判 → 人工复核）。
 *
 * 研判这一块的交互刻意做成「先看见、再研判」：
 *   选文件 → 服务端解析并抽出关键字段 → 界面把解析器 / 字符数 / 缺失字段摆出来
 *   → 用户确认（或改正文）后才发起研判 → 复核环节还能推翻并回写。
 * 之所以不「上传即研判」，是因为 REQ-M1-05 要求结论可追溯到规则与原文片段，
 * 中间这一步正是人工能校正文的地方。
 */
import { api } from '../api.js';
import {
  badge, compact, emptyBox, esc, fmtDateTime, formValues, jsonBlock, kvList,
  loading, openModal, closeModal, reportError, statCard, statusText, table,
  toastOk, toastErr, uid, toInt,
} from '../ui.js';

const STATUS_OPTIONS = [
  ['', '全部状态'], ['NEW', '新线索'], ['FOLLOWING', '跟进中'],
  ['SIGNED', '已签约'], ['LOST', '已流失'],
];

const REVIEW_OPTIONS = [
  ['', '全部复核状态'], ['PENDING', '待复核'],
  ['CONFIRMED', '已确认'], ['OVERRIDDEN', '已推翻'],
];

const REVIEW_TEXT = { PENDING: '待复核', CONFIRMED: '已确认', OVERRIDDEN: '已推翻' };
const REVIEW_KIND = { PENDING: 'badge-warn', CONFIRMED: 'badge-ok', OVERRIDDEN: 'badge-bad' };

function conclusionBadge(value) {
  if (!value) return '<span class="badge badge-neutral">—</span>';
  const kind = value === '符合' ? 'badge-ok' : (value === '不符合' ? 'badge-bad' : 'badge-warn');
  return `<span class="badge ${kind}">${esc(value)}</span>`;
}

function reviewBadge(value) {
  const text = REVIEW_TEXT[value] || value || '—';
  return `<span class="badge ${REVIEW_KIND[value] || 'badge-neutral'}">${esc(text)}</span>`;
}

function chips(rows) {
  if (!rows || !rows.length) return '<div class="card-sub">未抽出任何关键字段</div>';
  return `<div class="chips">${rows.map((r) => `<span class="chip"><b>${esc(r.label)}</b>${esc(r.value)}</span>`).join('')}</div>`;
}

function missingChips(list) {
  if (!list || !list.length) return '<div class="card-sub">关键字段齐全</div>';
  return `<div class="chips">${list.map((m) => `<span class="chip chip-missing" title="${esc(m.hint || '')}">缺 ${esc(m.label)}</span>`).join('')}</div>`;
}

function kb(size) {
  return `${(Number(size || 0) / 1024).toFixed(1)} KB`;
}

export default {
  key: 'crm',
  title: '客户管理',
  desc: '意向客户建档 → 跟进记录（幂等） → 材料研判（多格式解析 + 人工复核）',
  roles: ['employee', 'manager', 'admin'],

  async mount(root, ctx) {
    root.innerHTML = `
      <div class="grid grid-4" id="crm-stats">${
        [1, 2, 3, 4].map(() => statCard('—', '…')).join('')}</div>

      <div class="card" style="margin-top:16px">
        <div class="card-head">
          <div><h2>意向客户</h2><div class="card-sub" id="crm-total"></div></div>
          <div>
            <button class="btn btn-sm" id="crm-pickall">全选本页</button>
            <button class="btn btn-sm" id="crm-batch" disabled>批量研判选中</button>
            <button class="btn btn-primary btn-sm" id="crm-new">新建客户</button>
          </div>
        </div>
        <div class="card-body">
          <div class="toolbar">
            <input class="grow" id="crm-kw" placeholder="按姓名搜索…">
            <select id="crm-status">${STATUS_OPTIONS
              .map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>
            <button class="btn" id="crm-refresh">刷新</button>
          </div>
          <div id="crm-table">${loading()}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>材料研判</h2>
            <div class="card-sub">上传 PDF / Excel / 文本 → 解析抽字段 → Dify 研判 → 人工复核回写</div></div>
          <div>
            <button class="btn btn-sm" id="sc-corr">修正清单</button>
            <button class="btn btn-primary btn-sm" id="sc-batch" disabled>批量发起研判</button>
          </div>
        </div>
        <div class="card-body">
          <div class="grid grid-2">
            <div>
              <label class="field"><span>关联客户 ID（可空）</span>
                <input id="sc-lead" placeholder="例如 1"></label>
              <label class="field"><span>材料文件（PDF / xlsx / csv / txt，可多选）</span>
                <input type="file" id="sc-file" multiple
                       accept=".pdf,.xlsx,.xlsm,.csv,.txt,.md"></label>
              <label class="field"><span>材料正文</span>
                <textarea id="sc-text" placeholder="可直接粘贴，或选文件后自动填充…"></textarea></label>
              <div class="toolbar">
                <button class="btn btn-primary" id="sc-run">发起研判</button>
                <button class="btn" id="sc-clear">清空正文</button>
              </div>
              <div id="sc-parse"></div>
            </div>
            <div>
              <div class="card-sub" style="margin-bottom:8px">研判结果</div>
              <div id="sc-result">${emptyBox('尚无研判结果')}</div>
            </div>
          </div>

          <div class="card-sub" style="margin:18px 0 8px">
            待研判队列 <span class="badge badge-neutral" id="sc-queue-count">0</span>
            <span style="font-weight:400">（多选文件后自动入队，统一批量发起）</span>
          </div>
          <div id="sc-queue"></div>

          <div class="card-sub" style="margin:18px 0 8px">历史研判记录</div>
          <div class="toolbar">
            <select id="sc-filter">${REVIEW_OPTIONS
              .map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select>
            <button class="btn" id="sc-refresh">刷新</button>
          </div>
          <div id="sc-list">${loading()}</div>
        </div>
      </div>`;

    const tableBox = root.querySelector('#crm-table');
    const statsBox = root.querySelector('#crm-stats');

    /** 待研判队列（多文件上传后逐条入队） */
    let queue = [];
    /** 单条研判时记住上一次上传的来源（让 source_type / 原件链接不丢失） */
    let lastUpload = null;
    /** 客户列表勾选（「批量研判选中客户」用） */
    let picked = new Set();
    let pageLeadIds = [];

    /* ===================== 意向客户 ===================== */
    async function reload() {
      tableBox.innerHTML = loading();
      const kw = root.querySelector('#crm-kw').value.trim();
      const status = root.querySelector('#crm-status').value;
      try {
        const { data } = await api.listLeads({ keyword: kw || undefined, status: status || undefined, page_size: 100 });
        root.querySelector('#crm-total').textContent = `共 ${data.total} 条（当前展示 ${data.items.length} 条）`;

        const dist = data.items.reduce((acc, x) => {
          acc[x.status] = (acc[x.status] || 0) + 1; return acc;
        }, {});
        statsBox.innerHTML = [
          statCard('客户总数', data.total),
          statCard('新线索', dist.NEW || 0),
          statCard('跟进中', dist.FOLLOWING || 0),
          statCard('已签约', dist.SIGNED || 0),
        ].join('');

        pageLeadIds = data.items.map((x) => x.id);
        tableBox.innerHTML = table([
          {
            title: '选',
            render: (r) => `<input type="checkbox" data-pick="${r.id}"${
              picked.has(r.id) ? ' checked' : ''}>`,
          },
          { title: 'ID', key: 'id' },
          { title: '姓名', render: (r) => `<strong>${esc(r.name)}</strong>` },
          { title: '手机号', render: (r) => `<span class="mono">${esc(r.phone)}</span>` },
          { title: '来源', key: 'source' },
          { title: '意向国家', key: 'intention_country' },
          { title: '状态', render: (r) => badge(r.status) },
          { title: '负责人', key: 'owner_id' },
          { title: '创建时间', render: (r) => esc(fmtDateTime(r.created_at)) },
          {
            title: '操作',
            render: (r) => `<button class="btn btn-sm" data-detail="${r.id}">详情</button>
                            <button class="btn btn-sm" data-edit="${r.id}">改状态</button>
                            <button class="btn btn-sm" data-screen="${r.id}">研判</button>`,
          },
        ], data.items, { empty: '还没有客户，点右上角「新建客户」' });

        tableBox.querySelectorAll('[data-detail]').forEach((b) =>
          b.addEventListener('click', () => openDetail(Number(b.dataset.detail))));
        tableBox.querySelectorAll('[data-edit]').forEach((b) =>
          b.addEventListener('click', () => openStatus(Number(b.dataset.edit))));
        tableBox.querySelectorAll('[data-screen]').forEach((b) =>
          b.addEventListener('click', () => {
            root.querySelector('#sc-lead').value = String(b.dataset.screen);
            root.querySelector('#sc-text').focus();
            syncBatchButton();
          }));
        tableBox.querySelectorAll('[data-pick]').forEach((box) =>
          box.addEventListener('change', () => {
            const id = Number(box.dataset.pick);
            if (box.checked) picked.add(id); else picked.delete(id);
            syncBatchButton();
          }));
        syncBatchButton();
      } catch (err) {
        tableBox.innerHTML = emptyBox('加载失败');
        reportError(err, '客户列表');
      }
    }

    function syncBatchButton() {
      const btn = root.querySelector('#crm-batch');
      btn.disabled = picked.size === 0;
      btn.textContent = picked.size ? `批量研判选中（${picked.size}）` : '批量研判选中';
    }

    root.querySelector('#crm-pickall').addEventListener('click', () => {
      const allPicked = pageLeadIds.length > 0 && pageLeadIds.every((id) => picked.has(id));
      if (allPicked) pageLeadIds.forEach((id) => picked.delete(id));
      else pageLeadIds.forEach((id) => picked.add(id));
      tableBox.querySelectorAll('[data-pick]').forEach((box) => {
        box.checked = picked.has(Number(box.dataset.pick));
      });
      syncBatchButton();
    });

    root.querySelector('#crm-batch').addEventListener('click', async () => {
      if (!picked.size) return;
      const leadIds = [...picked];
      const out = root.querySelector('#sc-result');
      out.innerHTML = loading(`按客户档案批量研判 ${leadIds.length} 位客户…`);
      try {
        const { data } = await api.batchScreening({ lead_ids: leadIds });
        out.innerHTML = batchResult(data);
        picked = new Set();
        syncBatchButton();
        toastOk(`批量研判完成：成功 ${data.succeeded} / 失败 ${data.failed}`);
        reloadScreenings();
      } catch (err) {
        out.innerHTML = emptyBox('批量研判失败');
        reportError(err, '批量研判');
      }
    });

    /* ===================== 新建 / 改状态（原有能力） ===================== */
    root.querySelector('#crm-new').addEventListener('click', () => {
      const h = openModal({
        title: '新建意向客户',
        body: `
          <form id="lead-form">
            <label class="field"><span>姓名 *</span><input name="name" required maxlength="64"></label>
            <label class="field"><span>手机号 *</span><input name="phone" required maxlength="32" placeholder="重复手机号会被 409 拒绝"></label>
            <label class="field"><span>来源</span><input name="source" placeholder="官网 / 转介绍 / 活动"></label>
            <label class="field"><span>意向国家</span><input name="intention_country" placeholder="英国 / 澳洲 / 中国香港…"></label>
            <label class="field"><span>意向阶段</span><input name="intention_stage" placeholder="了解中 / 比价中 / 待签约"></label>
            <label class="field"><span>备注</span><textarea name="remark"></textarea></label>
          </form>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="lead-save">保存</button>`,
      });
      h.root.querySelector('#lead-save').addEventListener('click', async () => {
        const form = h.root.querySelector('#lead-form');
        if (!form.reportValidity()) return;
        try {
          const { data } = await api.createLead(compact(formValues(form)));
          toastOk(`客户已创建（id=${data.id}，状态 ${statusText(data.status)}）`);
          closeModal();
          reload();
        } catch (err) {
          reportError(err, '创建失败');
        }
      });
    });

    async function openStatus(id) {
      const h = openModal({
        title: `变更客户状态 #${id}`,
        width: '440px',
        body: `
          <label class="field"><span>新状态</span>
            <select id="st-status">${STATUS_OPTIONS.slice(1)
              .map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select></label>
          <label class="field"><span>备注</span><textarea id="st-remark"></textarea></label>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="st-save">提交</button>`,
      });
      h.root.querySelector('#st-save').addEventListener('click', async () => {
        try {
          const body = compact({
            status: h.root.querySelector('#st-status').value,
            remark: h.root.querySelector('#st-remark').value.trim(),
          });
          const { data } = await api.updateLeadStatus(id, body);
          toastOk(`状态已更新：${statusText(data.before)} → ${statusText(data.status)}`);
          closeModal();
          reload();
        } catch (err) {
          reportError(err, '状态变更失败');
        }
      });
    }

    async function openDetail(id) {
      const h = openModal({
        title: `客户详情 #${id}`,
        width: '680px',
        body: loading(),
      });
      try {
        const { data } = await api.getLead(id);
        const lead = data.lead;
        const idemKey = uid('fu');
        h.body.innerHTML = `
          ${kvList([
            ['ID', lead.id], ['姓名', lead.name], ['手机号', lead.phone],
            ['来源', lead.source], ['意向国家', lead.intention_country],
            ['意向阶段', lead.intention_stage], ['状态', statusText(lead.status)],
            ['负责人', lead.owner_id], ['备注', lead.remark],
          ])}

          <div class="card-sub" style="margin:16px 0 8px">新增跟进记录</div>
          <form id="fu-form">
            <label class="field"><span>跟进内容 *</span>
              <textarea name="content" required placeholder="例：电话沟通 15 分钟，客户关注英国 G5 申请门槛"></textarea></label>
            <label class="field"><span>跟进方式</span>
              <select name="follow_type">
                <option value="PHONE">电话</option><option value="WECHAT">微信</option>
                <option value="VISIT">到访</option><option value="EMAIL">邮件</option>
              </select></label>
            <label class="field"><span>下一步计划</span><input name="next_plan"></label>
            <label class="stream-toggle">
              <input type="checkbox" id="fu-idem" checked>
              携带幂等键 <span class="mono">${esc(idemKey)}</span>（再点一次返回同一条）
            </label>
          </form>

          <div class="card-sub" style="margin:16px 0 8px">历史跟进（${data.followups.length}）</div>
          <div id="fu-list">${data.followups.length
            ? `<ul class="kv">${data.followups.map((f) => `<li>
                <span class="k">${esc(fmtDateTime(f.created_at).slice(5, 16))}</span>
                <span class="v">${badge(f.follow_type)} ${esc(f.content)}</span></li>`).join('')}</ul>`
            : emptyBox('暂无跟进记录')}</div>`;

        h.root.querySelector('.modal-body').style.maxHeight = 'none';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-primary';
        saveBtn.textContent = '提交跟进';
        const foot = h.root.querySelector('.modal-foot');
        if (!foot) {
          const div = document.createElement('div');
          div.className = 'modal-foot';
          div.appendChild(saveBtn);
          h.root.querySelector('.modal').appendChild(div);
        } else foot.appendChild(saveBtn);

        saveBtn.addEventListener('click', async () => {
          const form = h.root.querySelector('#fu-form');
          if (!form.reportValidity()) return;
          const useIdem = h.root.querySelector('#fu-idem').checked;
          try {
            const body = compact({
              ...formValues(form),
              idempotency_key: useIdem ? idemKey : null,
            });
            const { data: res } = await api.addFollowup(id, body);
            if (res.duplicated) toastOk(`命中幂等键，返回原记录 #${res.id}`);
            else toastOk(`跟进已记录 #${res.id}`);
            closeModal();
            reload();
          } catch (err) {
            reportError(err, '跟进失败');
          }
        });
      } catch (err) {
        h.body.innerHTML = emptyBox('加载失败');
        reportError(err, '客户详情');
      }
    }

    /* ===================== 材料解析 ===================== */
    function parseBox(data) {
      const warnings = data.warnings || [];
      const fields = data.field_rows || [];
      const missing = data.missing_fields || [];
      return `
        <div class="parse-box">
          <div class="parse-meta">
            <span class="badge badge-info">${esc(data.source_type)}</span>
            <span class="mono">${esc(data.filename)}</span>
            <span>${kb(data.size)}</span>
            <span>${esc(data.chars)} 字 / ${esc(data.units)} ${esc(data.unit_name)}</span>
            <span class="badge badge-neutral" title="实际使用的解析器">${esc(data.parser)}</span>
            ${data.truncated ? '<span class="badge badge-warn">已截断</span>' : ''}
            <button class="btn btn-sm" data-raw="${esc(data.stored_path)}">原件</button>
          </div>
          ${warnings.length ? `<ul class="parse-warn">${warnings
            .map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
          <div class="card-sub" style="margin:10px 0 6px">抽取字段（${fields.length}）</div>
          ${chips(fields)}
          <div class="card-sub" style="margin:10px 0 6px">缺失字段（${missing.length}）</div>
          ${missingChips(missing)}
        </div>`;
    }

    function bindRaw(scope) {
      scope.querySelectorAll('[data-raw]').forEach((b) => b.addEventListener('click', () => {
        openRawFile(b.dataset.raw);
      }));
    }

    /**
     * 打开材料原件。下载接口要求 Bearer 鉴权，`<a href>` / `window.open` 裸开
     * 不会带 Token（会 401），所以先用带鉴权的 fetch 取回 blob 再开本地 URL。
     */
    async function openRawFile(relPath) {
      try {
        const url = await api.screeningFileBlobUrl(relPath);
        window.open(url, '_blank');
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      } catch (err) {
        reportError(err, '打开材料原件');
      }
    }

    function renderQueue() {
      const box = root.querySelector('#sc-queue');
      root.querySelector('#sc-queue-count').textContent = String(queue.length);
      const batch = root.querySelector('#sc-batch');
      batch.disabled = queue.length === 0;
      batch.textContent = queue.length ? `批量发起研判（${queue.length}）` : '批量发起研判';
      if (!queue.length) {
        box.innerHTML = emptyBox('队列为空：上面选材料文件即可自动入队');
        return;
      }
      box.innerHTML = `<div class="queue">${queue.map((it, i) => `
        <div class="queue-item">
          <span class="badge badge-info">${esc(it.source_type)}</span>
          <span class="queue-name" title="${esc(it.name)}">${esc(it.name)}</span>
          <span class="card-sub">${esc(it.chars)} 字 · ${esc(it.parser)}</span>
          <input class="queue-lead" data-qlead="${i}" placeholder="客户ID"
                 value="${it.lead_id == null ? '' : it.lead_id}">
          <button class="btn btn-sm" data-qdrop="${i}">移除</button>
        </div>`).join('')}</div>`;
      box.querySelectorAll('[data-qdrop]').forEach((b) => b.addEventListener('click', () => {
        queue.splice(Number(b.dataset.qdrop), 1);
        renderQueue();
      }));
      box.querySelectorAll('[data-qlead]').forEach((input) => input.addEventListener('change', () => {
        queue[Number(input.dataset.qlead)].lead_id = toInt(input.value);
      }));
    }

    root.querySelector('#sc-file').addEventListener('change', async (ev) => {
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';                 // 允许重复选同一个文件
      if (!files.length) return;

      const box = root.querySelector('#sc-parse');
      const leadId = toInt(root.querySelector('#sc-lead').value);
      box.innerHTML = loading(`正在解析 ${files.length} 份材料…`);
      let last = null;

      for (const file of files) {
        try {
          const { data } = await api.uploadScreeningMaterial(file, leadId);
          last = data;
          queue.push({
            name: data.filename,
            source_type: data.source_type,
            text: data.text,
            raw_file_url: data.raw_file_url,
            lead_id: leadId,
            parser: data.parser,
            chars: data.chars,
          });
          toastOk(`${data.filename} 解析完成（${data.parser}）`);
        } catch (err) {
          reportError(err, `${file.name} 解析失败`);
        }
      }

      if (last) {
        box.innerHTML = parseBox(last);
        bindRaw(box);
        lastUpload = last;
        if (files.length === 1) root.querySelector('#sc-text').value = last.text;
      } else {
        box.innerHTML = emptyBox('没有成功解析的材料');
      }
      renderQueue();
    });

    root.querySelector('#sc-clear').addEventListener('click', () => {
      root.querySelector('#sc-text').value = '';
      lastUpload = null;
      root.querySelector('#sc-parse').innerHTML = '';
    });

    /* ===================== 研判 ===================== */
    function singleResult(d, costMs) {
      return `
        <ul class="kv">
          <li><span class="k">结论</span><span class="v">${conclusionBadge(d.conclusion)}</span></li>
          <li><span class="k">复核状态</span><span class="v">${reviewBadge(d.review_status)}</span></li>
        </ul>
        ${kvList([
          ['研判 ID', `#${d.id}`],
          ['关联客户', d.lead_id],
          ['材料来源', `${d.source_type}${d.source_name ? ` · ${d.source_name}` : ''}`],
          ['置信度', `${(Number(d.confidence) * 100).toFixed(0)}%`],
          ['AI 原结论', d.ai_conclusion],
          ['命中产品', (d.hit_products || []).map((p) => p.name || p).join('、') || '—'],
          ['Dify Run', d.dify_run_id],
          ['耗时', `${Number(costMs || 0).toFixed(0)} ms`],
        ])}
        <div class="card-sub" style="margin:12px 0 6px">缺失字段（${(d.missing_fields || []).length}）</div>
        ${missingChips(d.missing_fields || [])}
        <div class="toolbar" style="margin-top:12px">
          <button class="btn btn-primary btn-sm" data-review="${d.id}" data-action="CONFIRM">确认结论</button>
          <button class="btn btn-sm" data-review="${d.id}" data-action="OVERRIDE">推翻并修正</button>
          <button class="btn btn-sm" data-sdetail="${d.id}">看依据详情</button>
        </div>`;
    }

    function bindResultActions(scope, { detail = true } = {}) {
      scope.querySelectorAll('[data-review]').forEach((b) => b.addEventListener('click',
        () => openReview(Number(b.dataset.review), b.dataset.action)));
      if (!detail) return;
      scope.querySelectorAll('[data-sdetail]').forEach((b) => b.addEventListener('click',
        () => openScreeningDetail(Number(b.dataset.sdetail))));
    }

    function batchResult(d) {
      return `
        ${kvList([
          ['批次', d.batch_id], ['材料总数', d.total],
          ['成功', d.succeeded], ['失败', d.failed],
          ['结论分布', Object.entries(d.by_conclusion || {})
            .map(([k, v]) => `${k}×${v}`).join('、') || '—'],
        ])}
        <div class="card-sub" style="margin:12px 0 6px">研判结果清单</div>
        ${table([
          { title: '#', render: (r) => r.index + 1 },
          { title: '材料', render: (r) => esc(r.source_name || '—') },
          { title: '客户', render: (r) => (r.lead_id == null ? '—' : r.lead_id) },
          {
            title: '结论',
            render: (r) => (r.ok ? conclusionBadge(r.conclusion)
              : '<span class="badge badge-bad">失败</span>'),
          },
          {
            title: '置信度',
            render: (r) => (r.ok ? `${(Number(r.confidence) * 100).toFixed(0)}%` : '—'),
          },
          {
            title: '缺失字段',
            render: (r) => {
              if (!r.ok) return `<span class="mono">${esc(r.error || '')}</span>`;
              return (r.missing_fields || []).length
                ? `<span class="chip chip-missing">缺 ${r.missing_fields.length} 项</span>`
                : '<span class="badge badge-ok">齐全</span>';
            },
          },
          {
            title: '操作',
            render: (r) => (r.ok ? `<button class="btn btn-sm" data-sdetail="${r.id}">复核</button>` : '—'),
          },
        ], d.items, { empty: '无结果' })}`;
    }

    root.querySelector('#sc-run').addEventListener('click', async () => {
      const text = root.querySelector('#sc-text').value.trim();
      if (!text) { toastErr('请填写材料正文，或先选文件自动填充'); return; }
      const out = root.querySelector('#sc-result');
      out.innerHTML = loading('研判中，可能需要几秒…');
      try {
        const { data, costMs } = await api.analyzeScreening(compact({
          lead_id: toInt(root.querySelector('#sc-lead').value),
          source_type: lastUpload ? lastUpload.source_type : 'TEXT',
          text,
          raw_file_url: lastUpload ? lastUpload.raw_file_url : null,
          source_name: lastUpload ? lastUpload.filename : '手工粘贴文本',
          idempotency_key: uid('sc'),
        }));
        out.innerHTML = singleResult(data, costMs);
        bindResultActions(out);
        toastOk('研判完成');
        reloadScreenings();
      } catch (err) {
        out.innerHTML = emptyBox('研判失败');
        reportError(err, '研判失败');
      }
    });

    root.querySelector('#sc-batch').addEventListener('click', async () => {
      if (!queue.length) { toastErr('待研判队列是空的'); return; }
      const out = root.querySelector('#sc-result');
      out.innerHTML = loading(`批量研判 ${queue.length} 条，可能需要几秒…`);
      try {
        const { data } = await api.batchScreening({
          items: queue.map((it) => compact({
            lead_id: it.lead_id,
            source_type: it.source_type,
            text: it.text,
            raw_file_url: it.raw_file_url,
            source_name: it.name,
          })),
        });
        out.innerHTML = batchResult(data);
        bindResultActions(out);
        queue = [];
        renderQueue();
        toastOk(`批量研判完成：成功 ${data.succeeded} / 失败 ${data.failed}`);
        reloadScreenings();
      } catch (err) {
        out.innerHTML = emptyBox('批量研判失败');
        reportError(err, '批量研判');
      }
    });

    /* ===================== 人工复核 ===================== */
    async function openReview(id, action) {
      let row = null;
      try {
        const { data } = await api.getScreening(id);
        row = data;
      } catch (err) {
        reportError(err, '读取研判结果');
        return;
      }
      const override = action === 'OVERRIDE';
      const h = openModal({
        title: `人工复核 · 研判 #${id}`,
        width: '560px',
        body: `
          ${kvList([
            ['关联客户', row.lead_id],
            ['材料来源', `${row.source_type}${row.source_name ? ` · ${row.source_name}` : ''}`],
            ['AI 原结论', row.ai_conclusion], ['当前结论', row.conclusion],
            ['置信度', `${(Number(row.confidence) * 100).toFixed(0)}%`],
            ['复核状态', REVIEW_TEXT[row.review_status] || row.review_status],
          ])}
          <div class="card-sub" style="margin:12px 0 6px">匹配依据</div>
          ${jsonBlock(row.evidence || [])}
          <div class="field" style="margin-top:12px"><span>复核动作</span>
            <select id="rv-action">
              <option value="CONFIRM"${override ? '' : ' selected'}>确认 AI 结论（CONFIRM）</option>
              <option value="OVERRIDE"${override ? ' selected' : ''}>推翻并修正（OVERRIDE）</option>
            </select></div>
          <label class="field" id="rv-conclusion-wrap"><span>修正后的结论 *</span>
            <select id="rv-conclusion">
              <option value="符合">符合</option>
              <option value="不符合">不符合</option>
              <option value="信息不足">信息不足</option>
            </select></label>
          <label class="field"><span>复核意见（会进审计与修正清单）</span>
            <textarea id="rv-remark" maxlength="512"
              placeholder="例：均分 68 低于直申门槛，AI 只按学历命中就判符合，属误判。"></textarea></label>
          <div class="parse-warn" style="list-style:none;padding-left:0">
            修正结果会回写数据库（原结论保留在 ai_conclusion），并进入「修正清单」用于规则优化。
          </div>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="rv-save">提交复核</button>`,
      });

      const actionSel = h.root.querySelector('#rv-action');
      const conclusionWrap = h.root.querySelector('#rv-conclusion-wrap');
      const syncWrap = () => {
        conclusionWrap.style.display = actionSel.value === 'OVERRIDE' ? '' : 'none';
      };
      actionSel.addEventListener('change', syncWrap);
      syncWrap();

      h.root.querySelector('#rv-save').addEventListener('click', async () => {
        const act = actionSel.value;
        try {
          const body = compact({
            action: act,
            conclusion: act === 'OVERRIDE' ? h.root.querySelector('#rv-conclusion').value : null,
            remark: h.root.querySelector('#rv-remark').value.trim(),
          });
          const { data } = await api.reviewScreening(id, body);
          toastOk(data.revised
            ? `已推翻：${data.ai_conclusion} → ${data.conclusion}`
            : `已确认：${data.conclusion}`);
          closeModal();
          reloadScreenings();
        } catch (err) {
          reportError(err, '复核失败');
        }
      });
    }

    async function openScreeningDetail(id) {
      const h = openModal({ title: `研判详情 #${id}`, width: '620px', body: loading() });
      try {
        const { data } = await api.getScreening(id);
        h.body.innerHTML = `
          ${singleResult(data, 0)}
          <div class="card-sub" style="margin:12px 0 6px">抽取字段</div>
          ${chips(Object.entries(data.extracted_fields || {})
            .map(([k, v]) => ({ label: k, value: typeof v === 'object' ? JSON.stringify(v) : v })))}
          <div class="card-sub" style="margin:12px 0 6px">匹配依据（引用规则条目 + 原文片段）</div>
          ${jsonBlock(data.evidence || [])}
          ${data.raw_file_url
            ? '<div style="margin-top:10px"><button class="btn btn-sm" id="rv-raw">下载材料原件</button></div>' : ''}
          ${data.review_remark
            ? `<div class="card-sub" style="margin-top:10px">复核意见：${esc(data.review_remark)}</div>` : ''}`;
        bindResultActions(h.root, { detail: false });
        const raw = h.root.querySelector('#rv-raw');
        if (raw) raw.addEventListener('click', () => openRawFile(
          String(data.raw_file_url).replace('/api/v1/screening/files/', '')));
      } catch (err) {
        h.body.innerHTML = emptyBox('加载失败');
        reportError(err, '研判详情');
      }
    }

    root.querySelector('#sc-corr').addEventListener('click', async () => {
      const h = openModal({ title: '人工修正清单（规则优化素材）', width: '760px', body: loading() });
      try {
        const { data } = await api.screeningCorrections({ limit: 50 });
        h.body.innerHTML = `
          <div class="card-sub">被推翻的研判共 ${data.total} 条 —— 这就是下一版《用户画像研判规则》该先改的地方。</div>
          <div class="card-sub" style="margin:10px 0 6px">AI 结论 → 人工结论</div>
          ${Object.keys(data.by_transition || {}).length
            ? `<div class="chips">${Object.entries(data.by_transition)
                .map(([k, v]) => `<span class="chip chip-missing">${esc(k)} ×${esc(v)}</span>`).join('')}</div>`
            : emptyBox('暂无人工修正记录')}
          <div class="card-sub" style="margin:10px 0 6px">高频缺失字段</div>
          ${Object.keys(data.frequent_missing_fields || {}).length
            ? `<div class="chips">${Object.entries(data.frequent_missing_fields)
                .map(([k, v]) => `<span class="chip">${esc(k)} ×${esc(v)}</span>`).join('')}</div>`
            : '<div class="card-sub">—</div>'}
          <div class="card-sub" style="margin:12px 0 6px">明细</div>
          ${table([
            { title: 'ID', key: 'id' },
            { title: '客户', render: (r) => (r.lead_id == null ? '—' : r.lead_id) },
            { title: '材料', render: (r) => esc(r.source_name || r.source_type) },
            { title: 'AI 原结论', render: (r) => conclusionBadge(r.ai_conclusion) },
            { title: '人工结论', render: (r) => conclusionBadge(r.conclusion) },
            { title: '复核意见', render: (r) => esc(r.remark || '—') },
            { title: '复核时间', render: (r) => esc(fmtDateTime(r.reviewed_at)) },
          ], data.items, { empty: '暂无人工修正记录' })}`;
      } catch (err) {
        h.body.innerHTML = emptyBox('加载失败');
        reportError(err, '修正清单');
      }
    });

    /* ===================== 历史记录 ===================== */
    async function reloadScreenings() {
      const box = root.querySelector('#sc-list');
      box.innerHTML = loading();
      const reviewStatus = root.querySelector('#sc-filter').value;
      try {
        const { data } = await api.listScreenings({
          limit: 20, review_status: reviewStatus || undefined,
        });
        const pending = (data.by_review_status || {}).PENDING || 0;
        box.innerHTML = `
          <div class="card-sub" style="margin-bottom:8px">
            共 ${data.total} 条 · 待复核 ${pending} 条 ·
            结论分布 ${Object.entries(data.by_conclusion || {})
              .map(([k, v]) => `${k}×${v}`).join('、') || '—'}
          </div>
          ${table([
            { title: 'ID', key: 'id' },
            { title: '客户', render: (r) => (r.lead_id == null ? '—' : r.lead_id) },
            {
              title: '材料',
              render: (r) => `${esc(r.source_type)}<div class="card-sub">${esc(r.source_name || '手工文本')}</div>`,
            },
            { title: '结论', render: (r) => conclusionBadge(r.conclusion) },
            {
              title: 'AI 原结论',
              render: (r) => (r.revised ? conclusionBadge(r.ai_conclusion) : '<span class="card-sub">—</span>'),
            },
            { title: '复核状态', render: (r) => reviewBadge(r.review_status) },
            {
              title: '缺失',
              render: (r) => ((r.missing_fields || []).length
                ? `<span class="chip chip-missing">${r.missing_fields.length} 项</span>`
                : '<span class="badge badge-ok">齐全</span>'),
            },
            { title: '置信度', render: (r) => `${(Number(r.confidence) * 100).toFixed(0)}%` },
            {
              title: '操作',
              render: (r) => `<button class="btn btn-sm" data-sdetail="${r.id}">详情</button>
                              <button class="btn btn-sm" data-review="${r.id}" data-action="CONFIRM">复核</button>`,
            },
          ], data.items, { empty: '暂无研判记录' })}`;
        bindResultActions(box);
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '研判列表');
      }
    }

    /* ===================== 绑定 ===================== */
    root.querySelector('#crm-refresh').addEventListener('click', reload);
    root.querySelector('#crm-status').addEventListener('change', reload);
    root.querySelector('#sc-refresh').addEventListener('click', reloadScreenings);
    root.querySelector('#sc-filter').addEventListener('change', reloadScreenings);
    let timer;
    root.querySelector('#crm-kw').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(reload, 320);
    });

    renderQueue();
    await Promise.all([reload(), reloadScreenings()]);
  },
};
