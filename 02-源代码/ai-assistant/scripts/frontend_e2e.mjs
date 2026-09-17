/**
 * 前端端到端验证（真实浏览器）。
 *
 * 用 puppeteer-core 驱动本机 Edge：
 *   访客落地页 → 员工登录 → 逐个业务视图打开 → 跑一轮对话 → 校验意图路由 →
 *   双主题 → 越权/容错 → 返回首页/访客入口/退出 →
 *   全程收集 console 错误 / 页面异常 / 失败请求，并逐页截图。
 *
 * 前置：
 *   - 后端已启动（默认 http://127.0.0.1:8010）
 *   - 前端已启动（默认 http://127.0.0.1:8020）
 *   - 本机装了 Edge
 *
 * 运行（puppeteer-core 通常不在项目里，脚本会自动去 node 工作区找；
 *  也可用 WB_NODE_WORKSPACE 指定含 node_modules 的目录）：
 *   set WB_NODE_WORKSPACE=<...>\binaries\node\workspace
 *   node scripts/frontend_e2e.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROJECT = path.resolve(HERE, '..');

const EDGE = process.env.EDGE_PATH
  || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const FRONT = process.env.FRONT_URL || 'http://127.0.0.1:8020/';
const BACK = process.env.BACK_URL || 'http://127.0.0.1:8010';
const OUT = process.env.SHOT_DIR || path.join(PROJECT, 'reports', 'frontend_shots');
const REPORT = path.join(PROJECT, 'reports', 'frontend_e2e_report.json');

fs.mkdirSync(OUT, { recursive: true });

/* puppeteer-core 的定位：ESM 不认 NODE_PATH，只能靠 node_modules 逐级向上找。
   项目里通常没装，所以额外探几个候选目录（可用 WB_NODE_WORKSPACE 覆盖）。 */
async function loadPuppeteer() {
  try {
    return (await import('puppeteer-core')).default;
  } catch { /* 继续找 */ }

  const { createRequire } = await import('node:module');
  const candidates = [
    process.env.WB_NODE_WORKSPACE,
    process.env.WB_NODE_WORKSPACE_HINT,
    path.join(process.env.USERPROFILE || '', '.workbuddy', 'binaries', 'node', 'workspace'),
  ].filter(Boolean);

  for (const base of candidates) {
    try {
      const req = createRequire(path.join(base, 'noop.js'));
      const resolved = req.resolve('puppeteer-core');
      return (await import(pathToFileURL(resolved).href)).default;
    } catch { /* 换下一个 */ }
  }
  throw new Error(
    '找不到 puppeteer-core。请在已安装它的目录下运行，'
    + '或设置环境变量 WB_NODE_WORKSPACE 指向含 node_modules 的目录。');
}

const puppeteer = await loadPuppeteer();

const steps = [];
const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function step(name, ok, detail = '') {
  steps.push({ name, ok, detail });
  console.log(`${ok ? '[OK]  ' : '[FAIL]'} ${name}${detail ? ' — ' + detail : ''}`);
}

