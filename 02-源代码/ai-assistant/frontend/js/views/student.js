/**
 * 学员中心视图。
 * - 学员角色：只看自己的档案 / 进度 / 成绩 / 申请 / 反馈
 * - 员工及以上：学员花名册、行政申请审批、售后工单、心理预警
 */
import { api } from '../api.js';
import {
  badge, card, compact, confirmDialog, emptyBox, esc, fmtDate, fmtDateTime, formValues,
  jsonBlock, kvList, loading, openModal, reportError, statCard, statusText, table,
  toastOk, toastErr, today, uid, toInt, toFloat, closeModal, pick,
} from '../ui.js';

const STAGE_LABELS = {
  PREPARING: '材料准备', APPLYING: '申请中', OFFERED: '已获录取',
  VISA: '签证阶段', ENROLLED: '已入读',
};

const REQ_TYPES = [['LEAVE', '请假'], ['EXAM', '考务'], ['OTHER', '其他']];

function timelineHTML(timeline) {
  return `<ul class="timeline">${timeline.map((t) => `
    <li class="${t.done ? 'done' : ''} ${t.current ? 'current' : ''}">
      <span class="node"></span>
      <span>${esc(STAGE_LABELS[t.stage] || t.stage)}</span>
    </li>`).join('')}</ul>`;
}

export default {
  key: 'student',
  title: '学员中心',
  desc: '档案 · 申请进度 · 成绩 · 请假审批 · 工单 · 心理预警',
  roles: ['student', 'employee', 'manager', 'admin'],

  async mount(root, ctx) {
    const isStudent = ctx.role === 'student';
    const isManager = ctx.role === 'manager' || ctx.role === 'admin';

    root.innerHTML = isStudent ? studentShell() : staffShell(isManager);

    if (isStudent) await mountStudent(root, ctx);
    else await mountStaff(root, ctx, isManager);
  },
};

/* ======================================================================== */
/* 学员视角                                                                  */
/* ======================================================================== */
function studentShell() {
  return `
    <div class="grid grid-4" id="my-stats"></div>

    <div class="grid grid-2" style="margin-top:16px">
      <div class="card">
        <div class="card-head"><h2>我的档案</h2></div>
        <div class="card-body" id="my-profile">${loading()}</div>
      </div>
      <div class="card">
        <div class="card-head"><h2>申请进度</h2></div>
        <div class="card-body" id="my-progress">${loading()}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>我的成绩</h2></div>
      <div class="card-body" id="my-scores">${loading()}</div>
    </div>

    <div class="grid grid-2">
      <div class="card">
        <div class="card-head">
          <div><h2>提交行政申请</h2><div class="card-sub">请假 / 考务，支持幂等键防重复提交</div></div>
        </div>
        <div class="card-body">
          <form id="lv-form">
            <label class="field"><span>类型</span>
              <select name="request_type">${REQ_TYPES
                .map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select></label>
            <label class="field"><span>开始日期</span><input type="date" name="start_date" value="${today()}"></label>
            <label class="field"><span>天数</span><input type="number" name="days" min="1" max="180" value="1"></label>
            <label class="field"><span>事由</span><textarea name="reason" placeholder="例：家中有事，需回老家三天"></textarea></label>
            <label class="stream-toggle"><input type="checkbox" id="lv-idem" checked> 携带幂等键</label>
            <button class="btn btn-primary btn-block" type="submit" style="margin-top:12px">提交申请</button>
          </form>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>我的反馈工单</h2><div class="card-sub">课程 / 服务 / 住宿等售后问题</div></div>
        </div>
        <div class="card-body">
          <form id="tk-form">
            <label class="field"><span>问题描述 *</span>
              <textarea name="content" required placeholder="尽量描述清楚时间、地点、涉及人员…"></textarea></label>
            <label class="field"><span>分类</span>
              <select name="category">
                <option value="课程">课程</option><option value="服务">服务</option>
                <option value="住宿">住宿</option><option value="其他">其他</option>
              </select></label>
            <button class="btn btn-primary btn-block" type="submit">提交工单</button>
          </form>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>我的申请记录</h2></div>
      <div class="card-body" id="my-requests">${loading()}</div>
    </div>`;
}

