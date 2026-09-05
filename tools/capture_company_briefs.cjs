// Local rendered proof of individual briefs and actual MIME alternatives.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {pathToFileURL}=require('node:url');
const {chromium}=require(process.argv[2]);
(async()=>{
 const root=path.resolve('output/company-briefs'),out=path.join(root,'review','qa');
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const results=[], status=JSON.parse(fs.readFileSync(path.join(root,'build-status.json'),'utf8'));
 const rgb=hex=>'rgb('+[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)).join(', ')+')';
 try {
  for(const ticker of ['TD','RY','CM','BNS','BMO','PFSI']) {
   for(const width of [320,390,1280]) {
    const page=await browser.newPage({viewport:{width,height:900}});
    for(const variant of ['brief','mime','inline','blocked']) {
     if(width===1280&&['inline','blocked'].includes(variant))continue;
     let html=fs.readFileSync(path.join(root,variant==='brief'?`${ticker}-review.html`:`${ticker}/email-1-preview.html`),'utf8');
     if(['inline','blocked'].includes(variant))html=html.replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi,'');
     await page.setContent(html,{waitUntil:'load'});
     if(variant==='blocked')await page.locator('img').evaluateAll(images=>images.forEach(i=>i.src='data:image/png;base64,broken'));
     await page.screenshot({path:path.join(out,`${ticker}-${width}-${variant}-full.png`),fullPage:true});
     if(variant==='brief')await page.screenshot({path:path.join(out,`${ticker}-${width}-first.png`)});
     results.push(await page.evaluate(({ticker,variant})=>{
      const elements=[...document.querySelectorAll('*')];
      const decorations=elements.flatMap(e=>['::before','::after','::marker'].map(p=>getComputedStyle(e,p).content));
      const readerAttributes=elements.flatMap(e=>['aria-label','aria-description','aria-valuetext','title','alt','placeholder'].map(a=>e.getAttribute(a)||''));
      const negative=/((?<![\w./-])(?:[\u2212-]\s*(?:[A-Z]{0,3}\s*[$€£]\s*)?|[A-Z]{0,3}\s*[$€£]\s*[\u2212-]\s*)(?:\d|\.\d))/;
      return {ticker,variant,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
      markerCount:document.querySelectorAll('[data-brief-company]').length,
      markers:[...document.querySelectorAll('[data-brief-company]')].map(x=>x.dataset.briefCompany),
      textHasEmDash:(document.title+' '+document.body.innerText).includes('\u2014'),
      accessibleHasEmDash:readerAttributes.some(v=>v.includes('\u2014')),
      decorationHasEmDash:decorations.some(v=>v.includes('\u2014')),
      negativeHasSign:[document.title,document.body.innerText,...decorations,...readerAttributes].some(v=>negative.test(v.replace(/https?:\/\/\S+/g,''))),
      logos:[...document.querySelectorAll('img[data-brand-ticker]')].map(i=>({ticker:i.dataset.brandTicker,loaded:i.complete&&i.naturalWidth>0,width:i.width,height:i.height})),
      brandSurfaces:[...document.querySelectorAll('[data-brand-surface]')].map(e=>({role:e.dataset.brandSurface,bg:getComputedStyle(e).backgroundColor,color:getComputedStyle(e).color})),
      heroText:[...document.querySelectorAll('.finding-band h1,.finding-band h2,.finding-band p,.finding-band a,.finding-band sup')].map(e=>({text:e.textContent,color:getComputedStyle(e).color})),
      headings:[...document.querySelectorAll('h1,h2,h3')].map(h=>({text:h.textContent,y:Math.round(h.getBoundingClientRect().top)})),
      bodyHeight:document.body.scrollHeight
     };},{ticker,variant}));
    }
    await page.close();
   }
  }
 } finally {await browser.close();}
 const hashes=Object.fromEntries(fs.readdirSync(root).filter(f=>f.endsWith('-review.html')||f.endsWith('-review.txt')).map(f=>[f,crypto.createHash('sha256').update(fs.readFileSync(path.join(root,f))).digest('hex')]));
 fs.writeFileSync(path.join(out,'audit.json'),JSON.stringify({results,hashes},null,2));
 const paletteFailures=results.filter(r=>r.brandSurfaces.some(s=>s.bg!==rgb(status[r.ticker].theme[{hero:'hero_bg',page:'page_bg',paper:'paper_bg',section:'section_bg'}[s.role]]))||r.heroText.some(t=>t.color!==rgb(status[r.ticker].theme.hero_text)));
 const failures=results.filter(r=>r.scrollWidth>r.width+1||r.markerCount!==1||r.markers[0]!==r.ticker||r.textHasEmDash||r.accessibleHasEmDash||r.decorationHasEmDash||r.negativeHasSign||(r.variant!=='blocked'&&r.logos.some(i=>!i.loaded))||paletteFailures.includes(r));
 console.log(JSON.stringify({surfaces:results.length,failures},null,2));
 if(failures.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
