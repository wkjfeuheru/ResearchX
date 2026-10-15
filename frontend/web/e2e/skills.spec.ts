import { test, expect } from '@playwright/test';

test('individual skill sources, selected entry loading and switches', async ({ page }) => {
  const entries: string[] = [];
  page.on('request', request => {
    if (request.method() === 'GET' && /\/api\/skills\/[^/]+$/.test(request.url())) entries.push(request.url());
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await expect(page.locator('.skill-card')).toHaveCount(8);
  await expect(page.locator('.skill-card').first().getByText('内置', { exact: true })).toBeVisible();
  await page.getByLabel('搜索技能').fill('industry-commentary');
  await expect(page.locator('.skill-card')).toHaveCount(1);
  await page.locator('.skill-card').getByRole('button', { name: '查看详情' }).click();
  expect(entries).toHaveLength(0);
  const section = page.locator('.skill-instructions');
  await section.getByRole('button', { name: '查看技能流程' }).click();
  await expect(section.getByRole('heading', { name: '适用与禁用场景', exact: true })).toBeVisible();
  expect(entries).toHaveLength(1);
  expect(entries[0]).toMatch(/\/skills\/builtin_[a-f0-9]+$/);
  const toggle = page.getByRole('dialog').getByRole('switch', { name: '启用 industry-commentary', exact: true });
  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByRole('button', { name: '在对话中试用' })).toBeDisabled();
  await expect(section.getByRole('heading', { name: '适用与禁用场景', exact: true })).toBeVisible();
  await toggle.click();
  await expect(toggle).toBeChecked();
});