async function mountStudent(root, ctx) {
  // /auth/me 拿 ref_id 作为学生 id
  let me;
  try {
    const { data } = await api.me();
    me = data;
  } catch (err) {
    reportError(err, '获取身份失败');
    return;
  }
  const sid = me.ref_id;
  if (!sid) {
    root.innerHTML = emptyBox('当前账号未绑定学员档案（sys_account.ref_id 为空）');
    return;
  }

  async function reloadProfile() {
    try {
      const [{ data: stu }, { data: prog }] = await Promise.all([
        api.getStudent(sid), api.studentProgress(sid),
      ]);
      root.querySelector('#my-profile').innerHTML = kvList([
        ['学号', stu.student_no], ['姓名', stu.name], ['意向国家', stu.country_target],
        ['申请层次', stu.program_level], ['当前阶段', STAGE_LABELS[stu.stage] || stu.stage],
        ['顾问 ID', stu.advisor_id], ['入学日期', fmtDate(stu.enroll_date)],
        ['证件号', stu.id_card_masked],
      ]);
      root.querySelector('#my-progress').innerHTML =
        timelineHTML(prog.timeline)
        + `<p class="field-hint" style="margin-top:12px">
             成绩记录 ${prog.score_records} 条 · 待审批申请 ${prog.pending_requests} 件</p>`;
      root.querySelector('#my-stats').innerHTML = [
        statCard('当前阶段', STAGE_LABELS[stu.stage] || stu.stage),
        statCard('成绩记录', prog.score_records),
        statCard('待审批', prog.pending_requests),
        statCard('意向国家', stu.country_target || '—'),
      ].join('');
    } catch (err) {
      reportError(err, '档案加载');
      root.querySelector('#my-profile').innerHTML = emptyBox('加载失败');
    }
  }

  async function reloadScores() {
    const box = root.querySelector('#my-scores');
    try {
      const { data } = await api.listScores({});
      box.innerHTML = table([
        { title: '考试', key: 'exam_name' },
        { title: '科目', key: 'subject' },
        { title: '分数', render: (r) => `<strong>${r.score}</strong> / ${r.full_score}` },
        { title: '日期', render: (r) => esc(fmtDate(r.exam_date)) },
      ], data.items, { empty: '暂无成绩记录' });
    } catch (err) {
      box.innerHTML = emptyBox('加载失败');
      reportError(err, '成绩加载');
    }
  }

  async function reloadRequests() {
    const box = root.querySelector('#my-requests');
    try {
      const { data } = await api.listRequests({});
      box.innerHTML = table([
        { title: 'ID', key: 'id' },
        { title: '类型', render: (r) => badge(r.type) },
        { title: '事由', render: (r) => esc(pick(r.content, 'reason')) },
        { title: '天数', render: (r) => esc(pick(r.content, 'days')) },
        { title: '状态', render: (r) => badge(r.status) },
        { title: '提交时间', render: (r) => esc(fmtDateTime(r.created_at)) },
      ], data.items, { empty: '暂无申请记录' });
    } catch (err) {
      box.innerHTML = emptyBox('加载失败');
      reportError(err, '申请加载');
    }
  }

  // 请假提交
  const idemKey = uid('leave');
  root.querySelector('#lv-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const raw = formValues(form);
    const body = compact({
      student_id: sid,
      request_type: raw.request_type,
      start_date: raw.start_date,
      days: toInt(raw.days, 1),
      reason: raw.reason,
      idempotency_key: root.querySelector('#lv-idem').checked ? idemKey : null,
    });
    try {
      const { data } = await api.applyLeave(body);
      if (data.duplicated) toastOk(`命中幂等键，返回原申请单 #${data.id}`);
      else toastOk(`申请已提交 #${data.id}（${statusText(data.status)}）`);
      form.reset();
      root.querySelector('#lv-form [name="start_date"]').value = today();
      await Promise.all([reloadRequests(), reloadProfile()]);
    } catch (err) {
      reportError(err, '提交申请失败');
    }
  });

  // 工单提交
  root.querySelector('#tk-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const raw = formValues(form);
    try {
      const { data } = await api.createTicket(compact({
        content: raw.content, category: raw.category, student_id: sid,
      }));
      toastOk(`工单已提交 #${data.id}（${statusText(data.status)}）`);
      form.reset();
    } catch (err) {
      reportError(err, '提交工单失败');
    }
  });

  await Promise.all([reloadProfile(), reloadScores(), reloadRequests()]);
}

