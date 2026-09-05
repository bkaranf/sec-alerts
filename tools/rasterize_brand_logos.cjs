// Rasterize official vector artwork for email compatibility, without redrawing it.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {chromium}=require(process.argv[2]);
(async()=>{
 const root=process.cwd(),out=path.join(root,'servicing_brief/assets/brands');fs.mkdirSync(out,{recursive:true});
 const research=JSON.parse(fs.readFileSync('output/brief-improvement/branding-research.json','utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true});const records=[];
 try {
  for(const c of research.companies){
   const r=c.representative; let source=path.resolve(r.path);
   if(c.ticker==='TD') source=path.resolve(r.emailFallback.path);
   if(c.ticker==='PFSI') source=path.resolve('assets/brands/PFSI-logo-R-horz-rgb-pos-color-thumbnail.png');
   const target=path.join(out,c.ticker+'.png');
   let dimensions;
   if(source.endsWith('.svg')){
    const svg=fs.readFileSync(source,'utf8');const viewbox=svg.match(/viewBox=["']([^"']+)["']/i);
    if(!viewbox) throw new Error('Logo has no verified viewBox: '+c.ticker);
    const [x,y,w,h]=viewbox[1].trim().split(/[ ,]+/).map(Number);const scale=Math.min(400/w,96/h);
    dimensions={width:Math.ceil(w*scale),height:Math.ceil(h*scale)};
    const page=await browser.newPage({viewport:dimensions,javaScriptEnabled:false});
    await page.route('**/*',r=>r.abort());
    await page.setContent(`<html><head><style>html,body{margin:0;padding:0;background:transparent}svg{display:block;width:${dimensions.width}px;height:${dimensions.height}px}</style></head><body>${svg}</body></html>`);
    await page.screenshot({path:target,omitBackground:true});await page.close();
   }else{fs.copyFileSync(source,target);dimensions={method:'original PNG dimensions retained'};}
   records.push({ticker:c.ticker,source:path.relative(root,source),source_sha256:crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex'),path:path.relative(root,target),sha256:crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex'),dimensions,method:source.endsWith('.svg')?'Exact SVG browser rasterization; unchanged paths, fills and proportions':'Unchanged official PNG copy'});
  }
 }finally{await browser.close();}
 fs.writeFileSync('output/brief-improvement/branding-rasterization.json',JSON.stringify(records,null,2));console.log(JSON.stringify(records));
})().catch(e=>{console.error(e);process.exitCode=1});
