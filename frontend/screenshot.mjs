import { chromium } from 'playwright';
const browser = await chromium.launch({
  executablePath: 'C:/Users/Priyanshu Madhup/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe',
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto('http://localhost:5173/', { waitUntil: 'networkidle' });
await page.fill('#query', 'hi');
await page.click('.submit-btn');
await page.waitForTimeout(1200);
await page.screenshot({ path: 'typing_early.png' });
await page.waitForTimeout(3500);
await page.screenshot({ path: 'typing_longwait.png' });
await browser.close();
