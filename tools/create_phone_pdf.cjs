// Render only local approved artifacts in an isolated headless browser.
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.argv[2]);
const {execFileSync} = require('node:child_process');
(async()=>{
  const root=path.resolve('output/five-company-review');
  execFileSync(path.resolve('.venv/Scripts/python.exe'), ['-m','servicing_brief.reader_value_release',root], {stdio:'inherit'});
  const out=path.resolve('tmp/pdfs'); fs.mkdirSync(out,{recursive:true});
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const audit=[];
  try {
    for(const ticker of ['TD','RY','CM','BNS','BMO']) {
      const page=await browser.newPage({viewport:{width:480,height:1000}});
      await page.goto(pathToFileURL(path.join(root,`${ticker}-review.html`)).href);
      await page.emulateMedia({media:'screen'});
      // This PDF contains the briefs themselves, not the email attachment note.
      await page.locator('.document-footer').evaluateAll(nodes=>nodes.forEach(n=>n.remove()));
      const height=await page.evaluate(()=>Math.ceil(document.documentElement.scrollHeight)+4);
      await page.pdf({path:path.join(out,`${ticker}.pdf`),width:'480px',height:`${height}px`,printBackground:true,margin:{top:0,right:0,bottom:0,left:0}});
      audit.push({ticker,height}); await page.close();
    }
  } finally {await browser.close();}
  fs.writeFileSync(path.join(out,'layout.json'),JSON.stringify(audit,null,2));
  console.log(JSON.stringify(audit));
})().catch(e=>{console.error(e);process.exitCode=1});
