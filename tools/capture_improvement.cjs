// Local visual QA only. No browser profile, network research or mail actions.
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const crypto = require('node:crypto');
const {chromium} = require(process.argv[2]);
(async()=>{
 const root = path.resolve('output/brief-improvement');
 const out = path.join(root,process.argv[3]||'loop-1','qa'); fs.mkdirSync(out,{recursive:true});
 const browser = await chromium.launch({channel:'msedge',headless:true});
 const results=[];
 try {
  for(const width of [320,390,1280]) {
   const page=await browser.newPage({viewport:{width,height:900}});
   await page.goto(pathToFileURL(path.join(root,'combined-email.html')).href);
   results.push(await page.locator('img[data-brand-ticker]').evaluateAll(imgs=>({surface:'company-artwork',logos:imgs.map(i=>({ticker:i.dataset.brandTicker,loaded:i.complete&&i.naturalWidth>0,alt:i.alt,width:i.width,height:i.height}))})));
   await page.screenshot({path:path.join(out,`combined-${width}.png`),fullPage:true});
   for(const ticker of ['TD','RY','CM','BNS','BMO']) await page.locator(`#company-${ticker}`).screenshot({path:path.join(out,`${ticker}-${width}.png`)});
   for(const ticker of ['TD','RY','CM','BNS','BMO']) {
    const individual=await browser.newPage({viewport:{width,height:900}});
    await individual.goto(pathToFileURL(path.join(root,`${ticker}-review.html`)).href);
    await individual.screenshot({path:path.join(out,`${ticker}-full-${width}.png`),fullPage:true});
    results.push(await individual.evaluate(ticker=>({surface:ticker+'-standalone',width:innerWidth,scrollWidth:document.documentElement.scrollWidth}),ticker));
    await individual.close();
   }
   results.push(await page.evaluate(()=>({surface:'html',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.textContent),emDash:document.body.innerText.includes('\u2014')})));
   if(width<=390) {
    const inline=fs.readFileSync(path.join(root,'gmail-body.html'),'utf8').replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi,'');
    await page.setContent(`<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0">${inline}</body></html>`);
    await page.screenshot({path:path.join(out,`inline-${width}.png`),fullPage:true});
    results.push(await page.evaluate(()=>({surface:'inline-email',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,emDash:document.body.innerText.includes('\u2014')})));
    await page.locator('img').evaluateAll(imgs=>imgs.forEach(i=>i.src='data:image/png;base64,broken'));
    await page.screenshot({path:path.join(out,`images-blocked-${width}.png`),fullPage:true});
    results.push(await page.evaluate(()=>({surface:'images-blocked',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,companyNames:[...document.querySelectorAll('.company-eyebrow')].map(e=>e.innerText),publisher:document.querySelector('.masthead-title').innerText})));
   }
   if(fs.existsSync(path.join(root,'PFSI-review.html'))) {
    await page.goto(pathToFileURL(path.join(root,'PFSI-review.html')).href);
    await page.screenshot({path:path.join(out,`PFSI-${width}.png`),fullPage:true});
    results.push(await page.evaluate(()=>({surface:'normal-PFSI',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,emDash:document.body.innerText.includes('\u2014')})));
    if(width<=390) {
     const inline=fs.readFileSync(path.join(root,'PFSI-review.html'),'utf8').replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi,'');
     await page.setContent(inline);
     await page.screenshot({path:path.join(out,`PFSI-inline-${width}.png`),fullPage:true});
     results.push(await page.evaluate(()=>({surface:'PFSI-inline',width:innerWidth,scrollWidth:document.documentElement.scrollWidth})));
     await page.locator('img').evaluateAll(imgs=>imgs.forEach(i=>i.src='data:image/png;base64,broken'));
     await page.screenshot({path:path.join(out,`PFSI-inline-images-blocked-${width}.png`),fullPage:true});
     results.push(await page.evaluate(()=>({surface:'PFSI-inline-images-blocked',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,name:document.querySelector('.company-title').innerText})));
    }
   }
   for(const label of ['combined','PFSI']) {
    const decoded=path.join(root,process.argv[3]||'loop-1',`${label}-preview-decoded.html`);
    if(fs.existsSync(decoded)) {
     await page.goto(pathToFileURL(decoded).href);
     await page.screenshot({path:path.join(out,`${label}-MIME-${width}.png`),fullPage:true});
     results.push(await page.evaluate(label=>({surface:label+'-decoded-MIME',width:innerWidth,scrollWidth:document.documentElement.scrollWidth,loadedImages:[...document.querySelectorAll('img')].every(i=>i.complete&&i.naturalWidth>0)}),label));
    }
   }
   await page.close();
  }
 } finally {await browser.close();}
 const hashes=Object.fromEntries(fs.readdirSync(root).filter(f=>/\.(html|txt|json)$/.test(f)).map(f=>[f,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,f))).digest('hex')]));
 fs.writeFileSync(path.join(out,'audit.json'),JSON.stringify({results,hashes},null,2));
 const failures=results.filter(r=>(r.width&&r.scrollWidth>r.width+1)||r.emDash||r.loadedImages===false||(r.logos&&r.logos.some(i=>!i.loaded)));
 if(failures.length) throw new Error('Visual regression: '+JSON.stringify(failures));
 console.log(JSON.stringify(results));
})().catch(e=>{console.error(e);process.exitCode=1;});
