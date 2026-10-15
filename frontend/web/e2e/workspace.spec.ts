import { expect, test, type Page } from '@playwright/test';

async function observeSSE(page: Page, observe: (event: any) => void) {
  await page.exposeFunction('__captureSSE', observe);
  await page.addInitScript(() => {
    const NativeSource = window.EventSource;
    window.EventSource = class extends NativeSource {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        this.addEventListener('message', event => {
          void (window as any).__captureSSE(JSON.parse(event.data));
        });
      }
    };
  });
}

test('ambiguous research requires requester choice and persists a blocked draft without human approval', async ({ page }) => {
  const profile = await (await page.request.post('/api/models', { data: {
    label: '交付版本测试', api_format: 'openai', model: 'delivery-test', api_key: 'delivery-secret', context_window_tokens: 200000,
  } })).json();
  const session = await (await page.request.post('/api/sessions', { data: { profile_id: profile.id } })).json();
  await page.addInitScript(sid => sessionStorage.setItem('researchx.web.session', sid), session.session_id);
  await page.goto('/');
  await page.getByLabel('对话输入').fill('请点评新闻和财报');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.global-error')).toContainText('发起人选择研究类型');
  await expect(page.locator('.message.user')).toHaveCount(0);
  await page.getByLabel('研究类型').selectOption('financial_commentary');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByLabel('研究交付版本')).toContainText('点评草稿 · 资料受阻 · 暂不可批准');
  await expect(page.locator('.message.assistant').last()).toContainText('未经人工复核');
  const items = (await (await page.request.get(`/api/sessions/${session.session_id}/deliveries`)).json()).items;
  expect(items).toHaveLength(1);
  expect((await page.request.post(`/api/sessions/${session.session_id}/deliveries/${items[0].delivery_id}/review`, {
    data: { actor_id: 'self-claimed-reviewer', decision: 'approve' },
  })).status()).toBe(422);
  await page.reload();
  await expect(page.getByLabel('研究交付版本')).toContainText('暂不可批准');
  await expect(page.getByRole('button', { name: '批准', exact: true })).toHaveCount(0);
});

test('report project status and extended task states survive history refresh', async ({ page }) => {
  const created = await page.request.post('/api/models', { data: {
    label: '研报状态模型', api_format: 'openai', model: 'report-state-test', api_key: 'report-state-secret', context_window_tokens: 200000,
  } });
  const { id } = await created.json();
  const session = await (await page.request.post('/api/sessions', { data: { profile_id: id } })).json();
  const progressFixture = {
      revision: 10, plan_id: 'report-plan', title: '离线财报点评', current_task_id: null,
      tasks: ['ready', 'validating', 'failed'].map((status, index) => ({
        id: `task-${index}`, title: `任务${index + 1}`, status, blocker: '', completion_note: '',
      })), completed: 0, total: 3, replan_required: false, project_status: 'running',
  };
  expect((await page.request.post(`/__test/project-progress/${session.session_id}`, { data: progressFixture })).ok()).toBe(true);
  await page.addInitScript(sid => sessionStorage.setItem('researchx.web.session', sid), session.session_id);
  await page.goto('/');
  const progress = page.getByLabel('研究任务进度');
  await expect(progress).toContainText('研究中 · 0/3 项任务完成');
  await expect(progress).toContainText('可执行');
  await expect(progress).toContainText('验证中');
  await expect(progress).toContainText('失败');
  expect((await page.request.post(`/__test/project-progress/${session.session_id}`, { data: { ...progressFixture, project_status: 'suspended' } })).ok()).toBe(true);
  await page.reload();
  await expect(progress).toContainText('已暂停 · 0/3 项任务完成');
  await expect(progress).not.toContainText('研报已完成');
});

