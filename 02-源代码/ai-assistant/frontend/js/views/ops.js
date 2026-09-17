/**
 * 运营中心：活动与报名 / 员工日报（语音） / 知识库元数据 / 报告生成。
 */
import { api } from '../api.js';
import {
  badge, barChart, card, compact, emptyBox, esc, fmtDate, fmtDateTime, formValues,
  jsonBlock, kvList, loading, openModal, reportError, statCard, statusText, table,
  toastOk, toastErr, toastWarn, today, uid, toInt, closeModal, pick,
} from '../ui.js';

export default {
  key: 'ops',
  title: '运营中心',
  desc: '活动报名 · 日报（含语音） · 知识库元数据 · 报告产出',
  roles: ['employee', 'manager', 'admin'],

  async mount(root, ctx) {
    const isManager = ctx.role === 'manager' || ctx.role === 'admin';

    root.innerHTML = `
      <div class="grid grid-4" id="op-stats"></div>

      <div class="card" style="margin-top:16px">
        <div class="card-head">
          <div><h2>活动运营</h2><div class="card-sub">报名人数上限校验 + 重复报名冲突拦截</div></div>
          <div><button class="btn btn-primary btn-sm" id="ac-new">创建活动</button></div>
        </div>
        <div class="card-body" id="ac-list">${loading()}</div>
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-head">
            <div><h2>提交日报</h2><div class="card-sub">同一员工同一天重复提交会覆盖，不新增记录</div></div>
          </div>
          <div class="card-body">
            <form id="dr-form">
              <label class="field"><span>员工 ID *</span><input name="employee_id" type="number" required value="1"></label>
              <label class="field"><span>日报日期</span><input name="report_date" type="date" value="${today()}"></label>
              <label class="field"><span>日报内容 *</span>
                <textarea name="content" required placeholder="例：今日回访意向客户 6 组，签约 1 单…"></textarea></label>
              <label class="field"><span>来源</span>
                <select name="source"><option value="manual">手动录入</option><option value="voice">语音录入</option></select></label>
              <div class="chat-actions">
                <input type="file" id="dr-audio" accept="audio/*" hidden>
                <button type="button" class="btn btn-sm" id="dr-asr">语音转写填入</button>
                <span class="grow"></span>
                <button class="btn btn-primary" type="submit">提交日报</button>
              </div>
            </form>
          </div>
        </div>

        <div class="card">
          <div class="card-head">
            <div><h2>日报汇总</h2><div class="card-sub" id="dr-date"></div></div>
            <div><button class="btn btn-sm" id="dr-refresh">刷新</button></div>
          </div>
          <div class="card-body">
            <div class="grid grid-4" id="dr-stats" style="margin-bottom:14px"></div>
            <div id="dr-trend">${loading()}</div>
            <div id="dr-list" style="margin-top:14px">${loading()}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>知识库元数据</h2><div class="card-sub">真实切片存放于 Dify 数据集，这里登记映射关系</div></div>
          <div><button class="btn btn-primary btn-sm" id="kb-new">登记文档</button></div>
        </div>
        <div class="card-body" id="kb-list">${loading()}</div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>主动待办与预警触达</h2>
            <div class="card-sub">定时任务与手动触发走同一条逻辑；同类提醒有频控，不会重复轰炸同一个人</div></div>
          <div>
            <button class="btn btn-sm" id="td-refresh">刷新</button>
            <button class="btn btn-sm" id="td-push">立刻推送一轮</button>
            ${isManager ? '<button class="btn btn-sm btn-primary" id="td-notify">触达未提醒预警</button>' : ''}
          </div>
        </div>
        <div class="card-body">
          <div id="td-pending">${loading()}</div>
          <div class="card-sub" style="margin:18px 0 8px">推送记录（留痕 + 频控依据）</div>
          <div id="td-pushes">${loading()}</div>
        </div>
      </div>

      ${isManager ? `
      <div class="card">
        <div class="card-head">
          <div><h2>报告生成（五类口径）</h2>
            <div class="card-sub">真实数据聚合 + 在线查阅 + PDF/Excel 导出；定时任务每天生成日报类、周一生成周报类</div></div>
          <div>
            <button class="btn btn-sm" id="rp-sched">运行定时报告</button>
            <button class="btn btn-primary btn-sm" id="rp-new">生成报告</button>
          </div>
        </div>
        <div class="card-body" id="rp-list">${loading()}</div>
      </div>` : ''}`;

    /* ================= 活动 ================= */
    async function reloadActivities() {
      const box = root.querySelector('#ac-list');
      box.innerHTML = loading();
      try {
        const { data } = await api.listActivities({});
        const open = data.items.filter((a) => a.status === 'OPEN').length;
        const seats = data.items.reduce((s, a) => s + (a.enrolled_count || 0), 0);
        root.querySelector('#op-stats').innerHTML = [
          statCard('活动总数', data.total),
          statCard('进行中', open),
          statCard('累计报名', seats),
          statCard('平均报名', data.items.length ? (seats / data.items.length).toFixed(1) : 0),
        ].join('');

        box.innerHTML = table([
          { title: 'ID', key: 'id' },
          { title: '标题', render: (r) => `<strong>${esc(r.title)}</strong>` },
          { title: '分类', key: 'category' },
          { title: '开始时间', render: (r) => esc(fmtDateTime(r.start_at)) },
          { title: '地点', key: 'location' },
          { title: '报名/容量', render: (r) => `${r.enrolled_count} / ${r.capacity ?? '不限'}` },
          {
            title: '状态',
            render: (r) => badge(r.status, {
              OPEN: '报名中', CLOSED: '已结束', CANCELLED: '已取消',
            }[r.status]),
          },
          {
            title: '操作',
            render: (r) => `<button class="btn btn-sm" data-en="${r.id}">报名</button>
                            <button class="btn btn-sm" data-enr="${r.id}">名单</button>`,
          },
        ], data.items, { empty: '暂无活动' });

        box.querySelectorAll('[data-en]').forEach((b) =>
          b.addEventListener('click', () => enroll(Number(b.dataset.en))));
        box.querySelectorAll('[data-enr]').forEach((b) =>
          b.addEventListener('click', () => showEnrollments(Number(b.dataset.enr))));
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '活动列表');
      }
    }

    root.querySelector('#ac-new').addEventListener('click', () => {
      const h = openModal({
        title: '创建活动',
        body: `
          <form id="ac-form">
            <label class="field"><span>标题 *</span><input name="title" required></label>
            <label class="field"><span>分类</span><input name="category" placeholder="讲座 / 沙龙 / 展会"></label>
            <label class="field"><span>开始时间</span><input name="start_at" type="datetime-local"></label>
            <label class="field"><span>结束时间</span><input name="end_at" type="datetime-local"></label>
            <label class="field"><span>地点</span><input name="location"></label>
            <label class="field"><span>容量</span><input name="capacity" type="number" min="1"></label>
            <label class="field"><span>简介</span><textarea name="description"></textarea></label>
          </form>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="ac-save">创建</button>`,
      });
      h.root.querySelector('#ac-save').addEventListener('click', async () => {
        const form = h.root.querySelector('#ac-form');
        if (!form.reportValidity()) return;
        const raw = formValues(form);
        try {
          const { data } = await api.createActivity(compact({
            title: raw.title, category: raw.category, description: raw.description,
            start_at: raw.start_at, end_at: raw.end_at, location: raw.location,
            capacity: toInt(raw.capacity), status: 'OPEN',
          }));
          toastOk(`活动已创建 #${data.id}`);
          closeModal();
          reloadActivities();
        } catch (err) {
          reportError(err, '创建活动失败');
        }
      });
    });

    function enroll(activityId) {
      const h = openModal({
        title: `活动报名 #${activityId}`,
        width: '420px',
        body: `
          <p class="field-hint">员工代报名时需指定学员或意向客户；学员本人登录则自动用自己档案。</p>
          <label class="field"><span>学员 ID</span><input id="en-stu" type="number"></label>
          <label class="field"><span>意向客户 ID</span><input id="en-lead" type="number"></label>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="en-save">报名</button>`,
      });
      h.root.querySelector('#en-save').addEventListener('click', async () => {
        try {
          const { data } = await api.enrollActivity(activityId, compact({
            student_id: toInt(h.root.querySelector('#en-stu').value),
            lead_id: toInt(h.root.querySelector('#en-lead').value),
          }));
          toastOk(`报名成功（当前 ${data.enrolled_count} 人）`);
          closeModal();
          reloadActivities();
        } catch (err) {
          reportError(err, '报名失败');
        }
      });
    }

    async function showEnrollments(activityId) {
      const h = openModal({ title: `报名名单 #${activityId}`, width: '560px', body: loading() });
      try {
        const { data } = await api.listEnrollments(activityId);
        h.body.innerHTML = table([
          { title: 'ID', key: 'id' },
          { title: '学员', key: 'student_id' },
          { title: '客户', key: 'lead_id' },
          { title: '状态', render: (r) => badge(r.status) },
          { title: '报名时间', render: (r) => esc(fmtDateTime(r.enrolled_at)) },
        ], data.items, { empty: '还没有人报名' });
      } catch (err) {
        h.body.innerHTML = emptyBox('加载失败');
        reportError(err, '报名名单');
      }
    }

    /* ================= 日报 ================= */
    async function reloadDaily() {
      try {
        const [{ data: sum }, { data: trend }] = await Promise.all([
          api.dailySummary(null), api.dailyTrend(7),
        ]);
        root.querySelector('#dr-date').textContent = `统计日：${sum.date}`;
        root.querySelector('#dr-stats').innerHTML = [
          statCard('在岗员工', sum.total_employees),
          statCard('已提交', sum.submitted),
          statCard('未提交', sum.missing.length),
          statCard('语音录入', sum.voice_sourced),
        ].join('');
        root.querySelector('#dr-trend').innerHTML = barChart(
          trend.series.map((s) => ({ label: s.date.slice(5), value: s.count })));
        root.querySelector('#dr-list').innerHTML = table([
          { title: '员工', key: 'employee_id' },
          { title: '摘要', cls: 'wrap', render: (r) => esc(r.summary) },
          {
            title: '来源',
            render: (r) => `<span class="badge ${r.source === 'voice' ? 'badge-info' : 'badge-neutral'}">${r.source === 'voice' ? '语音' : '手动'}</span>`,
          },
        ], sum.items, { empty: '今日还没有日报' });
      } catch (err) {
        reportError(err, '日报加载');
      }
    }

    root.querySelector('#dr-refresh').addEventListener('click', reloadDaily);
    root.querySelector('#dr-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const raw = formValues(e.target);
      try {
        const { data } = await api.submitDailyReport(compact({
          employee_id: toInt(raw.employee_id),
          report_date: raw.report_date,
          content: raw.content,
          source: raw.source,
        }));
        toastOk(data.updated ? `日报已覆盖更新（${data.report_date}）` : `日报已提交（${data.report_date}）`);
        e.target.querySelector('textarea').value = '';
        reloadDaily();
      } catch (err) {
        reportError(err, '日报提交失败');
      }
    });

    root.querySelector('#dr-asr').addEventListener('click', () =>
      root.querySelector('#dr-audio').click());
    root.querySelector('#dr-audio').addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = '';
      if (!file) return;
      try {
        toastOk(`正在转写 ${file.name}…`);
        const { data } = await api.asr(file, 'daily_report');
        const form = root.querySelector('#dr-form');
        form.querySelector('[name="content"]').value = data.transcript;
        form.querySelector('[name="source"]').value = 'voice';
        toastOk('转写完成，槽位已填入', JSON.stringify(data.structured || {}));
      } catch (err) {
        reportError(err, '语音转写失败');
      }
    });

    /* ================= 知识库 ================= */
    async function reloadKb() {
      const box = root.querySelector('#kb-list');
      try {
        const { data } = await api.listKbDocuments({});
        box.innerHTML = table([
          { title: 'ID', key: 'id' },
          { title: '标题', render: (r) => `<strong>${esc(r.title)}</strong>` },
          { title: '分类', key: 'category' },
          { title: '版本', key: 'version' },
          { title: '切片数', key: 'chunk_count' },
          { title: '状态', render: (r) => badge(r.status) },
        ], data.items, { empty: '暂无知识文档' });
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '知识库列表');
      }
    }

    root.querySelector('#kb-new').addEventListener('click', () => {
      const h = openModal({
        title: '登记知识文档',
        body: `
          <form id="kb-form">
            <label class="field"><span>标题 *</span><input name="title" required></label>
            <label class="field"><span>分类</span><input name="category" placeholder="签证政策 / 院校库 / 服务流程"></label>
            <label class="field"><span>版本</span><input name="version" value="V1.0"></label>
            <label class="field"><span>来源 URL</span><input name="source_url"></label>
            <label class="field"><span>Dify 数据集 ID</span><input name="dify_dataset_id" placeholder="填写后状态变为 INDEXED"></label>
            <label class="field"><span>切片数</span><input name="chunk_count" type="number" value="0"></label>
          </form>`,
        footer: `<button class="btn" data-modal-close>取消</button>
                 <button class="btn btn-primary" id="kb-save">保存</button>`,
      });
      h.root.querySelector('#kb-save').addEventListener('click', async () => {
        const form = h.root.querySelector('#kb-form');
        if (!form.reportValidity()) return;
        const raw = formValues(form);
        try {
          const { data } = await api.createKbDocument(compact({
            title: raw.title, category: raw.category, version: raw.version || 'V1.0',
            source_url: raw.source_url, dify_dataset_id: raw.dify_dataset_id,
            chunk_count: toInt(raw.chunk_count, 0),
          }));
          toastOk(`文档已登记 #${data.id}（${statusText(data.status)}）`);
          closeModal();
          reloadKb();
        } catch (err) {
          reportError(err, '登记失败');
        }
      });
    });

    /* ================= 报告（管理层）：五类口径 + 在线查阅 + 导出 ================= */
    const REPORT_TYPE_LABEL = {
      customer_ops: '全域客户经营分析报告',
      daily_digest: '员工日报汇总报告（日）',
      weekly_digest: '员工日报汇总报告（周）',
      mental_weekly: '学生心理健康周报',
      complaint_weekly: '投诉处理周报',
    };

    // 报告列表的视图状态：来源筛选（''=全部 / scheduled / manual）与已加载条数。
    // 放在闭包里而不是 DOM 上 —— 筛选与「加载更多」都要在重渲染后保持住。
    const REPORT_PAGE = 20;
    let reportsFilter = '';
    let reportsShown = 0;

    /** 导出/预览报告。下载接口要 Bearer 鉴权，所以走 blob，不能裸开新标签。 */
    async function exportReport(id, format) {
      try {
        if (format === 'print') {
          const url = await api.reportExportBlobUrl(id, 'print');
          window.open(url, '_blank');
          setTimeout(() => URL.revokeObjectURL(url), 120000);
          reloadReports();
          return;
        }
        const { url, filename } = await api.reportExportBlobUrl(id, format, true);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
        toastOk(`已导出 ${format.toUpperCase()}`);
        reloadReports();
      } catch (err) {
        reportError(err, '导出失败');
      }
    }

    async function reloadReports() {
      const box = root.querySelector('#rp-list');
      if (!box) return;
      box.innerHTML = loading();
      try {
        // 报告多起来之后「只看最新 50 条」是够不到老数据的（定时报告尤其容易被
        // 人工报告挤出去），所以这里带来源筛选 + 分页：筛选走服务端，翻页累加。
        const query = { limit: REPORT_PAGE, offset: reportsShown };
        if (reportsFilter) query.from_schedule = reportsFilter === 'scheduled';
        const { data } = await api.listReports(query);
        reportsShown += (data.items || []).length;

        const chips = [
          ['', '全部'],
          ['scheduled', '只看定时'],
          ['manual', '只看人工'],
        ].map(([key, label]) => {
          const active = (reportsFilter || '') === key;
          return `<button class="btn btn-sm ${active ? 'btn-primary' : ''}"
            id="rp-filter-${key || 'all'}" data-rpfilter="${key}">${label}</button>`;
        }).join(' ');
        const more = reportsShown < data.total
          ? `<button class="btn btn-sm" id="rp-more">加载更多（已显示 ${reportsShown}/${data.total}）</button>`
          : '';

        box.innerHTML = `
          <div class="card-sub" style="margin-bottom:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span>共 ${data.total} 份（定时 ${data.scheduled_total} 份）· 五类口径见「生成报告」下拉</span>
            <span style="margin-left:auto;display:flex;gap:6px">${chips} ${more}</span>
          </div>
          ${table([
          { title: 'ID', key: 'id' },
          {
            title: '报告',
            render: (r) => `<strong>${esc(r.title)}</strong>
              <div class="card-sub">${esc(REPORT_TYPE_LABEL[r.report_type] || r.report_type)}</div>`,
          },
          { title: '报告期', render: (r) => (r.period ? esc(r.period.label) : '—') },
          {
            title: '来源',
            render: (r) => (r.from_schedule
              ? '<span class="badge badge-info">定时</span>'
              : '<span class="badge badge-neutral">人工</span>'),
          },
          { title: '结论摘要', render: (r) => esc((r.summary || '').slice(0, 40)) },
          { title: '状态', render: (r) => badge(r.status) },
          {
            title: '已导出',
            render: (r) => ((r.exports || []).length
              ? `<span class="card-sub">${esc(r.exports.join(' / '))}</span>`
              : '<span class="card-sub">—</span>'),
          },
          {
            title: '操作',
            render: (r) => `<button class="btn btn-sm" data-rp="${r.id}">查阅</button>
              <button class="btn btn-sm" data-rpex="${r.id}" data-fmt="xlsx">Excel</button>
              <button class="btn btn-sm" data-rpex="${r.id}" data-fmt="print">打印版 PDF</button>`,
          },
        ], data.items, { empty: '暂无报告，点右上角「生成报告」' })}`;

        box.querySelectorAll('[data-rpfilter]').forEach((b) =>
          b.addEventListener('click', () => {
            reportsFilter = b.dataset.rpfilter || '';
            reportsShown = 0;
            reloadReports();
          }));
        const moreBtn = box.querySelector('#rp-more');
        if (moreBtn) moreBtn.addEventListener('click', () => reloadReports());
        box.querySelectorAll('[data-rp]').forEach((b) =>
          b.addEventListener('click', () => showReport(Number(b.dataset.rp))));
        box.querySelectorAll('[data-rpex]').forEach((b) =>
          b.addEventListener('click', () => exportReport(Number(b.dataset.rpex), b.dataset.fmt)));
      } catch (err) {
        box.innerHTML = emptyBox('报告列表加载失败');
        reportError(err, '报告列表');
      }
    }

    const newBtn = root.querySelector('#rp-new');
    if (newBtn) {
      newBtn.addEventListener('click', async () => {
        let types = Object.entries(REPORT_TYPE_LABEL);
        try {
          const { data } = await api.reportTypes();
          types = data.items.map((t) => [t.key, t.title]);
        } catch { /* 拿不到就退回内置表 */ }

        const h = openModal({
          title: '生成业务报告（五类口径）',
          width: '620px',
          body: `
            <form id="rp-form">
              <label class="field"><span>报告类型 *</span>
                <select name="report_type">${types.map(([key, label]) =>
                  `<option value="${key}">${esc(label)}</option>`).join('')}</select></label>
              <div class="grid grid-2">
                <label class="field"><span>报告期起（可空）</span>
                  <input name="period_start" type="date"></label>
                <label class="field"><span>报告期止（可空）</span>
                  <input name="period_end" type="date"></label>
              </div>
              <label class="field"><span>自定义标题（可空）</span>
                <input name="title" placeholder="例：第 37 周经营分析"></label>
              <div class="card-sub">
                报告由后端先做真实数据聚合（意向/成交/流失、日报、预警、投诉…），再接 Dify 生成叙述；
                同一份快照供给在线查阅与导出，数字不会漂。
              </div>
            </form>`,
          footer: `<button class="btn" data-modal-close>取消</button>
                   <button class="btn btn-primary" id="rp-save">生成</button>`,
        });
        h.root.querySelector('#rp-save').addEventListener('click', async () => {
          const form = h.root.querySelector('#rp-form');
          if (!form.reportValidity()) return;
          const raw = formValues(form);
          try {
            const { data, costMs } = await api.generateReport(compact({
              report_type: raw.report_type,
              title: raw.title,
              params: compact({
                period_start: raw.period_start, period_end: raw.period_end,
              }),
            }));
            toastOk(`报告 #${data.id} 已生成（${data.period.label}）`, `${costMs.toFixed(0)} ms`);
            closeModal();
            reloadReports();
            showReport(data.id);
          } catch (err) {
            reportError(err, '报告生成失败');
          }
        });
      });
    }

    const schedBtn = root.querySelector('#rp-sched');
    if (schedBtn) {
      schedBtn.addEventListener('click', async () => {
        try {
          const { data } = await api.runScheduledReports({ force: 'true' });
          toastOk(data.created_count
            ? `定时任务生成 ${data.created_count} 份报告`
            : `本轮无需生成（已存在 ${data.skipped_count} 份，幂等跳过）`);
          reloadReports();
        } catch (err) {
          reportError(err, '定时生成失败');
        }
      });
    }

    async function showReport(id) {
      const h = openModal({ title: `报告详情 #${id}`, width: '860px', body: loading() });
      try {
        const { data } = await api.getReport(id);
        const snap = data.snapshot || {};
        const metrics = snap.metrics || [];
        h.body.innerHTML = `
          ${kvList([
            ['标题', data.title],
            ['口径类型', REPORT_TYPE_LABEL[data.report_type] || data.report_type],
            ['报告期', snap.period ? `${snap.period.label}（${snap.period.days} 天）` : '—'],
            ['来源', data.from_schedule ? '定时任务' : '人工触发'],
            ['状态', statusText(data.status)],
            ['生成时间', fmtDateTime(data.created_at)],
          ])}
          <div class="card-sub" style="margin:12px 0 6px">结论</div>
          <div class="todo-item">${esc(snap.summary || '—')}</div>

          <div class="card-sub" style="margin:14px 0 6px">核心指标（${metrics.length}）</div>
          <div class="chips">${metrics.map((m) => `<span class="chip" title="${esc(m.hint || '')}">
            <b>${esc(m.label)}</b>${esc(String(m.value))}${m.delta ? ` · ${esc(m.delta)}` : ''}</span>`).join('')}</div>

          ${(snap.sections || []).map((section) => `
            <div class="card-sub" style="margin:14px 0 6px">${esc(section.heading)}</div>
            ${(section.lines || []).map((line) => `<div>${esc(line)}</div>`).join('')}
            ${(section.bullets || []).length
              ? `<ul class="kv">${section.bullets.map((b) => `<li><span class="v">${esc(b)}</span></li>`).join('')}</ul>`
              : ''}`).join('')}

          ${(snap.tables || []).map((tb) => `
            <div class="card-sub" style="margin:14px 0 6px">${esc(tb.title)}</div>
            ${table(tb.columns.map((c) => ({ title: c, key: c })),
              tb.rows.map((row) => {
                const obj = {};
                tb.columns.forEach((c, i) => { obj[c] = row[i]; });
                return obj;
              }), { empty: '暂无数据' })}`).join('')}

          ${(snap.alerts || []).length ? `
            <div class="card-sub" style="margin:14px 0 6px">风险提示</div>
            <ul class="parse-warn">${snap.alerts.map((a) =>
              `<li>${a.level === 'high' ? '【高】' : '【中】'}${esc(a.text)}</li>`).join('')}</ul>` : ''}

          ${(snap.notes || []).length ? `
            <div class="card-sub" style="margin:14px 0 6px">口径说明</div>
            <ul class="parse-warn" style="color:var(--text-muted)">${snap.notes.map((n) =>
              `<li>${esc(n)}</li>`).join('')}</ul>` : ''}

          <div class="card-sub" style="margin:16px 0 6px">导出</div>
          <div class="toolbar">
            <button class="btn btn-sm btn-primary" data-mo="print">打印 / 另存 PDF（推荐）</button>
            <button class="btn btn-sm" data-mo="pdf">导出 PDF（服务端直出）</button>
            <button class="btn btn-sm" data-mo="xlsx">导出 Excel</button>
            <button class="btn btn-sm" data-mo="md">导出 Markdown</button>
          </div>
          <div class="card-sub" style="margin-top:8px">
            「打印版」由浏览器用系统字体排版后另存 PDF，中文渲染一定正确；
            「服务端直出 PDF」是矢量文字（可选中、可提取），但用的是 PDF 预定义中文字体、
            不嵌字体文件 —— Chrome/Edge 内置阅读器可能显示异常，Acrobat / WPS 正常。
          </div>
          <div class="card-sub" style="margin-top:10px">Markdown 原文</div>
          <pre class="code">${esc(data.content || '（空）')}</pre>`;
        h.root.querySelectorAll('[data-mo]').forEach((b) =>
          b.addEventListener('click', () => exportReport(id, b.dataset.mo)));
      } catch (err) {
        h.body.innerHTML = emptyBox('报告详情加载失败');
        reportError(err, '报告详情');
      }
    }

    /* ================= 主动待办 / 预警触达 ================= */
    async function reloadTodo() {
      const box = root.querySelector('#td-pending');
      box.innerHTML = loading();
      try {
        const { data } = await api.todoPending();
        if (!data.items.length) {
          box.innerHTML = emptyBox('当前没有待处理事项：申请都审完了、工单都跟进了、研判也都复核过了');
          return;
        }
        box.innerHTML = `
          ${data.items.map((t) => `
            <div class="todo-item${t.severity === 'high' ? ' is-high' : ''}">
              <div class="todo-item-head">
                <span class="badge ${t.severity === 'high' ? 'badge-bad' : 'badge-warn'}">
                  ${t.severity === 'high' ? '高优先' : '待处理'}</span>
                <strong>${esc(t.question)}</strong>
                <span class="badge badge-neutral">${t.count} 条</span>
                <span class="card-sub">最长已挂 ${esc(t.oldest_hours)} 小时</span>
              </div>
              <ul class="kv">${t.items.slice(0, 3).map((i) => `<li>
                <span class="k">${esc(i.label)}</span>
                <span class="v">${esc(i.detail)}</span></li>`).join('')}</ul>
              ${t.count > 3 ? `<div class="card-sub">…… 还有 ${t.count - 3} 条</div>` : ''}
              <div class="card-sub">处理入口：${esc(t.action)}</div>
            </div>`).join('')}
          <div class="card-sub" style="margin-top:10px">
            共 ${data.total} 件待处理 · ${data.category_count} 类 · 生成于 ${esc(fmtDateTime(data.generated_at))}
          </div>`;
      } catch (err) {
        box.innerHTML = emptyBox('待办清单加载失败');
        reportError(err, '待办清单');
      }
    }

    async function reloadPushes() {
      const box = root.querySelector('#td-pushes');
      box.innerHTML = loading();
      try {
        const { data } = await api.todoPushes({ limit: 20 });
        box.innerHTML = `<div class="card-sub" style="margin-bottom:8px">
            共 ${data.total} 条，未处理 ${data.unacknowledged} 条
          </div>${table([
          { title: 'ID', key: 'id' },
          { title: '类别', render: (r) => esc(r.title) },
          { title: '主动询问', render: (r) => esc(r.question) },
          { title: '条数', key: 'item_count' },
          {
            title: '优先级',
            render: (r) => (r.severity === 'high'
              ? '<span class="badge badge-bad">高</span>'
              : '<span class="badge badge-neutral">普通</span>'),
          },
          { title: '推送时间', render: (r) => esc(fmtDateTime(r.delivered_at)) },
          {
            title: '状态',
            render: (r) => (r.acknowledged_at
              ? '<span class="badge badge-ok">已处理</span>'
              : '<span class="badge badge-warn">未处理</span>'),
          },
          {
            title: '操作',
            render: (r) => (r.acknowledged_at ? '—'
              : `<button class="btn btn-sm" data-ack="${r.id}">标记已处理</button>`),
          },
        ], data.items, { empty: '还没有推送记录，点右上角「立刻推送一轮」' })}`;

        box.querySelectorAll('[data-ack]').forEach((b) => b.addEventListener('click', async () => {
          try {
            const { data: got } = await api.ackTodoPush(Number(b.dataset.ack),
              { remark: '运营中心标记已处理' });
            toastOk(got.already ? '这条早就标记过了' : '已标记处理');
            reloadPushes();
          } catch (err) {
            reportError(err, '标记失败');
          }
        }));
      } catch (err) {
        box.innerHTML = emptyBox('推送记录加载失败');
        reportError(err, '推送记录');
      }
    }

    root.querySelector('#td-refresh').addEventListener('click', () => {
      reloadTodo();
      reloadPushes();
    });

    root.querySelector('#td-push').addEventListener('click', async () => {
      try {
        const { data } = await api.todoPush({});
        if (data.pushed) {
          toastOk(`本轮新推 ${data.pushed} 条提醒`
            + (data.alerts_delivered ? `，并触达 ${data.alerts_delivered} 条心理预警` : ''));
        } else {
          toastWarn('这些提醒刚才已经推过一轮了，本次没有新增（频控生效）');
        }
        reloadTodo();
        reloadPushes();
      } catch (err) {
        reportError(err, '推送失败');
      }
    });

    const notifyBtn = root.querySelector('#td-notify');
    if (notifyBtn) {
      notifyBtn.addEventListener('click', async () => {
        notifyBtn.disabled = true;
        try {
          const { data } = await api.notifyAlerts({});
          if (!data.total) {
            toastOk(data.message || '没有需要触达的预警');
          } else {
            toastOk(`已触达 ${data.total} 条心理预警`);
            openModal({
              title: '心理预警触达结果',
              width: '720px',
              body: `
                <div class="card-sub">
                  触达会写入 <span class="mono">notified_at / notified_to</span>，
                  并把干预建议一起留痕，避免「通知了但没说该做什么」。
                </div>
                ${data.items.map((i) => `
                  <div class="todo-item${i.risk_level === 'HIGH' ? ' is-high' : ''}">
                    <div class="todo-item-head">
                      <span class="badge ${i.risk_level === 'HIGH' ? 'badge-bad' : 'badge-warn'}">${esc(i.risk_level)}</span>
                      <strong>#${esc(i.id)} ${esc(i.student_name)}</strong>
                      <span class="card-sub">处理人：${esc(i.notified_to_name || '未指定')} · ${esc(i.assignee_note)}</span>
                    </div>
                    <div class="card-sub">触发原因：${esc(i.reason)}</div>
                    <ul class="kv">${i.advice.map((line) => `<li><span class="v">${esc(line)}</span></li>`).join('')}</ul>
                  </div>`).join('')}`,
            });
          }
          reloadTodo();
          reloadPushes();
        } catch (err) {
          reportError(err, '预警触达失败');
        } finally {
          notifyBtn.disabled = false;
        }
      });
    }

    await Promise.all([
      reloadActivities(), reloadDaily(), reloadKb(),
      reloadTodo(), reloadPushes(),
      isManager ? reloadReports() : Promise.resolve(),
    ]);
  },
};