/* ======================================================================== */
/* 员工 / 管理层视角                                                          */
/* ======================================================================== */
function staffShell(isManager) {
  return `
    <div class="grid grid-4" id="st-stats"></div>

    <div class="card" style="margin-top:16px">
      <div class="card-head">
        <div><h2>学员花名册</h2><div class="card-sub" id="st-total"></div></div>
      </div>
      <div class="card-body">
        <div class="toolbar">
          <input class="grow" id="st-kw" placeholder="按姓名搜索…">
          <select id="st-stage">
            <option value="">全部阶段</option>
            ${Object.entries(STAGE_LABELS).map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}
          </select>
          <button class="btn" id="st-refresh">刷新</button>
        </div>
        <div id="st-list">${loading()}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <div><h2>行政申请审批</h2><div class="card-sub">PENDING → APPROVED / REJECTED，重复审批返回 409</div></div>
        <div>
          <select id="rq-status" style="width:auto">
            <option value="">全部</option>
            <option value="PENDING" selected>待审批</option>
            <option value="APPROVED">已通过</option>
            <option value="REJECTED">已驳回</option>
          </select>
        </div>
      </div>
      <div class="card-body" id="rq-list">${loading()}</div>
    </div>

    <div class="card">
      <div class="card-head">
        <div><h2>售后工单</h2><div class="card-sub">OPEN → PROCESSING → RESOLVED → CLOSED</div></div>
      </div>
      <div class="card-body" id="tk-list">${loading()}</div>
    </div>

    ${isManager ? `
    <div class="card">
      <div class="card-head">
        <div><h2>心理预警</h2>
          <div class="card-sub">敏感数据，仅管理层可见；员工调用接口会得到 403</div></div>
      </div>
      <div class="card-body" id="al-list">${loading()}</div>
    </div>` : ''}`;
}