for (const outcome of ['unresolved', 'compatible']) {
  test(`conflict outcomes remain visible after tasks complete and history restores: ${outcome}`, async ({ page }) => {
    const created = await page.request.post('/api/models', { data: {
      label: '冲突进度模型', api_format: 'openai', model: 'conflict-test', api_key: 'conflict-test-secret', context_window_tokens: 200000,
    } });
    const { id } = await created.json();
    const response = await page.request.post(`/__test/conflict-progress?profile_id=${id}&outcome=${outcome}`);
    expect(response.ok()).toBe(true);
    const { session_id } = await response.json();
    await page.addInitScript(sid => sessionStorage.setItem('researchx.web.session', sid), session_id);
    await page.goto('/');
    const progress = page.getByLabel('研究任务进度');
    const toggle = progress.getByRole('button');
    await expect(progress).toContainText('1/1 项任务完成');
    await expect(toggle).toHaveAttribute('aria-expanded', outcome === 'unresolved' ? 'true' : 'false');
    if (outcome === 'compatible') await toggle.click();
    const conflicts = page.getByLabel('争议处理进度');
    await expect(conflicts).toContainText('两份公告的营收数字为何不同？');
    await expect(conflicts).toContainText(outcome === 'unresolved' ? '仍未决' : '裁决完成');
    await expect(conflicts).not.toContainText('assessments');
    await page.reload();
    await expect(conflicts).toContainText(outcome === 'unresolved' ? '仍未决' : '裁决完成');
  });
}

test('long collapsed progress and website-only source rows survive refresh and copy', async ({ page }) => {
  const created = await page.request.post('/api/models', { data: {
    label: '来源布局模型', api_format: 'openai', model: 'layout-test', api_key: 'layout-test-secret', context_window_tokens: 200000,
  } });
  const { id } = await created.json();
  const response = await page.request.post(`/__test/citation-layout?profile_id=${id}`);
  expect(response.ok()).toBe(true);
  const { session_id } = await response.json();
  await page.addInitScript(sid => {
    sessionStorage.setItem('researchx.web.session', sid);
    Object.defineProperty(navigator, 'clipboard', { value: {
      writeText: async (text: string) => { (window as any).copiedAnswer = text; },
    }, configurable: true });
  }, session_id);
  await page.goto('/');
  const toggle = page.getByLabel('研究任务进度').getByRole('button');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await toggle.click();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).not.toContainText('收集国内新增装机');
    const bounds = await toggle.evaluate(element => {
      const title = element.querySelector('strong')!;
      const count = element.querySelector('.research-progress-count')!;
      return { titleRight: title.getBoundingClientRect().right, countLeft: count.getBoundingClientRect().left,
        clipped: title.scrollWidth > title.clientWidth, tooltip: title.getAttribute('title'),
        overflow: document.documentElement.scrollWidth > window.innerWidth };
    });
    expect(bounds.titleRight).toBeLessThanOrEqual(bounds.countLeft);
    expect(bounds.clipped).toBe(true);
    expect(bounds.tooltip).toContain('光伏行业景气度');
    expect(bounds.overflow).toBe(false);
    const answer = page.locator('.answer-content');
    await expect(answer).toContainText('结论0；结论1[1]；结论2；结论3[2]；重复[1]');
    await expect(answer.locator('ol > li')).toHaveCount(2);
    await expect(answer).not.toContainText('内部数据');
    const items = await answer.locator('ol > li').evaluateAll(elements => elements.map(e => ({ top: e.getBoundingClientRect().top, bottom: e.getBoundingClientRect().bottom })));
    expect(items[1].top).toBeGreaterThanOrEqual(items[0].bottom);
  }
  const text = await page.locator('.answer-content').innerText();
  await page.getByRole('button', { name: '复制回复', exact: true }).click();
  expect(await page.evaluate(() => (window as any).copiedAnswer)).toContain('\n\n来源：\n\n1. ');
  expect(await page.evaluate(() => (window as any).copiedAnswer)).not.toContain('内部数据');
  await page.reload();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(page.locator('.answer-content')).toHaveText(text);
  await toggle.click();
  await expect(page.getByLabel('研究任务进度')).toContainText('收集国内新增装机');
});

