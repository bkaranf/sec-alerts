// Optional local visual QA. Uses an installed Playwright package; no product dependency.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.argv[2] || 'playwright');
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const output = path.resolve('output/qa');
  fs.mkdirSync(output, {recursive:true});
  const checks = [];
  try {
  for (const [name, width, height] of [['desktop',1280,1000],['mobile',481,1000],['phone',390,844]]) {
    const page = await browser.newPage({viewport:{width,height}, deviceScaleFactor:1});
    await page.goto('http://127.0.0.1:8879/qa/draft/briefing.html', {waitUntil:'load'});
    await page.screenshot({path:path.join(output,`${name}-brief.png`), fullPage:true});
    await page.screenshot({path:path.join(output,`${name}-brief-top.png`)});
    await page.locator('table.figures').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(output,`${name}-brief-table.png`)});
    checks.push(await page.evaluate(() => ({
      viewport:innerWidth, scrollWidth:document.documentElement.scrollWidth,
      tableHeaders:[...document.querySelectorAll('table.figures th')].map(n=>n.textContent.trim()),
      title:document.querySelector('h1')?.textContent.trim(),
      headings:[...document.querySelectorAll('h1,h2,h3')].map(n=>n.textContent.trim()),
      highlightRows:[...document.querySelectorAll('table.figures tbody > tr')].filter(n=>n.style.backgroundColor).map(n=>({text:n.textContent.trim(),color:n.style.backgroundColor})),
      minimumBodySize:Math.min(...[...document.querySelectorAll('.body-copy')].map(n=>parseFloat(getComputedStyle(n).fontSize)))
    })));
    await page.close();
  }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(output,'layout-audit.json'), JSON.stringify(checks,null,2));
  console.log(JSON.stringify(checks));
})().catch(error=>{console.error(error);process.exitCode=1});
