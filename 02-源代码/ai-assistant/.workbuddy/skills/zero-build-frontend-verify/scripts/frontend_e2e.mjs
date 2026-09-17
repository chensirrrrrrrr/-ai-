/**
 * 前端端到端验证（真实浏览器）。
 *
 * 用 puppeteer-core 驱动本机 Edge：
 *   登录 → 逐个业务视图打开 → 跑一轮对话 → 校验意图路由 → 双主题 →
 *   越权/容错 → 全程收集 console 错误 / 页面异常 / 失败请求，并逐页截图。
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

  /* ---------- 1. 登录页 ---------- */
  await page.goto(FRONT, { waitUntil: 'networkidle2', timeout: 45000 });
  await page.waitForSelector('#login-screen:not([hidden])', { timeout: 15000 });
  const title = await page.title();
  step('打开前端登录页', title.includes('留学机构'), `title="${title}"`);
  await shot(page, '01-login');

  const demoCount = await page.$$eval('#demo-accounts .demo-item', (n) => n.length);
  step('演示账号渲染', demoCount === 5, `${demoCount} 个`);

  /* ---------- 2. 登录 ---------- */
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
  await shot(page, '02-console-chat');

  /* ---------- 3. 逐个业务视图 ---------- */
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
    const crashed = text.includes('页面加载失败');
    const newErr = (pageErrors.length + consoleErrors.length) - before;
    step(`视图「${v.label}」`, active && !crashed && text.length > 10 && newErr === 0,
      crashed ? '出现「页面加载失败」' : (newErr ? `${newErr} 个 JS 错误` : `${text.length} 字`));
    await shot(page, `03-view-${v.key}`);
  }

  /* ---------- 4. 对话链路 ---------- */
  await page.click('.nav-item[data-view="chat"]');
  await sleep(500);

  const waitAnswer = () => page.waitForFunction(() => {
    const els = document.querySelectorAll('.msg.assistant .msg-bubble');
    if (!els.length) return false;
    const t = els[els.length - 1].textContent.trim();
    return t.length > 8 && !t.startsWith('\u3010');
  }, { timeout: 40000 });

  const ask = async (text) => {
    await page.click('#chat-text');
    await page.type('#chat-text', text);
    await page.click('#chat-send');
    try { await waitAnswer(); } catch { /* 由断言判定 */ }
    await sleep(500);
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
  await shot(page, '04-chat-reply');

  await ask('我要请假');
  const route2 = await page.$eval('#route-panel',
    (el) => el.textContent.replace(/\s+/g, ' ').trim());
  step('请假意图路由到学员助手', /学员助手/.test(route2), route2.slice(0, 130));
  await shot(page, '05-chat-leave');

  /* ---------- 5. 双主题 ---------- */
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
  await shot(page, `06-theme-${nowTheme}`);
  await page.click('#btn-theme');
  await sleep(300);

  /* ---------- 6. 访客越权收敛 ---------- */
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

  /* ---------- 7. 坏接口地址容错 ---------- */
  await page.evaluate(() => localStorage.setItem('uas.api_base', 'http://127.0.0.1:9/'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await sleep(2500);
  step('坏接口地址时回到登录屏', !!(await page.$('#login-screen:not([hidden])')));
  await shot(page, '07-bad-endpoint');
  await page.evaluate(() => localStorage.removeItem('uas.api_base'));

  /* ---------- 8. 汇总 ---------- */
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