test('session approvals skip repeated confirmations and remain isolated after refresh', async ({ page }) => {
  const prompts: { kind: string; tool_name?: string }[] = [];
  await observeSSE(page, data => {
    if (data.type === 'prompt') prompts.push(data);
  });
  const created = await page.request.post('/api/models', { data: {
    label: '会话授权测试模型', api_format: 'openai', model: 'session-approval-test', api_key: 'session-approval-secret', context_window_tokens: 200000,
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  const approval = page.getByRole('dialog', { name: '操作确认' });
  const sendWrite = async () => {
    await page.getByLabel('对话输入').fill('请写入测试文件');
    const needsSession = await page.evaluate(() => !sessionStorage.getItem('researchx.web.session'));
    const sessionCreated = needsSession ? page.waitForResponse(response =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/sessions' && response.status() === 201,
    ) : null;
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    if (sessionCreated) await sessionCreated;
  };
  await sendWrite();
  await expect(approval).toContainText('同一工具');
  await expect(approval.getByRole('button', { name: '允许此次操作', exact: true })).toBeVisible();
  await approval.getByRole('button', { name: '本会话始终允许', exact: true }).click();
  await expect(approval.locator('.diff')).toBeVisible();
  await expect(approval).toContainText('此文件');
  await approval.getByRole('button', { name: '本会话始终允许', exact: true }).click();
  await expect(approval).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  expect(prompts.map(prompt => prompt.kind)).toEqual(['permission', 'edit']);
  for (const refresh of [false, true]) {
    if (refresh) await page.reload();
    await sendWrite();
    await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
    await expect(approval).toHaveCount(0);
    expect(prompts).toHaveLength(2);
  }
  await page.getByLabel('对话输入').fill('请提问');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  const question = page.getByRole('dialog', { name: '补充研究信息' });
  await expect(question).toBeVisible();
  await expect(question.getByRole('button', { name: '本会话始终允许', exact: true })).toHaveCount(0);
  await question.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByRole('button', { name: '新建对话', exact: true }).click();
  await page.getByLabel('当前对话模型').selectOption(id);
  await sendWrite();
  await expect(approval.locator('.tag')).toHaveText('写入文件');
  expect(prompts.at(-1)?.kind).toBe('permission');
  await approval.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
});

test('model configuration, skills, streaming, approvals, stop and recovery', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await page.getByRole('button', { name: '模型配置', exact: true }).click();
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  await page.getByLabel('配置名称').fill('浏览器测试模型');
  await page.getByLabel('模型名称').fill('browser-test-model');
  await page.getByLabel('上下文窗口（Token）').fill('200000');
  await page.getByLabel(/^API Key/).fill('browser-test-secret');
  await page.getByRole('button', { name: '保存配置' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const card = page.locator('.model-card').filter({ has: page.getByRole('heading', { name: '浏览器测试模型', exact: true }) });
  await card.getByRole('button', { name: '测试连接' }).click();
  await expect(card.getByText('连接成功，模型可正常响应')).toBeVisible();
  await card.getByRole('button', { name: '设为默认' }).click();
  await expect(card.getByText('默认', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 financial-statement-analysis', exact: true })).toBeChecked();
  await page.getByRole('switch', { name: '启用 financial-statement-analysis', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 financial-statement-analysis', exact: true })).not.toBeChecked();
  await page.getByRole('switch', { name: '启用 financial-statement-analysis', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 financial-statement-analysis', exact: true })).toBeChecked();
  await page.getByLabel('搜索技能').fill('financial-statement-analysis');
  await expect(page.locator('.skill-card')).toHaveCount(1);
  await page.locator('.skill-card').getByRole('button', { name: '查看详情' }).click();
  await page.getByRole('button', { name: '在对话中试用' }).click();
  await expect(page.getByLabel('对话输入')).toHaveValue('/financial-statement-analysis');
  await page.getByLabel('当前对话模型').selectOption({ label: '浏览器测试模型 · browser-test-model' });
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant')).toContainText('这是浏览器测试回复');
  await expect(page.locator('.message.assistant table')).toBeVisible();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.getByLabel('会话用量')).toContainText('输入 10');
  await expect(page.getByLabel('会话用量')).toContainText('命中率 0.0%');
  await expect(page.getByLabel('会话用量')).toContainText('缓存写入 未提供');
  await expect(page.locator('.message-list')).not.toContainText('runtime_context');
  await page.getByLabel('对话输入').fill('请写入测试文件');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  const approval = page.getByRole('dialog', { name: '操作确认' });
  await expect(approval.locator('.tag')).toHaveText('写入文件');
  await expect(approval).toContainText('当前权限模式要求你确认可能修改文件或数据的操作');
  await expect(approval).not.toContainText('write_file');
  await expect(approval).not.toContainText('/permissions');
  await expect(approval.getByRole('button', { name: '允许此次操作' })).toBeVisible();
  await approval.getByRole('button', { name: '取消', exact: true }).click();
  await expect(approval).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByLabel('对话输入').fill('并行确认测试');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  for (let index = 0; index < 4; index++) {
    await expect(approval).toBeVisible();
    await expect(page.getByText('等待操作确认', { exact: true })).toBeVisible();
    await approval.getByRole('button', { name: '取消', exact: true }).click();
  }
  await expect(approval).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByLabel('对话输入').fill('请提问');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('你希望研究哪个时间范围');
  await page.getByLabel('补充信息').fill('最近一年');
  await page.getByRole('button', { name: '回复并继续' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByLabel('对话输入').fill('请慢慢回复');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await page.getByRole('button', { name: '停止生成' }).click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  const usage = await page.getByLabel('会话用量').textContent();
  const count = await page.locator('.message').count();
  await page.reload();
  await expect(page.locator('.message')).toHaveCount(count);
  await expect(page.getByLabel('会话用量')).toHaveText(usage || '');
  await page.locator('.message.assistant').last().getByRole('button', { name: '执行过程 · 已停止' }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await expect(page.getByRole('button', { name: '复制回复' }).first()).toBeVisible();
  await expect(page.locator('body')).not.toContainText('browser-test-secret');
  await page.screenshot({ path: 'test-results/research-chat.png', fullPage: true });
  await page.getByLabel('对话输入').fill('请显示测试标记');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('安全内容');
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.locator('.message.assistant script, .message.assistant img')).toHaveCount(0);
  expect(await page.evaluate(() => '__unsafe' in window)).toBe(false);
  await expect(page.getByRole('link', { name: '危险链接' })).not.toHaveAttribute('href', /^javascript:/);
  expect(browserErrors).toEqual([]);
});

test('responsive navigation and empty skill search', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await page.getByLabel('搜索技能').fill('不存在的技能');
  await expect(page.getByText('没有找到相关技能')).toBeVisible();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await page.screenshot({ path: 'test-results/research-mobile.png', fullPage: true });
});

test('research progress, source footnotes and interrupt to replan', async ({ page }) => {
  const progressFrames: any[] = [];
  let researchStarts = 0;
  await observeSSE(page, data => {
    if (data.type === 'started') researchStarts += 1;
    if (data.type === 'research_progress') progressFrames.push(data.progress);
  });
  const created = await page.request.post('/api/models', { data: {
    label: '研究记忆测试模型', api_format: 'openai', model: 'memory-test', api_key: 'memory-test-secret', context_window_tokens: 200000,
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('开展测试研究');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  // Wait for cold Runtime/database initialization before asserting progress transitions.
  await expect.poll(() => researchStarts, { timeout: 15_000 }).toBe(1);
  const progress = page.getByLabel('研究任务进度');
  const progressToggle = progress.getByRole('button');
  await expect(progressToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(progress).toContainText('0/2 项任务完成');
  await expect(progress.locator('li').filter({ hasText: '收集年报' })).toContainText('进行中');
  await expect(progress).toContainText('1/2 项任务完成');
  await expect(progress.locator('li').filter({ hasText: '核验与分析' })).toContainText('进行中');
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成');
  await expect(progressToggle).toHaveAttribute('aria-expanded', 'false');
  expect(progressFrames.some(item => item.tasks?.[0]?.status === 'completed'
    && ['pending', 'in_progress'].includes(item.tasks?.[1]?.status))).toBe(true);
  expect(progressFrames.some(item => item.current_task_id === item.tasks?.[1]?.id
    && item.tasks?.[1]?.status === 'in_progress')).toBe(true);
  await progressToggle.click();
  await expect(progressToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(progress).toContainText('收集年报');
  await expect(page.locator('.message.assistant').last().locator('.answer-content')).not.toContainText('来源：');
  await expect(page.locator('.message.assistant').last().locator('.answer-content')).not.toContainText('report.txt');
  await expect(page.locator('.message.assistant').last()).toContainText('已核对原文');
  await expect(page.locator('.tool-row')).toHaveCount(0);
  await expect(page.locator('.message-list')).not.toContainText('research_memory');
  await page.reload();
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成');
  await expect(page.getByLabel('研究任务进度').getByRole('button')).toHaveAttribute('aria-expanded', 'true');
  await page.getByLabel('研究任务进度').getByRole('button').click();
  await expect(page.getByLabel('研究任务进度').getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  await page.reload();
  await expect(page.getByLabel('研究任务进度').getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  await page.getByLabel('对话输入').fill('慢速研究公司 A');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(() => researchStarts, { timeout: 15_000 }).toBe(2);
  await expect(page.locator('.message.assistant').last()).toContainText('旧研究执行中');
  await expect(page.getByLabel('研究任务进度').getByRole('button')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByLabel('对话输入')).toBeEnabled();
  await page.getByLabel('对话输入').fill('改为研究公司 B');
  await page.getByRole('button', { name: '打断并修改', exact: true }).click();
  await expect(page.getByLabel('研究任务进度')).toContainText('公司 B 简要研究');
  // This waits for the complete replanned research (12 transactional tool calls),
  // not just rendering the accepted steer. Keep the overall 60-second test bound.
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成', { timeout: 15_000 });
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.locator('.message.user').filter({ hasText: '改为研究公司 B' })).toHaveCount(1);
  await page.reload();
  await expect(page.getByLabel('研究任务进度')).toContainText('公司 B 简要研究');
  await expect(page.locator('.message.user').filter({ hasText: '改为研究公司 B' })).toHaveCount(1);
  await expect(page.locator('.message-list')).not.toContainText('method');
  await page.screenshot({ path: 'test-results/research-memory.png', fullPage: true });
});

test('blocked research task stays expanded with its blocker', async ({ page }) => {
  let researchStarts = 0;
  await observeSSE(page, data => { if (data.type === 'started') researchStarts += 1; });
  const created = await page.request.post('/api/models', { data: {
    label: '受阻任务测试模型', api_format: 'openai', model: 'memory-test', api_key: 'memory-test-secret', context_window_tokens: 200000,
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('慢速研究公司 A');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(() => researchStarts, { timeout: 15_000 }).toBe(1);
  await expect(page.locator('.message.assistant').last()).toContainText('旧研究执行中');
  await page.getByRole('button', { name: '停止生成' }).click();
  const progress = page.getByLabel('研究任务进度');
  await expect(progress.getByRole('button')).toHaveAttribute('aria-expanded', 'true');
  await expect(progress.getByRole('button')).toHaveAttribute('aria-disabled', 'true');
  await expect(progress).toContainText('受阻 · 执行已停止');
});

async function prepareProcessChat(page: import('@playwright/test').Page) {
  const created = await page.request.post('/api/models', { data: {
    label: '执行过程测试模型', api_format: 'openai', model: 'process-test', api_key: 'process-test-secret', context_window_tokens: 200000,
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('查看执行过程');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  // Session creation loads the real Runtime; observe its first streamed event
  // before testing disclosure or completion, even during concurrent regression.
  await expect(page.locator('.process-heading').last()).toBeVisible({ timeout: 15_000 });
}

test('two levels of process disclosure, final answer and refresh recovery', async ({ page }) => {
  await prepareProcessChat(page);
  const turn = page.locator('.message.assistant').last();
  const heading = turn.locator('.process-heading');
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await expect(turn.locator('.process-content')).toContainText('我先读取两份资料，再整理结论');
  await expect(turn.locator('.activity-summary')).toContainText('读取 2 个文件');
  await expect(turn.locator('.activity-details')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(heading).toHaveText('执行过程 · 已完成');
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await expect(turn.locator('.answer-content')).toHaveCount(1);
  await expect(turn.locator('.answer-content')).toContainText('已整理资料；第二份文件不可用');
  await heading.focus();
  await page.keyboard.press('Enter');
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await expect(turn.locator('.activity-summary')).toContainText('1 项失败');
  await turn.locator('.activity-summary').click();
  await expect(turn.locator('.activity-details')).toContainText('report.txt');
  await expect(turn.locator('.activity-details')).toContainText('missing.txt');
  await expect(turn.locator('.activity-status.completed')).toHaveCount(1);
  await expect(turn.locator('.activity-status.failed')).toHaveCount(1);
  await expect(turn.locator('.process-content .message-avatar, .process-content .copy-button')).toHaveCount(0);
  await expect(turn.locator('.process-content')).not.toContainText('测试资料：营业收入同比增长');
  await page.screenshot({ path: 'test-results/conversation-process.png', fullPage: true });
  await page.reload();
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await heading.click();
  await turn.locator('.activity-summary').click();
  await expect(turn.locator('.activity-details')).toContainText('missing.txt');
  await expect(turn.locator('.answer-content')).toHaveCount(1);
});

test('manual process disclosure survives completion and stopping preserves partial progress', async ({ page }) => {
  await prepareProcessChat(page);
  const heading = page.locator('.process-heading').last();
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await heading.click();
  await heading.click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await page.getByLabel('对话输入').fill('请慢慢回复');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await page.getByRole('button', { name: '停止生成' }).click();
  const lastTurn = page.locator('.message.assistant').last();
  await expect(lastTurn.locator('.process-heading')).toHaveText('执行过程 · 已停止');
  await expect(lastTurn.locator('.process-heading')).toHaveAttribute('aria-expanded', 'false');
  await lastTurn.locator('.process-heading').click();
  await expect(lastTurn.locator('.process-content')).toContainText('这是部分回复');
  await expect(lastTurn.locator('.answer-content')).toHaveCount(0);
});

test('delete confirmation, failure retry, noncurrent and current session cleanup', async ({ page }) => {
  await prepareProcessChat(page);
  await expect(page.locator('.answer-content').last()).toContainText('已整理资料；第二份文件不可用', { timeout: 15_000 });
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  const currentId = await page.evaluate(() => sessionStorage.getItem('researchx.web.session'));
  const session = await (await page.request.get(`/api/sessions/${currentId}`)).json();
  const other = await (await page.request.post('/api/sessions', { data: { profile_id: session.profile_id } })).json();
  await page.reload();
  // History is newest first; earlier test sessions may have the same default title.
  const otherRow = page.locator('.session-row').filter({ has: page.getByRole('button', { name: '新对话', exact: true }) }).first();
  await otherRow.hover();
  await otherRow.getByRole('button', { name: '删除对话：新对话', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除对话', exact: true });
  await expect(dialog).toContainText('无法恢复');
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeFocused();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect((await page.request.get(`/api/sessions/${other.session_id}`)).ok()).toBe(true);
  await otherRow.getByRole('button', { name: '删除对话：新对话', exact: true }).click();
  await page.route(`**/api/sessions/${other.session_id}`, async route => {
    if (route.request().method() === 'DELETE') await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '删除失败，请重试' }) });
    else await route.continue();
  });
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('删除失败');
  await expect(otherRow).toHaveCount(1);
  await page.unroute(`**/api/sessions/${other.session_id}`);
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await page.request.get(`/api/sessions/${other.session_id}`)).status()).toBe(404);
  expect(await page.evaluate(() => sessionStorage.getItem('researchx.web.session'))).toBe(currentId);
  await page.getByLabel('对话输入').fill('未发送草稿');
  await page.locator('.session-row.selected').hover();
  await page.locator('.session-row.selected .session-delete').click();
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await expect(page.getByLabel('对话输入')).toHaveValue('');
  expect(await page.evaluate(() => sessionStorage.getItem('researchx.web.session'))).toBe(null);
  expect((await page.request.get(`/api/sessions/${currentId}`)).status()).toBe(404);
  await expect(page.getByText('连接已断开', { exact: false })).toHaveCount(0);
});

test('mobile deletion remains visible and returns to welcome', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await prepareProcessChat(page);
  await expect(page.locator('.answer-content').last()).toContainText('已整理资料；第二份文件不可用', { timeout: 15_000 });
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByRole('button', { name: '打开导航' }).click();
  const remove = page.locator('.session-row.selected .session-delete');
  await expect(remove).toBeVisible();
  await expect(remove).toHaveCSS('opacity', '1');
  await remove.click();
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});

test('disconnect keeps the run and reports only the event connection', async ({ page }) => {
  await page.addInitScript(() => {
    const NativeSource = window.EventSource;
    window.EventSource = class extends NativeSource {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        (window as Window & { testStream?: EventSource }).testStream = this;
      }
    };
  });
  const created = await page.request.post('/api/models', { data: {
    label: '断线测试模型', api_format: 'openai', model: 'disconnect-test', api_key: 'disconnect-test-secret', context_window_tokens: 200000,
  } });
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('请提问');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.evaluate(() => {
    const stream = (window as Window & { testStream?: EventSource }).testStream!;
    stream.close();
    stream.dispatchEvent(new Event('error'));
  });
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByText('事件连接断开')).toBeVisible();
  const heading = page.locator('.process-heading');
  await expect(heading).toHaveText('执行过程 · 进行中');
  const sid = await page.evaluate(() => sessionStorage.getItem('researchx.web.session'));
  await expect.poll(async () => {
    const session = await (await page.request.get(`/api/sessions/${sid}`)).json();
    return (session.messages ?? []).some((row: { status?: string; turn_status?: string }) =>
      row.status === 'interrupted' || row.turn_status === 'stopped');
  }).toBe(false);
  await page.reload();
  await expect(page.getByText('执行过程 · 已停止')).toHaveCount(0);
  await expect(page.getByText('已中断')).toHaveCount(0);
});

test('new stream content follows the bottom without interrupting reading above', async ({ page }) => {
  let startedRequestId = '';
  await observeSSE(page, event => {
    if (event.type === 'started') startedRequestId = event.request_id;
  });
  const created = await page.request.post('/api/models', { data: {
    label: '滚动测试模型', api_format: 'openai', model: 'scroll-test', api_key: 'scroll-test-secret', context_window_tokens: 200000,
  } });
  const { id } = await created.json();
  const session = await (await page.request.post('/api/sessions', { data: { profile_id: id } })).json();
  let requestId = '';
  page.on('request', request => {
    if (request.url().endsWith(`/api/sessions/${session.session_id}/commands`) && request.method() === 'POST') {
      const command = request.postDataJSON();
      if (command.type === 'submit') requestId = command.request_id;
    }
  });
  await page.addInitScript(sid => sessionStorage.setItem('researchx.web.session', sid), session.session_id);
  await page.goto('/');
  await expect(page.getByLabel('当前对话模型')).toHaveValue(id);
  await page.getByLabel('对话输入').fill('长篇研究');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(() => requestId).not.toBe('');
  // HTTP acceptance precedes Runtime startup and its persisted initial state.
  await expect.poll(() => startedRequestId, { timeout: 15_000 }).toBe(requestId);
  const send = async (text: string) => {
    const response = await page.request.post(`/__test/stream-delta/${session.session_id}`, { data: {
      request_id: requestId, turn_id: requestId, id: 'long-reply', text,
    } });
    expect(response.ok()).toBe(true);
  };
  await send('研究资料段落。\n\n'.repeat(100));
  const scroll = page.locator('.conversation-scroll');
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
  await scroll.evaluate(element => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event('scroll'));
  });
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(0);
  await send('新增资料。\n\n'.repeat(10));
  await expect(page.locator('.answer-content')).toContainText('新增资料');
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(0);
  await scroll.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
  await scroll.evaluate(element => {
    // Deliver a scroll notification after content grows but before passive
    // effects, as browsers can do when anchoring streaming Markdown.
    const observer = new MutationObserver(() => {
      observer.disconnect();
      element.dispatchEvent(new Event('scroll'));
    });
    observer.observe(element.querySelector('.answer-content')!, { childList: true, characterData: true, subtree: true });
  });
  await send('继续补充。\n\n'.repeat(10));
  await expect(page.locator('.answer-content')).toContainText('继续补充');
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
});


test('attachments, real script artifacts, download, reload and isolation', async ({page}) => {
  const created = await page.request.post('/api/models', { data: {
    label: '附件验收模型', api_format: 'openai', model: 'file-test-model', api_key: 'file-test-secret', context_window_tokens: 200000,
  }});
  const {id} = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('上传研究资料').setInputFiles({name:'sample.txt', mimeType:'text/plain', buffer:Buffer.from('Synthetic reference input')});
  await expect(page.getByLabel('会话附件')).toContainText('sample.txt');
  await expect(page.getByLabel('会话附件').getByRole('checkbox')).toBeChecked();
  await page.getByLabel('对话输入').fill('导出固定验收产物');
  await page.getByRole('button', {name:'发送消息', exact:true}).click();
  await page.getByRole('dialog', {name:'操作确认'}).getByRole('button', {name:'允许此次操作'}).click();
  await expect(page.getByRole('button', {name:'停止生成'})).toHaveCount(0, { timeout: 20_000 });
  await expect(page.getByLabel('研究产物').getByRole('link')).toHaveCount(4);
  const response = await page.request.get(await page.getByLabel('研究产物').getByRole('link').filter({hasText:'digest.json'}).getAttribute('href') || '');
  expect(response.ok()).toBe(true);
  expect((await response.json()).reports[0].predictions[0].year).toBe(2026);
  await page.reload();
  await expect(page.getByLabel('研究产物').getByRole('link')).toHaveCount(4);
  await expect(page.getByLabel('会话附件')).toContainText('sample.txt');
  await page.getByRole('button', {name:'删除附件 sample.txt'}).click();
  await expect(page.getByLabel('会话附件')).toHaveCount(0);
  await page.getByRole('button', {name:'新建对话', exact:true}).click();
  await expect(page.getByLabel('研究产物')).toHaveCount(0);
  await expect(page.getByLabel('会话附件')).toHaveCount(0);
});

test('first message is delivered when SSE becomes ready before session refresh finishes', async ({ page }) => {
  const created = await page.request.post('/api/models', { data: {
    label: '首次连接竞态模型', api_format: 'openai', model: 'browser-test-model', api_key: 'first-connection-secret', context_window_tokens: 200000,
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.addInitScript(() => {
    const NativeSource = window.EventSource;
    window.EventSource = class extends NativeSource {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options);
        this.addEventListener('message', event => {
          if (JSON.parse(String(event.data)).type === 'ready') {
            queueMicrotask(() => { (window as any).__sessionReadyDelivered = true; });
          }
        });
      }
    };
  });
  let holdRefresh = false;
  await page.route('**/api/sessions', async route => {
    if (holdRefresh && route.request().method() === 'GET') {
      holdRefresh = false;
      await page.waitForFunction(() => (window as any).__sessionReadyDelivered === true);
    }
    await route.continue();
  });
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('首次连接时发送消息');
  holdRefresh = true;
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant')).toContainText('这是浏览器测试回复', { timeout: 15_000 });
  await expect(page.getByLabel('对话输入')).toHaveValue('');
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.locator('.message.user')).toHaveCount(1);
});

test('parallel collection branches update without a parent revision and reject stale branch versions', async ({ page }) => {
  const profile = await (await page.request.post('/api/models', { data: {
    label: '并行采集测试', api_format: 'openai', model: 'parallel-test', api_key: 'parallel-secret', context_window_tokens: 200000,
  } })).json();
  const session = await (await page.request.post('/api/sessions', { data: { profile_id: profile.id } })).json();
  const base = { revision: 10, plan_id: 'parallel-plan', title: '宁德时代近期新闻', current_task_id: 'collect',
    tasks: [{ id: 'collect', title: '采集与核验', status: 'in_progress', blocker: '', completion_note: '' }],
    completed: 0, total: 1, replan_required: false };
  const batch = { dispatch_id: 'batch', version: 1, parent_task_id: 'collect', status: 'running',
    branches: ['新闻事件', '公司公告', '券商观点'].map((title, index) => ({ task_id: `${index}`, title, status: 'running', reviewed: false })) };
  const update = async (value: unknown) => expect((await page.request.post(`/__test/project-progress/${session.session_id}`, { data: value })).ok()).toBe(true);
  await update({ ...base, dispatches: [batch] });
  await page.addInitScript(sid => sessionStorage.setItem('researchx.web.session', sid), session.session_id);
  await page.goto('/');
  const branches = page.getByLabel('并行采集进度');
  await expect(branches.getByText('采集中', { exact: true })).toHaveCount(3);
  const complete = { ...batch, version: 2, branches: batch.branches.map(b => b.task_id === '0' ? { ...b, status: 'awaiting_review' } : b) };
  await update({ ...base, dispatches: [complete] });
  await expect(branches).toContainText('采集结束 · 待主代理核对');
  await expect(branches.getByText('采集中', { exact: true })).toHaveCount(2);
  await update({ ...base, dispatches: [batch] });
  await expect(branches).toContainText('采集结束 · 待主代理核对');
  await update({ ...base, revision: 9, dispatches: [{ ...batch, version: 99 }] });
  await expect(branches).toContainText('采集结束 · 待主代理核对');
  await update({ ...base, revision: 11, dispatches: [{ ...complete, branches: complete.branches.map(b => b.task_id === '0' ? { ...b, reviewed: true } : b) }] });
  await expect(branches).toContainText('主 Agent 已核对');
  await page.reload();
  await expect(page.getByLabel('并行采集进度')).toContainText('主 Agent 已核对');
});