const shot = (page, name) => page.screenshot({ path: path.join(OUT, `${name}.png`) });

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: true,
  // 浏览器 profile 放系统临时目录，别污染 reports/
  args: ['--no-first-run', '--no-default-browser-check', '--disable-gpu',
    `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), 'fe-e2e-'))}`],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 940, deviceScaleFactor: 1 });

  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => pageErrors.push(String(e.message || e)));
  page.on('requestfailed', (r) =>
    failedRequests.push(`${r.method()} ${r.url()} :: ${r.failure()?.errorText}`));
  page.on('response', (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.request().method()} ${r.url()}`);
  });

  /* ---------- 1. 访客落地页 ---------- */
  await page.goto(FRONT, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#landing:not([hidden])', { timeout: 15000 });
  const title = await page.title();
  step('未登录默认打开访客落地页', title.includes('留学机构'), `title="${title}"`);

  const landing = await page.evaluate(() => ({
    cards: document.querySelectorAll('#capability .landing-card').length,
    scenes: document.querySelectorAll('#scene .landing-scene').length,
    steps: document.querySelectorAll('#flow .landing-flow > li').length,
    faq: document.querySelectorAll('#faq details').length,
    loginBtns: document.querySelectorAll('[data-landing="login"]').length,
    visitorBtns: document.querySelectorAll('[data-landing="visitor"]').length,
    loginHidden: document.getElementById('login-screen').hidden,
    consoleHidden: document.getElementById('console').hidden,
  }));
  step('落地页内容渲染完整',
    landing.cards === 6 && landing.scenes === 3 && landing.steps === 4 && landing.faq === 4
    && landing.loginBtns >= 1 && landing.visitorBtns >= 1
    && landing.loginHidden && landing.consoleHidden,
    `${landing.cards} 能力卡 / ${landing.scenes} 场景 / ${landing.steps} 步骤 / ${landing.faq} FAQ`);
  await shot(page, '01-landing');

  /* ---------- 2. 落地页 → 登录屏 ---------- */
  await page.click('[data-landing="login"]');
  await page.waitForSelector('#login-screen:not([hidden])', { timeout: 10000 });
  step('落地页「员工登录」切到登录屏', !(await page.$('#landing:not([hidden])')));
  await shot(page, '02-login');

  const demoCount = await page.$$eval('#demo-accounts .demo-item', (n) => n.length);
  step('演示账号渲染', demoCount === 5, `${demoCount} 个`);

  /* ---------- 3. 登录 ---------- */
  await page.click('[data-user="admin"]');
  const filled = await page.$eval('#login-username', (el) => el.value);
  step('点击演示账号自动填充', filled === 'admin', `username=${filled}`);

  await page.click('#login-submit');
  await page.waitForSelector('#console:not([hidden])', { timeout: 20000 });
  step('登录成功并进入控制台', true);

  await page.waitForFunction(
    () => !(document.getElementById('mode-text') || {}).textContent?.includes('检测中'),
    { timeout: 15000 },
  ).catch(() => {});
  await sleep(600);
  const modeText = await page.$eval('#mode-text', (el) => el.textContent);
  step('健康探针连通（状态灯）', modeText.includes('dify='), modeText);
  await shot(page, '03-console-chat');

  /* ---------- 4. 逐个业务视图 ---------- */
  const views = await page.$$eval('#nav .nav-item', (els) =>
    els.map((e) => ({ key: e.dataset.view, label: e.textContent.trim() })));
  step('导航项渲染', views.length >= 6, views.map((v) => v.label).join(' / '));

  for (const v of views) {
    const before = pageErrors.length + consoleErrors.length;
    await page.click(`.nav-item[data-view="${v.key}"]`);
    await sleep(1500);
    const active = await page.$eval(`.nav-item[data-view="${v.key}"]`,
      (el) => el.classList.contains('active'));
    const text = await page.$eval('#view', (el) => el.textContent.trim());
    // ⚠️ 视图自己 catch 住的渲染失败**不会**抛到 pageerror / console —— 它只是一句
    // emptyBox('加载失败') 或一个 reportError 的 toast。只查「页面加载失败」（路由层）
    // 会漏掉「某个卡片加载失败」这类局部静默失败，所以两种文案都要查。
    const crashed = text.includes('页面加载失败') || text.includes('加载失败');
    const newErr = (pageErrors.length + consoleErrors.length) - before;
    step(`视图「${v.label}」`, active && !crashed && text.length > 10 && newErr === 0,
      crashed ? '出现「加载失败」/「页面加载失败」' : (newErr ? `${newErr} 个 JS 错误` : `${text.length} 字`));
    await shot(page, `04-view-${v.key}`);
  }

  /* ---------- 4b. 组织与指南：内容必须真的渲染出来 ---------- */
  await page.click('.nav-item[data-view="org"]');
  await sleep(1500);
  const org = await page.evaluate(() => ({
    nodes: document.querySelectorAll('#org-tree .org-node').length,
    deptRows: document.querySelectorAll('#dept-list table.data tbody tr').length,
    stages: document.querySelectorAll('#neo-stages details.guide-stage').length,
    items: document.querySelectorAll('#neo-stages [data-check]').length,
    contacts: document.querySelectorAll('#neo-contacts .contact-card').length,
    faqs: document.querySelectorAll('#faq-list .faq-item').length,
    failed: document.querySelectorAll('#view .empty').length,
  }));
  step('组织与指南：架构树 / 花名册 / 指引 / 联系人 / FAQ 全部渲染',
    org.nodes === 3 && org.deptRows === 3 && org.stages === 5 && org.items === 19
    && org.contacts === 5 && org.faqs === 12 && org.failed === 0,
    `${org.nodes} 节点 / ${org.deptRows} 部门 / ${org.stages} 阶段 / ${org.items} 项 / `
    + `${org.contacts} 联系人 / ${org.faqs} FAQ / ${org.failed} 个失败占位`);

  /* ---------- 4c. 客户管理：材料研判区（上传 / 队列 / 复核入口） ---------- */
  await page.click('.nav-item[data-view="crm"]');
  await sleep(1800);

  const crm = await page.evaluate(() => ({
    pickall: !!document.getElementById('crm-pickall'),
    batchBtn: !!document.getElementById('crm-batch'),
    fileInput: !!document.querySelector('#sc-file[multiple]'),
    parserBox: !!document.getElementById('sc-parse'),
    emptyQueue: !!document.querySelector('#sc-queue .empty'),
    queueCount: document.getElementById('sc-queue-count')?.textContent,
    rows: document.querySelectorAll('#sc-list table.data tbody tr').length,
    detailBtns: document.querySelectorAll('#sc-list [data-sdetail]').length,
    reviewBtns: document.querySelectorAll('#sc-list [data-review]').length,
    revisedBadges: document.querySelectorAll('#sc-list .badge-bad').length,
    corrBtn: !!document.getElementById('sc-corr'),
  }));
  step('客户管理：研判列表 / 复核入口 / 批量队列结构完整',
    crm.pickall && crm.batchBtn && crm.fileInput && crm.parserBox && crm.corrBtn
    && crm.emptyQueue && crm.queueCount === '0'
    && crm.rows >= 3 && crm.detailBtns === crm.rows && crm.reviewBtns === crm.rows,
    `${crm.rows} 条研判 / ${crm.detailBtns} 详情 / ${crm.reviewBtns} 复核 / `
    + `${crm.revisedBadges} 条已推翻`);

  // 演示数据里预置了一条「AI 判定被人工推翻」的记录，修正清单必须有内容
  await page.click('#sc-corr');
  await page.waitForSelector('#modal-root:not([hidden])', { timeout: 8000 }).catch(() => {});
  await sleep(1200);
  const corrections = await page.evaluate(() => ({
    open: !document.getElementById('modal-root').hidden,
    chips: document.querySelectorAll('#modal-root .chips .chip').length,
    rows: document.querySelectorAll('#modal-root table.data tbody tr').length,
    showsTransition: (document.getElementById('modal-root').textContent || '').includes('→'),
  }));
  step('「修正清单」弹窗：人工推翻记录 + 结论迁移分布',
    corrections.open && corrections.rows >= 1 && corrections.showsTransition,
    `${corrections.rows} 条修正 / ${corrections.chips} 个统计标签`);
  await shot(page, '04b-crm-corrections');
  await page.click('#modal-root [data-modal-close]');
  await sleep(400);

  // 打开一条研判的复核弹窗（只读检查结构，不提交）
  await page.click('#sc-list [data-review]');
  await page.waitForSelector('#modal-root #rv-action', { timeout: 8000 }).catch(() => {});
  await sleep(600);
  const review = await page.evaluate(() => ({
    hasAction: !!document.getElementById('rv-action'),
    hasConclusion: !!document.getElementById('rv-conclusion'),
    hasRemark: !!document.getElementById('rv-remark'),
    options: Array.from(document.getElementById('rv-action')?.options || [])
      .map((o) => o.value).join(','),
  }));
  step('复核弹窗提供确认 / 推翻两个动作与修正结论',
    review.hasAction && review.hasConclusion && review.hasRemark
    && review.options === 'CONFIRM,OVERRIDE',
    `动作=${review.options}`);
  await shot(page, '04c-crm-review');
  await page.click('#modal-root [data-modal-close]');
  await sleep(400);

  /* ---------- 4d. 主动待办：对话页横幅 + 运营中心卡片 ---------- */
  await page.click('.nav-item[data-view="chat"]');
  await sleep(1600);
  const banner = await page.evaluate(() => {
    const box = document.getElementById('todo-banner');
    return {
      exists: !!box,
      visible: !!box && !box.hidden,
      text: box ? box.textContent.replace(/\s+/g, ' ').trim() : '',
      chips: document.querySelectorAll('#todo-banner .chip').length,
      ask: !!document.getElementById('todo-ask'),
      goto: !!document.getElementById('todo-goto'),
    };
  });
  step('对话页「主动提醒」横幅：待办问句 + 分类计数 + 动作按钮',
    banner.exists && banner.visible && banner.ask && banner.goto
    && banner.chips >= 3 && banner.text.includes('有没有'),
    banner.chips ? `${banner.chips} 类待办 · ${banner.text.slice(0, 56)}` : '横幅未渲染');
  await shot(page, '04d-todo-banner');

  await page.click('.nav-item[data-view="ops"]');
  await sleep(1800);
  await page.click('#td-push');                       // 手动触发一轮（与定时任务同一入口）
  await sleep(1600);
  const opsTodo = await page.evaluate(() => ({
    items: document.querySelectorAll('#td-pending .todo-item').length,
    high: document.querySelectorAll('#td-pending .todo-item.is-high').length,
    chips: document.querySelectorAll('#td-pending .badge').length,
    pushes: document.querySelectorAll('#td-pushes table.data tbody tr').length,
    ackBtns: document.querySelectorAll('#td-pushes [data-ack]').length,
    pushBtn: !!document.getElementById('td-push'),
    notifyBtn: !!document.getElementById('td-notify'),
    failed: document.querySelectorAll('#td-pending .empty').length,
  }));
  step('运营中心：待办卡片 + 推送留痕表 + 触达入口',
    opsTodo.items >= 4 && opsTodo.pushes >= 1 && opsTodo.pushBtn && opsTodo.notifyBtn
    && opsTodo.ackBtns >= 1 && opsTodo.failed === 0,
    `${opsTodo.items} 类待办（${opsTodo.high} 高优先）/ ${opsTodo.pushes} 条推送记录 / `
    + `${opsTodo.ackBtns} 条待处理`);
  await shot(page, '04e-ops-todo');

  /* ---------- 4e. 五类业务报告：列表 / 生成弹窗 / 详情结构化 ---------- */
  await page.click('.nav-item[data-view="ops"]');
  await sleep(1200);
  await page.click('#rp-sched');                 // 手动跑一轮定时报告（幂等，第二次只会跳过）
  await sleep(2400);
  const reports = await page.evaluate(() => ({
    rows: document.querySelectorAll('#rp-list table.data tbody tr').length,
    scheduled: document.querySelectorAll('#rp-list .badge-info').length,
    exportBtns: document.querySelectorAll('#rp-list [data-rpex]').length,
    newBtn: !!document.getElementById('rp-new'),
    schedBtn: !!document.getElementById('rp-sched'),
    filterAll: !!document.getElementById('rp-filter-all'),
  }));
  step('运营中心：报告列表（五类口径 + 导出入口 + 来源筛选）',
    reports.rows >= 1 && reports.exportBtns === reports.rows * 2
    && reports.newBtn && reports.schedBtn && reports.filterAll,
    `${reports.rows} 份报告 / ${reports.exportBtns} 个导出按钮`);
  await shot(page, '04f-report-list');

  /* 定时报告不能靠「最新 N 条里恰好有」来验 —— 报告一多，定时报告就被人工报告
     挤出可视窗口（本机实测踩过）。走「只看定时」筛选，断言与数据量无关。 */
  await page.evaluate(() => document.querySelector('#rp-filter-scheduled').click());
  await sleep(1600);
  const schedList = await page.evaluate(() => ({
    rows: document.querySelectorAll('#rp-list table.data tbody tr').length,
    scheduled: document.querySelectorAll('#rp-list .badge-info').length,
    manual: document.querySelectorAll('#rp-list .badge-neutral').length,
  }));
  step('运营中心：报告列表「只看定时」筛选',
    schedList.rows >= 1 && schedList.scheduled === schedList.rows && schedList.manual === 0,
    `${schedList.rows} 份定时 / ${schedList.manual} 份人工（应为 0）`);

  await page.evaluate(() => document.querySelector('#rp-filter-all').click());
  await sleep(1400);

  await page.click('#rp-new');
  await page.waitForSelector('#modal-root #rp-form', { timeout: 8000 }).catch(() => {});
  await sleep(900);
  const genForm = await page.evaluate(() => ({
    options: Array.from(document.querySelectorAll('#rp-form select[name="report_type"] option'))
      .map((o) => o.value),
    hasStart: !!document.querySelector('#rp-form input[name="period_start"]'),
  }));
  step('「生成报告」弹窗提供五类口径与报告期选择',
    genForm.options.length === 5 && genForm.options.includes('customer_ops')
    && genForm.options.includes('mental_weekly') && genForm.hasStart,
    genForm.options.join(' / '));
  await shot(page, '04g-report-generate');
  await page.click('#modal-root [data-modal-close]');
  await sleep(400);

  await page.click('#rp-list [data-rp]');        // 打开第一份报告的详情
  await page.waitForSelector('#modal-root .chips', { timeout: 8000 }).catch(() => {});
  await sleep(1000);
  const reportDetail = await page.evaluate(() => {
    const modal = document.getElementById('modal-root');
    const text = modal.textContent || '';
    return {
      metrics: modal.querySelectorAll('.chips .chip').length,
      tables: modal.querySelectorAll('table.data').length,
      exportBtns: modal.querySelectorAll('[data-mo]').length,
      hasNotes: text.includes('口径说明'),
      hasSummary: text.includes('结论'),
      crashed: text.includes('加载失败'),
    };
  });
  step('报告详情：指标芯片 / 明细表 / 口径说明 / 四种导出',
    reportDetail.metrics >= 4 && reportDetail.tables >= 1 && reportDetail.exportBtns === 4
    && reportDetail.hasNotes && reportDetail.hasSummary && !reportDetail.crashed,
    `${reportDetail.metrics} 个指标 / ${reportDetail.tables} 张表 / `
    + `${reportDetail.exportBtns} 个导出按钮`);
  await shot(page, '04h-report-detail');
  await page.click('#modal-root [data-modal-close]');
  await sleep(300);

  /* ---------- 5. 对话链路 ---------- */
  await page.click('.nav-item[data-view="chat"]');
  await sleep(500);

  // ⚠️ 这里必须等「上一条真的收尾」，不能只等「最后一个气泡字数 > 8」。
  //    流式是逐块追加的，字数达标时生成还没结束 —— 紧接着的第二次 ask 会被
  //    前端的 `state.streaming` 守卫静默吞掉（只弹个 toast，不产生任何气泡），
  //    于是路由面板停在上一句，`请假意图路由到学员助手` 变成随机失败。
  //    判据用「发送按钮重新可用」+「出现了新的气泡」。
  const waitIdle = () => page.waitForFunction(() => {
    const btn = document.querySelector('#chat-send');
    return !!btn && !btn.disabled;
  }, { timeout: 60000 }).catch(() => { /* 由断言判定 */ });

  const waitAnswer = () => page.waitForFunction(() => {
    const btn = document.querySelector('#chat-send');
    if (!btn || btn.disabled) return false;                       // 还在生成
    if (document.querySelectorAll('.msg').length <= (window.__e2eMsgCount || 0)) {
      return false;                                               // 新气泡还没出现
    }
    const els = document.querySelectorAll('.msg.assistant .msg-bubble');
    if (!els.length) return false;
    const t = els[els.length - 1].textContent.trim();
    return t.length > 8 && !t.startsWith('\u3010');
  }, { timeout: 60000 }).catch(() => { /* 由断言判定 */ });

  const ask = async (text) => {
    await waitIdle();                                             // 上一条必须已收尾
    await page.evaluate(() => {
      window.__e2eMsgCount = document.querySelectorAll('.msg').length;
    });
    await page.click('#chat-text');
    await page.type('#chat-text', text);
    await page.click('#chat-send');
    await waitAnswer();
    await sleep(300);
  };

  await ask('留学需要准备哪些材料？');
  const bubbles = await page.$$eval('.msg', (els) => els.map((e) => e.textContent.trim()));
  const last = bubbles[bubbles.length - 1] || '';
  step('对话返回应答', bubbles.length >= 2 && last.length > 12 && !last.startsWith('\u3010'),
    `${bubbles.length} 个气泡；末条：${last.replace(/\s+/g, ' ').slice(0, 46)}`);

  const routeText = await page.$eval('#route-panel',
    (el) => el.textContent.replace(/\s+/g, ' ').trim());
  step('路由状态面板刷新', !routeText.includes('尚未发起对话') && /Agent/.test(routeText),
    routeText.slice(0, 130));
  await shot(page, '05-chat-reply');

  await ask('我要请假');
  const route2 = await page.$eval('#route-panel',
    (el) => el.textContent.replace(/\s+/g, ' ').trim());
  step('请假意图路由到学员助手', /学员助手/.test(route2), route2.slice(0, 130));
  await shot(page, '06-chat-leave');

  /* ---------- 6. 双主题 ---------- */
  const initialTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  await page.click('#btn-theme');
  await sleep(500);
  const nowTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  const probe = await page.evaluate(() => {
    const rgb = (v) => v.match(/\d+/g).map(Number);
    const bg = rgb(getComputedStyle(document.body).backgroundColor);
    const fg = rgb(getComputedStyle(document.body).color);
    return { bg: (bg[0] + bg[1] + bg[2]) / 3, fg: (fg[0] + fg[1] + fg[2]) / 3 };
  });
  const contrastOk = (probe.bg > 150 && probe.fg < 110) || (probe.bg < 90 && probe.fg > 150);
  step('主题切换与对比度', nowTheme !== initialTheme && contrastOk,
    `${initialTheme} → ${nowTheme}，底色亮度 ${probe.bg.toFixed(0)} / 字色亮度 ${probe.fg.toFixed(0)}`);
  await shot(page, `07-theme-${nowTheme}`);
  await page.click('#btn-theme');
  await sleep(300);

  /* ---------- 7. 访客越权收敛 ---------- */
  const visitorCheck = await page.evaluate(async (B) => {
    const r = await fetch(`${B}/api/v1/auth/visitor`, { method: 'POST' });
    const j = await r.json();
    const r2 = await fetch(`${B}/api/v1/leads`, {
      headers: { Authorization: `Bearer ${j.data.access_token}` },
    });
    const j2 = await r2.json();
    return { status: r2.status, code: j2.code };
  }, BACK);
  step('访客访问客户列表被拒（403）', visitorCheck.status === 403,
    `${visitorCheck.status} code=${visitorCheck.code}`);

  /* ---------- 8. 坏接口地址容错 ---------- */
  await page.evaluate(() => localStorage.setItem('uas.api_base', 'http://127.0.0.1:9/'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await sleep(2500);
  step('坏接口地址时回落到登录屏', !!(await page.$('#login-screen:not([hidden])')));
  await shot(page, '08-bad-endpoint');
  await page.evaluate(() => localStorage.removeItem('uas.api_base'));

  /* ---------- 9. 返回首页 → 访客入口 → 退出 ---------- */
  await page.click('#btn-back-home');
  await sleep(400);
  step('登录屏「返回首页」回到落地页', !!(await page.$('#landing:not([hidden])')));

  await page.click('[data-landing="visitor"]');
  await page.waitForSelector('#console:not([hidden])', { timeout: 20000 });
  await sleep(900);
  const visitorNav = await page.$$eval('#nav .nav-item', (els) => els.map((e) => e.dataset.view));
  step('落地页「免费咨询」以访客进入且仅见 2 个视图',
    visitorNav.length === 2 && visitorNav.includes('chat') && visitorNav.includes('system')
    && visitorNav.includes('chat') && !visitorNav.includes('crm') && !visitorNav.includes('data'),
    visitorNav.join(' / '));
  await shot(page, '09-visitor-console');

  await page.click('#btn-logout');
  await sleep(500);
  step('退出登录后回到落地页', !!(await page.$('#landing:not([hidden])')));

  /* ---------- 10. 汇总 ---------- */
  await page.goto(FRONT, { waitUntil: 'domcontentloaded' });
  await sleep(400);

  const f422 = failedRequests.filter((x) => x.startsWith('422'));
  step('无 422 请求参数校验失败', f422.length === 0, f422.slice(0, 3).join(' | '));

  const f404 = failedRequests.filter((x) => x.startsWith('404'));
  step('无 404 静态资源缺失', f404.length === 0, f404.slice(0, 3).join(' | '));

  step('无未捕获的页面异常', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));

  const summary = {
    passed: steps.filter((s) => s.ok).length,
    total: steps.length,
    steps,
    consoleErrors,
    pageErrors,
    failedRequests: failedRequests.filter((x) => !x.includes('127.0.0.1:9')),
    frontUrl: FRONT,
    backUrl: BACK,
    generatedAt: new Date().toISOString(),
  };
  fs.writeFileSync(REPORT, JSON.stringify(summary, null, 2), 'utf-8');

  console.log(`\n===== ${summary.passed}/${summary.total} 通过 =====`);
  console.log(`console 错误 ${consoleErrors.length} ｜ 页面异常 ${pageErrors.length}` +
    ` ｜ 失败请求 ${failedRequests.length}`);
  if (consoleErrors.length) console.log('console:', consoleErrors.slice(0, 8));
  if (pageErrors.length) console.log('pageerror:', pageErrors.slice(0, 8));
  console.log(`报告：${REPORT}`);
  console.log(`截图：${OUT}`);

  process.exitCode = summary.passed === summary.total ? 0 : 1;
} finally {
  await browser.close();
}
