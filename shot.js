const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 });
  await p.goto('http://127.0.0.1:8097/', { waitUntil: 'networkidle' });
  await p.waitForTimeout(600);
  await p.screenshot({ path: 'login.png' });
  await p.fill('#pw', 'a-very-long-test-password');
  await p.click('#pwToggle');          // exercise the reveal toggle
  await p.waitForTimeout(200);
  await p.screenshot({ path: 'login-revealed.png' });
  await p.click('#loginBtn');
  await p.waitForTimeout(2500);
  await p.screenshot({ path: 'dashboard.png', fullPage: true });
  console.log('password field type after toggle:', await p.getAttribute('#pw', 'type'));
  await b.close();
})();