async function mountStaff(root, ctx, isManager) {
  /* ---------- 学员列表 ---------- */
  async function reloadStudents() {
    const box = root.querySelector('#st-list');
    box.innerHTML = loading();
    try {
      const kw = root.querySelector('#st-kw').value.trim();
      const stage = root.querySelector('#st-stage').value;
      const { data } = await api.listStudents(compact({
        keyword: kw || undefined, stage: stage || undefined, page_size: 100,
      }));
      root.querySelector('#st-total').textContent =
        `共 ${data.total} 名学员（当前展示 ${data.items.length} 名）`;
      const dist = data.items.reduce((a, s) => { a[s.stage] = (a[s.stage] || 0) + 1; return a; }, {});
      root.querySelector('#st-stats').innerHTML = [
        statCard('学员总数', data.total),
        statCard('申请中', dist.APPLYING || 0),
        statCard('已获录取', dist.OFFERED || 0),
        statCard('已入读', dist.ENROLLED || 0),
      ].join('');

      box.innerHTML = table([
        { title: 'ID', key: 'id' },
        { title: '学号', render: (r) => `<span class="mono">${esc(r.student_no)}</span>` },
        { title: '姓名', render: (r) => `<strong>${esc(r.name)}</strong>` },
        { title: '阶段', render: (r) => `<span class="badge badge-info">${esc(STAGE_LABELS[r.stage] || r.stage)}</span>` },
        { title: '意向国家', key: 'country_target' },
        { title: '顾问', key: 'advisor_id' },
        { title: '操作', render: (r) => `<button class="btn btn-sm" data-stu="${r.id}">查看</button>
                                        <button class="btn btn-sm" data-score="${r.id}">录成绩</button>` },
      ], data.items, { empty: '没有匹配的学员' });

      box.querySelectorAll('[data-stu]').forEach((b) =>
        b.addEventListener('click', () => openStudent(Number(b.dataset.stu))));
      box.querySelectorAll('[data-score]').forEach((b) =>
        b.addEventListener('click', () => openScore(Number(b.dataset.score))));
    } catch (err) {
      box.innerHTML = emptyBox('加载失败');
      reportError(err, '学员列表');
    }
  }

  /* ---------- 学员详情 ---------- */
  async function openStudent(id) {
    const h = openModal({ title: `学员详情 #${id}`, width: '680px', body: loading() });
    try {
      const [{ data: stu }, { data: prog }, { data: scores }] = await Promise.all([
        api.getStudent(id), api.studentProgress(id), api.listScores({ student_id: id }),
      ]);
      h.body.innerHTML = `
        ${kvList([
          ['学号', stu.student_no], ['姓名', stu.name], ['意向国家', stu.country_target],
          ['申请层次', stu.program_level], ['当前阶段', STAGE_LABELS[stu.stage] || stu.stage],
          ['顾问 ID', stu.advisor_id], ['入学日期', fmtDate(stu.enroll_date)],
          ['证件号（脱敏）', stu.id_card_masked],
        ])}
        <div class="card-sub" style="margin:16px 0 8px">申请进度</div>
        ${timelineHTML(prog.timeline)}
        <div class="card-sub" style="margin:16px 0 8px">成绩（${scores.items.length}）</div>
        ${table([
          { title: '考试', key: 'exam_name' }, { title: '科目', key: 'subject' },
          { title: '分数', render: (r) => `${r.score} / ${r.full_score}` },
          { title: '日期', render: (r) => esc(fmtDate(r.exam_date)) },
        ], scores.items, { empty: '暂无成绩' })}`;
    } catch (err) {
      h.body.innerHTML = emptyBox('加载失败');
      reportError(err, '学员详情');
    }
  }

  /* ---------- 录入成绩 ---------- */
  function openScore(studentId) {
    const h = openModal({
      title: `为学员 #${studentId} 录入成绩`,
      width: '460px',
      body: `
        <form id="sc-form">
          <label class="field"><span>考试名称 *</span>
            <input name="exam_name" required placeholder="例：IELTS 雅思首考"></label>
          <label class="field"><span>科目 *</span><input name="subject" required placeholder="例：听力"></label>
          <label class="field"><span>得分 *</span><input name="score" type="number" step="0.5" min="0" required></label>
          <label class="field"><span>满分</span><input name="full_score" type="number" step="0.5" value="100"></label>
          <label class="field"><span>考试日期</span><input name="exam_date" type="date" value="${today()}"></label>
        </form>`,
      footer: `<button class="btn" data-modal-close>取消</button>
               <button class="btn btn-primary" id="sc-save">保存</button>`,
    });
    h.root.querySelector('#sc-save').addEventListener('click', async () => {
      const form = h.root.querySelector('#sc-form');
      if (!form.reportValidity()) return;
      const raw = formValues(form);
      try {
        const { data } = await api.createScore(compact({
          student_id: studentId, exam_name: raw.exam_name, subject: raw.subject,
          score: toFloat(raw.score), full_score: toFloat(raw.full_score, 100),
          exam_date: raw.exam_date,
        }));
        toastOk(`成绩已录入 #${data.id}`);
        closeModal();
        reloadStudents();
      } catch (err) {
        reportError(err, '录入失败');
      }
    });
  }

  /* ---------- 申请审批 ---------- */
  async function reloadRequests() {
    const box = root.querySelector('#rq-list');
    box.innerHTML = loading();
    try {
      const status = root.querySelector('#rq-status').value;
      const { data } = await api.listRequests(compact({ status: status || undefined }));
      box.innerHTML = table([
        { title: 'ID', key: 'id' },
        { title: '学员', key: 'student_id' },
        { title: '类型', render: (r) => badge(r.type) },
        { title: '事由', cls: 'wrap', render: (r) => esc(pick(r.content, 'reason')) },
        { title: '天数', render: (r) => esc(pick(r.content, 'days')) },
        { title: '状态', render: (r) => badge(r.status) },
        { title: '提交', render: (r) => esc(fmtDateTime(r.created_at)) },
        {
          title: '操作',
          render: (r) => (r.status === 'PENDING'
            ? `<button class="btn btn-sm" data-ok="${r.id}">通过</button>
               <button class="btn btn-sm btn-danger" data-no="${r.id}">驳回</button>`
            : '<span class="field-hint">已处理</span>'),
        },
      ], data.items, { empty: '暂无申请单' });

      box.querySelectorAll('[data-ok]').forEach((b) =>
        b.addEventListener('click', () => approve(Number(b.dataset.ok), true)));
      box.querySelectorAll('[data-no]').forEach((b) =>
        b.addEventListener('click', () => approve(Number(b.dataset.no), false)));
    } catch (err) {
      box.innerHTML = emptyBox('加载失败');
      reportError(err, '申请列表');
    }
  }

  async function approve(id, ok) {
    const reason = ok ? '' : (prompt('驳回理由（可空）：') || '');
    try {
      const { data } = await api.approveLeave(id, compact({ approve: ok, remark: reason }));
      toastOk(`申请单 #${data.id} 已${ok ? '通过' : '驳回'}（${statusText(data.status)}）`);
      reloadRequests();
    } catch (err) {
      if (err.isConflict) reportError(err, '重复审批被拒绝');
      else reportError(err, '审批失败');
    }
  }

  /* ---------- 工单 ---------- */
  async function reloadTickets() {
    const box = root.querySelector('#tk-list');
    try {
      const { data } = await api.listTickets({});
      box.innerHTML = table([
        { title: 'ID', key: 'id' },
        { title: '学员', key: 'student_id' },
        { title: '摘要', cls: 'wrap', render: (r) => esc(r.summary) },
        { title: '分类', key: 'category' },
        { title: '状态', render: (r) => badge(r.status) },
        { title: '处理人', key: 'handler_id' },
        { title: '提交', render: (r) => esc(fmtDateTime(r.created_at)) },
        {
          title: '操作',
          render: (r) => `<button class="btn btn-sm" data-tk="${r.id}">流转</button>`,
        },
      ], data.items, { empty: '暂无工单' });
      box.querySelectorAll('[data-tk]').forEach((b) =>
        b.addEventListener('click', () => updateTicket(Number(b.dataset.tk))));
    } catch (err) {
      box.innerHTML = emptyBox('加载失败');
      reportError(err, '工单列表');
    }
  }

  function updateTicket(id) {
    const h = openModal({
      title: `工单流转 #${id}`,
      width: '440px',
      body: `
        <label class="field"><span>状态</span>
          <select id="tk-status">
            <option value="OPEN">待处理</option><option value="PROCESSING">处理中</option>
            <option value="RESOLVED">已解决</option><option value="CLOSED">已关闭</option>
          </select></label>
        <label class="field"><span>满意度（0-5，可空）</span>
          <input id="tk-satis" type="number" min="0" max="5"></label>`,
      footer: `<button class="btn" data-modal-close>取消</button>
               <button class="btn btn-primary" id="tk-save">提交</button>`,
    });
    h.root.querySelector('#tk-save').addEventListener('click', async () => {
      try {
        const { data } = await api.updateTicket(id, compact({
          status: h.root.querySelector('#tk-status').value,
          satisfaction: toInt(h.root.querySelector('#tk-satis').value),
        }));
        toastOk(`工单 #${data.id}：${statusText(data.before)} → ${statusText(data.status)}`);
        closeModal();
        reloadTickets();
      } catch (err) {
        reportError(err, '工单更新失败');
      }
    });
  }

  /* ---------- 心理预警（管理层） ---------- */
  async function reloadAlerts() {
    const box = root.querySelector('#al-list');
    if (!box) return;
    try {
      const { data } = await api.listAlerts({});
      box.innerHTML = table([
        { title: 'ID', key: 'id' },
        { title: '学员', key: 'student_id' },
        { title: '风险等级', render: (r) => badge(r.risk_level) },
        { title: '事由', cls: 'wrap', render: (r) => esc(r.reason) },
        { title: '状态', render: (r) => badge(r.status) },
        { title: '时间', render: (r) => esc(fmtDateTime(r.created_at)) },
        {
          title: '操作',
          render: (r) => `<button class="btn btn-sm" data-al="${r.id}">跟进</button>`,
        },
      ], data.items, { empty: '暂无预警' });
      box.querySelectorAll('[data-al]').forEach((b) =>
        b.addEventListener('click', () => updateAlert(Number(b.dataset.al))));
    } catch (err) {
      box.innerHTML = emptyBox('加载失败（员工角色访问会返回 403）');
      reportError(err, '预警列表');
    }
  }

  function updateAlert(id) {
    const h = openModal({
      title: `预警跟进 #${id}`,
      width: '420px',
      body: `<label class="field"><span>状态</span>
        <select id="al-status">
          <option value="FOLLOWING">跟进中</option>
          <option value="CLOSED">已关闭</option>
          <option value="OPEN">待处理</option>
        </select></label>`,
      footer: `<button class="btn" data-modal-close>取消</button>
               <button class="btn btn-primary" id="al-save">提交</button>`,
    });
    h.root.querySelector('#al-save').addEventListener('click', async () => {
      try {
        const { data } = await api.updateAlert(id, { status: h.root.querySelector('#al-status').value });
        toastOk(`预警 #${data.id} 状态 → ${statusText(data.status)}`);
        closeModal();
        reloadAlerts();
      } catch (err) {
        reportError(err, '预警更新失败');
      }
    });
  }

  /* ---------- 绑定 ---------- */
  root.querySelector('#st-refresh').addEventListener('click', reloadStudents);
  root.querySelector('#st-stage').addEventListener('change', reloadStudents);
  let timer;
  root.querySelector('#st-kw').addEventListener('input', () => {
    clearTimeout(timer); timer = setTimeout(reloadStudents, 320);
  });
  root.querySelector('#rq-status').addEventListener('change', reloadRequests);

  await Promise.all([reloadStudents(), reloadRequests(), reloadTickets(), reloadAlerts()]);
}
