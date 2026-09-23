import { chromium } from 'playwright';
const browser = await chromium.launch({
  executablePath: 'C:/Users/Priyanshu Madhup/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe',
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.route('**/api/chat', async (route) => {
  await new Promise((r) => setTimeout(r, 6000));
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ reply: 'slow reply', pending_action: null }) });
});
await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.fill('#query', 'hi');
await page.click('.submit-btn');
await page.waitForTimeout(4800);
await page.screenshot({ path: 'longwait_visible.png' });
await browser.close();
