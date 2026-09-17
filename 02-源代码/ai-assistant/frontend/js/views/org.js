/**
 * 组织与指南：组织架构查询（M3-06） + 新人入职指引（M3-07）。
 *
 * 三者共用一套数据：组织架构树 / 部门概览 / 员工花名册来自 `employee` 表，
 * 入职指引来自后端版本化的知识资产（`services/onboarding.py` → `NEO` 分类知识文档）。
 * 前端不做任何业务判断，只负责渲染与勾选进度。
 */
import { api } from '../api.js';
import {
  compact, debounce, emptyBox, esc, kvList, loading, openModal, reportError,
  statCard, table, toastOk, toastWarn,
} from '../ui.js';

const CHECK_KEY = 'uas.neo_checklist';        // 入职清单勾选进度（本机）

/** 业务角色中文名（employee.biz_role 的取值） */
const BIZ_ROLE = {
  advisor: '顾问', teacher: '带教/跟进', manager: '管理层', admin: '管理员',
};

/** 本地小徽标：ui.badge() 遇到空值会强制显示「—」，这里需要能自定义文案 */
const chip = (text, cls = 'badge-neutral') =>
  `<span class="badge ${cls}">${esc(text)}</span>`;

function loadChecks() {
  try {
    const raw = JSON.parse(localStorage.getItem(CHECK_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    return new Set();
  }
}

function saveChecks(set) {
  try { localStorage.setItem(CHECK_KEY, JSON.stringify([...set])); } catch { /* 隐私模式忽略 */ }
}

export default {
  key: 'org',
  title: '组织与指南',
  desc: '组织架构 · 部门与花名册 · 新人入职指引（阶段清单 / 常见问题 / 关键联系人）',
  roles: ['employee', 'manager', 'admin'],

  async mount(root, ctx) {
    const checks = loadChecks();

    root.innerHTML = `
      <div class="grid grid-4" id="org-stats"></div>

      <div class="card" style="margin-top:16px">
        <div class="card-head">
          <div><h2>组织架构</h2>
            <div class="card-sub">按汇报关系（manager_id）展开，点任意员工看详情</div></div>
          <div class="toolbar" style="margin:0">
            <select id="org-dept"><option value="">全部部门</option></select>
            <button class="btn btn-sm" id="org-refresh">刷新</button>
          </div>
        </div>
        <div class="card-body" id="org-tree">${loading()}</div>
      </div>

      <div class="grid grid-2">
        <div class="card">
          <div class="card-head">
            <div><h2>部门概览</h2><div class="card-sub">人数 / 负责人 / 成员</div></div>
          </div>
          <div class="card-body" id="dept-list">${loading()}</div>
        </div>

        <div class="card">
          <div class="card-head">
            <div><h2>员工花名册</h2><div class="card-sub">按姓名 / 工号 / 职位检索</div></div>
          </div>
          <div class="card-body">
            <div class="toolbar" style="margin-bottom:12px">
              <input id="emp-kw" class="grow" placeholder="搜索姓名、工号或职位…">
              <select id="emp-dept"><option value="">全部部门</option></select>
            </div>
            <div id="emp-list">${loading()}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2 id="neo-title">新人入职指引</h2><div class="card-sub" id="neo-head"></div></div>
          <div class="toolbar" style="margin:0">
            <span id="neo-progress" class="badge badge-neutral">—</span>
            <button class="btn btn-sm" id="neo-reset">清空勾选</button>
          </div>
        </div>
        <div class="card-body">
          <div id="neo-stages">${loading()}</div>
          <div class="card-sub" style="margin:18px 0 8px">关键联系人（按当前登录人解析）</div>
          <div id="neo-contacts">${loading()}</div>
          <div id="neo-limits" style="margin-top:14px"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>新人常见问题</h2><div class="card-sub">后端按「子串 + 二元组相似度」召回，整句提问也能命中</div></div>
          <div class="toolbar" style="margin:0">
            <input id="faq-kw" placeholder="例如：报到要带什么">
            <button class="btn btn-sm" id="faq-search">检索</button>
          </div>
        </div>
        <div class="card-body" id="faq-list">${loading()}</div>
      </div>`;

    /* ---------------- 组织架构树 ---------------- */
    function renderNode(node, depth) {
      const kids = node.children || [];
      const line = `
        <div class="org-line" data-emp="${node.id}" title="查看详情">
          <span class="org-name">${esc(node.name)}</span>
          <span class="org-title">${esc(node.title || '—')}</span>
          <span class="org-dept-chip">${esc(node.department || '未分组')}</span>
          <span class="org-title">${esc(node.emp_no)}</span>
          ${kids.length ? `<span class="org-title">下辖 ${kids.length} 人</span>` : ''}
        </div>`;
      const child = kids.length
        ? `<div class="org-children">${kids.map((k) => renderNode(k, depth + 1)).join('')}</div>`
        : '';
      return `<div class="org-node">${line}${child}</div>`;
    }

    async function reloadTree() {
      const box = root.querySelector('#org-tree');
      const dept = root.querySelector('#org-dept').value;
      box.innerHTML = loading();
      try {
        const { data } = await api.orgTree(compact({ department: dept }));
        box.innerHTML = data.tree.length
          ? data.tree.map((n) => renderNode(n, 0)).join('')
          : emptyBox(dept ? `「${dept}」下没有在职员工` : '暂无在职员工');
        box.querySelectorAll('[data-emp]').forEach((el) =>
          el.addEventListener('click', () => showEmployee(Number(el.dataset.emp))));
        root.querySelector('#org-stats').innerHTML = [
          statCard('部门数', data.departments.length),
          statCard('在册员工', data.total),
          statCard('汇报线根节点', data.root_count, '未被他人管理的最高层'),
          statCard('含下辖人数', data.tree.length ? '是' : '—'),
        ].join('');
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '组织架构');
      }
    }

    /* ---------------- 部门概览 ---------------- */
    async function reloadDepartments() {
      const box = root.querySelector('#dept-list');
      try {
        const { data } = await api.orgDepartments();
        box.innerHTML = table([
          { title: '部门', render: (r) => `<strong>${esc(r.department)}</strong>` },
          { title: '人数', key: 'headcount' },
          {
            title: '负责人',
            render: (r) => (r.head
              ? `<button class="btn btn-sm" data-emp="${r.head.id}">${esc(r.head.name)}</button>`
              : chip('未指定', 'badge-warn')),
          },
          { title: '成员', cls: 'wrap', render: (r) => esc(r.members.map((m) => m.name).join('、')) },
        ], data.items, { empty: '暂无部门数据' });
        box.querySelectorAll('[data-emp]').forEach((el) =>
          el.addEventListener('click', () => showEmployee(Number(el.dataset.emp))));
        return data.items;
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '部门概览');
        return [];
      }
    }

    /* ---------------- 花名册 ---------------- */
    async function reloadEmployees() {
      const box = root.querySelector('#emp-list');
      box.innerHTML = loading();
      const kw = root.querySelector('#emp-kw').value.trim();
      const dept = root.querySelector('#emp-dept').value;
      try {
        const { data } = await api.listEmployees(
          compact({ keyword: kw, department: dept, page_size: 20 }));
        box.innerHTML = table([
          { title: '工号', key: 'emp_no' },
          { title: '姓名', render: (r) => `<strong>${esc(r.name)}</strong>` },
          { title: '部门', key: 'department' },
          { title: '职位', key: 'title' },
          { title: '业务角色', render: (r) => chip(BIZ_ROLE[r.biz_role] || r.biz_role, 'badge-info') },
          {
            title: '操作',
            render: (r) => `<button class="btn btn-sm" data-emp="${r.id}">详情</button>`,
          },
        ], data.items, { empty: '没有匹配的员工' });
        box.querySelectorAll('[data-emp]').forEach((el) =>
          el.addEventListener('click', () => showEmployee(Number(el.dataset.emp))));
      } catch (err) {
        box.innerHTML = emptyBox('加载失败');
        reportError(err, '员工花名册');
      }
    }

    async function showEmployee(id) {
      const h = openModal({ title: `员工详情 #${id}`, width: '560px', body: loading() });
      try {
        const { data } = await api.getEmployee(id);
        h.body.innerHTML = `
          ${kvList([
            ['工号', data.emp_no], ['姓名', data.name], ['部门', data.department],
            ['职位', data.title], ['业务角色', data.biz_role],
            ['电话', data.phone], ['邮箱', data.email],
          ])}
          <div class="card-sub" style="margin:14px 0 8px">汇报关系</div>
          ${data.manager
            ? `<p class="field-hint">直属上级：<strong>${esc(data.manager.name)}</strong>
                 （${esc(data.manager.title || '-')}）</p>`
            : '<p class="field-hint">系统里未登记直属上级。</p>'}
          ${data.subordinates.length
            ? `<p class="field-hint">下辖 ${data.subordinates.length} 人：
                 ${esc(data.subordinates.map((s) => s.name).join('、'))}</p>`
            : '<p class="field-hint">暂无下辖成员。</p>'}
          ${data.colleagues.length
            ? `<p class="field-hint">同部门同事：
                 ${esc(data.colleagues.map((s) => s.name).join('、'))}</p>`
            : '<p class="field-hint">同部门暂无其他在职同事。</p>'}`;
      } catch (err) {
        h.body.innerHTML = emptyBox('加载失败');
        reportError(err, '员工详情');
      }
    }

    /* ---------------- 入职指引 ---------------- */
    function updateProgress(total) {
      const done = [...checks].length;
      root.querySelector('#neo-progress').textContent = `已勾选 ${done} / ${total}`;
      root.querySelector('#neo-progress').className =
        `badge ${done === 0 ? 'badge-neutral' : done >= total ? 'badge-ok' : 'badge-info'}`;
    }

    function renderStages(guide) {
      const box = root.querySelector('#neo-stages');
      const stages = guide.stages;
      const total = stages.reduce((s, x) => s + x.items.length, 0);

      box.innerHTML = stages.map((s) => `
        <details class="guide-stage">
          <summary>
            <span>${esc(s.name)}</span>
            <span class="org-dept-chip">${esc(s.window)}</span>
            <span class="org-title">${s.items.length} 项 · ${esc(s.goal)}</span>
          </summary>
          <div class="guide-stage-body">
            ${s.items.map((it, i) => `
              <div class="guide-item">
                <span class="idx">${i + 1}</span>
                <div class="body">
                  <strong>
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                      <input type="checkbox" data-check="${esc(it.key)}"
                             ${checks.has(it.key) ? 'checked' : ''}>
                      ${esc(it.title)}
                    </label>
                  </strong>
                  <p>${esc(it.detail)}</p>
                  <div class="meta">负责人：${esc(it.owner)} · 办理入口：${esc(it.channel)}</div>
                </div>
              </div>`).join('')}
          </div>
        </details>`).join('');

      box.querySelectorAll('[data-check]').forEach((el) =>
        el.addEventListener('change', () => {
          const key = el.dataset.check;
          if (el.checked) checks.add(key); else checks.delete(key);
          saveChecks(checks);
          updateProgress(total);
        }));

      updateProgress(total);
    }

    function renderContacts(data) {
      const box = root.querySelector('#neo-contacts');
      box.innerHTML = `<div class="contact-grid">${data.items.map((i) => `
        <div class="contact-card">
          <div class="label">${esc(i.label)}</div>
          ${i.contact
            ? `<div class="who">${esc(i.contact.name)}
                 <span class="org-title">${esc(i.contact.title || i.contact.department || '')}</span></div>
               <div class="desc">${esc(i.contact.phone || '')}${i.contact.email ? ' · ' + esc(i.contact.email) : ''}</div>`
            : '<div class="who">—</div>'}
          <div class="desc">${esc(i.note || i.desc)}</div>
        </div>`).join('')}</div>`;

      const notes = Object.entries(data.notes || {});
      root.querySelector('#neo-limits').innerHTML = notes.length
        ? `<div class="badge badge-warn">资料缺口</div>
           <ul class="side-list" style="margin-top:8px">
             ${notes.map(([, v]) => `<li><span>${esc(v)}</span></li>`).join('')}
           </ul>`
        : '';
    }

    function renderFaq(items, emptyText) {
      const box = root.querySelector('#faq-list');
      if (!items.length) {
        box.innerHTML = emptyBox(emptyText || '没有命中的条目');
        return;
      }
      box.innerHTML = items.map((f) => `
        <div class="faq-item">
          <div class="q">${esc(f.q)}</div>
          <div class="a">${esc(f.a)}</div>
          <div class="foot">负责人：${esc(f.owner)} ·
            标签：${esc((f.tags || []).join('、'))}${f.score ? ` · 匹配度 ${f.score}` : ''}</div>
        </div>`).join('');
    }

    async function reloadOnboarding() {
      try {
        const [{ data: guide }, { data: contacts }] = await Promise.all([
          api.onboardingGuide(), api.onboardingContacts(null),
        ]);
        root.querySelector('#neo-title').textContent = guide.title;
        root.querySelector('#neo-head').textContent =
          `${guide.version} · 负责人 ${guide.owner} · 更新 ${guide.updated_at} · `
          + `${guide.stats.stage_count} 阶段 / ${guide.stats.item_count} 项 / ${guide.stats.faq_count} 常见问题`;
        renderStages(guide);
        renderContacts(contacts);
        renderFaq(guide.faq, '指引里还没有常见问题');
        // 指引底部补上「不覆盖什么」，避免新人误以为已包含全部制度
        root.querySelector('#neo-limits').insertAdjacentHTML('afterbegin',
          `<div class="card-sub" style="margin-bottom:6px">适用范围：${esc(guide.scope)}</div>
           <ul class="side-list">${guide.limits.map((x) => `<li><span>${esc(x)}</span></li>`).join('')}</ul>`);
        return guide;
      } catch (err) {
        root.querySelector('#neo-stages').innerHTML = emptyBox('入职指引加载失败');
        reportError(err, '入职指引');
        return null;
      }
    }

    /* ---------------- 事件绑定 ---------------- */
    root.querySelector('#org-refresh').addEventListener('click', () => {
      reloadTree(); toastOk('组织架构已刷新');
    });
    root.querySelector('#org-dept').addEventListener('change', reloadTree);
    root.querySelector('#emp-dept').addEventListener('change', reloadEmployees);
    root.querySelector('#emp-kw').addEventListener('input', debounce(reloadEmployees));

    root.querySelector('#neo-reset').addEventListener('click', () => {
      checks.clear();
      saveChecks(checks);
      root.querySelectorAll('[data-check]').forEach((el) => { el.checked = false; });
      updateProgress(root.querySelectorAll('[data-check]').length);
      toastWarn('已清空本机的勾选进度');
    });

    async function searchFaq() {
      const kw = root.querySelector('#faq-kw').value.trim();
      const box = root.querySelector('#faq-list');
      if (!kw) { await reloadOnboarding(); return; }
      box.innerHTML = loading('检索中…');
      try {
        const { data } = await api.onboardingFaq(kw, 20);
        renderFaq(data.items, `「${kw}」没有命中的条目，换个说法再试`);
      } catch (err) {
        box.innerHTML = emptyBox('检索失败');
        reportError(err, '常见问题检索');
      }
    }
    root.querySelector('#faq-search').addEventListener('click', searchFaq);
    root.querySelector('#faq-kw').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); searchFaq(); }
    });

    /* ---------------- 首屏 ---------------- */
    const depts = await reloadDepartments();
    const opts = depts.map((d) => `<option value="${esc(d.department)}">${esc(d.department)}</option>`).join('');
    root.querySelector('#org-dept').insertAdjacentHTML('beforeend', opts);
    root.querySelector('#emp-dept').insertAdjacentHTML('beforeend', opts);

    await Promise.all([reloadTree(), reloadEmployees(), reloadOnboarding()]);
  },
};
