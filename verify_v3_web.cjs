// Execute the real v3 page against an isolated HTTP backend with mocked upstream.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const {setTimeout:sleep}=require('node:timers/promises');
const root=__dirname,dataDir=fs.mkdtempSync(path.join(__dirname,'v3-qa-'));
const backend=spawn('python',[path.join(__dirname,'qa_v3_server.py'),'--data-dir',dataDir],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONIOENCODING:'utf-8'}});
let output='';backend.stdout.on('data',x=>output+=x);backend.stderr.on('data',x=>output+=x);
async function until(test){for(let i=0;i<200;i++){if(test())return;await sleep(25);}throw Error('QA timeout '+output);}
(async()=>{try{
 await until(()=>fs.existsSync(path.join(dataDir,'runtime.json')));
 const base='http://127.0.0.1:'+JSON.parse(fs.readFileSync(path.join(dataDir,'runtime.json'))).port;
 const html=fs.readFileSync(path.join(root,'web_v3/index.html'),'utf8'),elements=new Map();
 for(const id of ['clock','history','start','saveGenerated'])assert(!html.includes(`id="${id}"`));
 assert(html.includes('id="seed"'));
 class Element{
  constructor(tag,attrs={}){this.tagName=tag;this.value=attrs.value||'';this.disabled='disabled'in attrs;this.checked=false;this.children=[];this.events={};this.attributes=attrs;this._text='';const classes=new Set((attrs.class||'').split(/\s+/));this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,value)=>{value=value===undefined?!classes.has(x):value;if(value)classes.add(x);else classes.delete(x);}};}
  set textContent(x){this._text=String(x);this.children=[];}get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
  append(...x){this.children.push(...x);}replaceChildren(...x){this.children=x;this._text='';}
  addEventListener(name,fn){(this.events[name]??=[]).push(fn);}querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  getBoundingClientRect(){return {width:700,height:360};}getContext(){return new Proxy({},{get:(target,key)=>target[key]??(()=>{})});}click(){if(!this.disabled)return this.onclick?.({preventDefault(){}});}
 }
 const attrs=source=>Object.fromEntries([...source.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(x=>[x[1],x[2]??'']));
 for(const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="[^"]+"[^>]*)>/g)){const a=attrs(match[2]);assert(!elements.has(a.id));elements.set(a.id,new Element(match[1],a));}
 elements.get('addMode').value='password';
 let cookie='',failSessionOnce=false,failUseOnce=false,failGenerateOnce=false,interleaveAccount=null,dropNextResponse=false,stopAfterNext=false;const calls=[],stored=new Map();
 async function fetchPage(relative,options={}){
  calls.push([options.method||'GET',relative,options.body?JSON.parse(options.body):null]);
  if(failSessionOnce&&relative==='/api/session'){failSessionOnce=false;return {ok:false,status:500,json:async()=>({code:'TEST_READ_FAILURE',message:'read failed'})};}
  if(failUseOnce&&relative.endsWith('/use')){failUseOnce=false;return {ok:false,status:502,json:async()=>({code:'TEST_USE_FAILURE',message:'use failed'})};}
  if(failGenerateOnce&&relative==='/api/trajectories/generate'){failGenerateOnce=false;return {ok:false,status:400,json:async()=>({code:'TEST_GENERATE_FAILURE',message:'generate failed'})};}
  if(interleaveAccount&&relative==='/api/trajectories/generate'){const id=interleaveAccount;interleaveAccount=null;await fetchPage(`/api/accounts/${id}/use`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}
  const response=await fetch(base+relative,{...options,headers:{...options.headers,...(cookie?{Cookie:cookie}:{})}});
  if(response.headers.get('set-cookie'))cookie=response.headers.get('set-cookie').split(';')[0];
  if(relative.startsWith('/api/auto-upload/')&&relative.endsWith('/next')){
   if(dropNextResponse){dropNextResponse=false;await response.arrayBuffer();return {ok:false,status:502,json:async()=>({code:'TEST_LOST_REPLY',message:'reply lost'})};}
   if(stopAfterNext){stopAfterNext=false;await elements.get('stopAuto').onclick();}
  }
  return response;
 }
 const context=vm.createContext({document:{getElementById:id=>{assert(elements.has(id),id);return elements.get(id);},createElement:tag=>new Element(tag)},window:{devicePixelRatio:1,addEventListener(){}},sessionStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)},fetch:fetchPage,Blob,URL,console});
 vm.runInContext(fs.readFileSync(path.join(root,'web_v3/app.js'),'utf8'),context);
 const run=source=>vm.runInContext(source,context);
 await until(()=>elements.get('accounts').children.length===2);
 const accounts=run('state.accounts');const a=accounts.find(x=>x.label==='Account A').id,b=accounts.find(x=>x.label==='Account B').id;
 await run(`selectAccount('${a}')`);assert.equal(elements.get('totalKm').textContent,'12.50 km');
 await run('generate()');assert.equal(elements.get('pointCount').textContent,'451');assert(!elements.get('upload').disabled);
 const first=JSON.stringify(run('state.plan.points'));await run('generate()');assert.notEqual(first,JSON.stringify(run('state.plan.points')));
 const generationRequest=calls.filter(x=>x[1]==='/api/trajectories/generate').at(-1)[2];assert.equal(generationRequest.seed,-1);assert(!('clockScale'in generationRequest));
 await run(`selectAccount('${b}')`);assert.equal(run('state.plan'),null);assert(elements.get('upload').disabled);assert.equal(elements.get('totalKm').textContent,'25.00 km');
 await fetchPage('/api/qa',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expireB:true})});
 await run(`selectAccount('${b}')`);await run('generate()');
 failSessionOnce=true;await run('upload()');assert.equal(run('state.receipt.state'),'accepted');assert(elements.get('upload').disabled);assert(elements.get('result').textContent.includes('服务器判定无效'));
 await run('upload()');let qa=await(await fetchPage('/api/qa',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();assert.equal(qa.data.uploads,1);assert.equal(qa.data.refreshes,1);
 const inspectQA=async body=>(await(await fetchPage('/api/qa',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json()).data;
 elements.get('laps').value='4';elements.get('sigma').value='0.7';elements.get('cap').value='3';elements.get('correlation').value='6';elements.get('seed').value='42';
 const oldGeneration=run('state.plan.generationId');const beforeQuick=calls.length;
 const firstQuick=run(`quickUpload('${a}')`);assert(run('state.busy'));assert(elements.get('accounts').querySelectorAll('button').every(button=>button.disabled));
 const secondClick=run(`quickUpload('${a}')`);await Promise.all([firstQuick,secondClick]);
 assert.equal(run('state.home.account.id'),a);assert.equal(run('state.plan.accountId'),a);assert.equal(run('state.receipt.accountId'),a);assert.notEqual(run('state.plan.generationId'),oldGeneration);
 assert.deepEqual(JSON.parse(JSON.stringify(run('state.plan.config'))),{laps:4,sigmaMeters:.7,maxOffsetMeters:3,correlationSeconds:6,seed:42});
 const quickCalls=calls.slice(beforeQuick);assert.deepEqual(quickCalls.slice(0,3).map(row=>row[1]),[`/api/accounts/${a}/use`,'/api/trajectories/generate',`/api/trajectories/${run('state.plan.generationId')}/upload`]);
 assert.equal(quickCalls.filter(row=>row[1].endsWith('/upload')).length,1);
 qa=await inspectQA();assert.equal(qa.uploads,2);assert.equal(qa.uploadAccounts.at(-1),'A');assert.equal(qa.lastUploadPointCount,601);
 const fixedId=run('state.plan.generationId'),fixedPoints=JSON.stringify(run('state.plan.points'));
 await run(`quickUpload('${a}')`);assert.notEqual(run('state.plan.generationId'),fixedId);assert.equal(JSON.stringify(run('state.plan.points')),fixedPoints);
 elements.get('seed').value='-1';await run(`quickUpload('${b}')`);const randomPoints=JSON.stringify(run('state.plan.points'));
 await run(`quickUpload('${b}')`);assert.notEqual(JSON.stringify(run('state.plan.points')),randomPoints);assert.equal(run('state.plan.entropySource'),'os-csprng');
 qa=await inspectQA();assert.equal(qa.uploads,5);assert.equal(qa.uploadAccounts.at(-1),'B');
 for(const invalid of ['-2','','1.5','2147483648']){elements.get('seed').value=invalid;run('controls()');assert(elements.get('accounts').querySelectorAll('button').filter(button=>button.name==='quickUpload').every(button=>button.disabled));const before=calls.length;await run(`quickUpload('${a}')`);assert.equal(calls.length,before);}
 elements.get('seed').value='-1';run('controls()');
 const stale=run('state.plan.generationId');failUseOnce=true;await run(`quickUpload('${a}')`);assert.equal(run('state.plan.generationId'),stale);assert.equal((await inspectQA()).uploads,5);
 failGenerateOnce=true;await run(`quickUpload('${a}')`);assert.equal(run('state.plan'),null);assert.equal((await inspectQA()).uploads,5);assert(!run('state.busy'));
 interleaveAccount=b;await run(`quickUpload('${a}')`);assert.equal(run('state.home'),null);assert.equal(run('state.plan'),null);assert.equal((await inspectQA()).uploads,5);
 await inspectQA({failUpload:true});await run(`quickUpload('${b}')`);assert.equal(run('state.receipt.state'),'unknown');assert(elements.get('upload').disabled);assert.equal((await inspectQA()).uploads,6);await inspectQA({failUpload:false});
 await run(`deleteAccount('${a}')`);assert.equal(run('state.accounts.length'),1);await elements.get('undoDelete').onclick();assert.equal(run('state.accounts.length'),2);
 await run(`deleteAccount('${b}')`);assert.equal(run('state.home'),null);assert.equal(run('state.plan'),null);assert(elements.get('upload').disabled);
 elements.get('phone').value='test-phone';elements.get('password').value='test-password';elements.get('label').value='Account A';
 await run('addAccount({preventDefault(){}})');assert.equal(elements.get('password').value,'');assert.equal(run('state.accounts.length'),1);
 await run(`api('/api/accounts/${b}/restore','POST',{})`);await run('loadAccounts()');await inspectQA({resetAutomatic:true});
 assert(run('state.accounts.every(account=>account.targetMileageKm===100)'));
 const draftInput=elements.get('accounts').children[0].querySelectorAll('input')[0];draftInput.value='99';draftInput.oninput();assert(elements.get('autoUpload').disabled);
 const beforeDraft=calls.length;await run('autoUploadAll()');assert.equal(calls.length,beforeDraft);
 await run(`saveTarget('${a}','30')`);await run(`saveTarget('${b}','60')`);
 assert.equal(run(`state.accounts.find(account=>account.id==='${a}').targetMileageKm`),30);
 assert.equal(run(`state.accounts.find(account=>account.id==='${b}').targetMileageKm`),60);
 await run(`saveTarget('${a}',null)`);assert.equal(run(`state.accounts.find(account=>account.id==='${a}').targetMileageKm`),100);
 await run(`saveTarget('${a}','0')`);elements.get('laps').value='';elements.get('seed').value='42';run('controls()');assert(!elements.get('autoUpload').disabled);
 await run('previewAuto()');const preview=run('state.autoJob');assert(preview.preview);assert.equal(preview.items[0].code,'TARGET_REACHED');assert.equal(preview.items[1].plannedMileageKm,4);assert.equal(preview.items[1].laps,10);assert.equal((await inspectQA()).uploads,0);
 const autoPromise=run('autoUploadAll()'),autoDoubleClick=run('autoUploadAll()');await Promise.all([autoPromise,autoDoubleClick]);
 assert.equal(run('state.autoJob.state'),'completed');assert.equal(run('state.autoJob.items[0].state'),'skipped');assert.equal(run('state.autoJob.items[1].state'),'accepted');
 assert.equal((await inspectQA()).uploads,1);assert.equal((await inspectQA()).lastUploadPointCount,1501);
 const startRequest=calls.filter(row=>row[1]==='/api/auto-upload').at(-1)[2];assert(!('laps'in startRequest.settings));assert.equal(startRequest.settings.seed,42);
 await run(`saveTarget('${a}','100')`);dropNextResponse=true;const beforeLost=calls.length;await run('autoUploadAll()');
 assert(run('activeBatch()'));assert(!run('state.busy'));const resumeId=run('state.autoJob.id');assert.equal(run('state.autoJob.items[0].state'),'accepted');assert.equal((await inspectQA()).uploads,2);
 assert(!elements.get('autoUpload').disabled);assert.equal(elements.get('autoUpload').textContent,'继续未完成批次');
 await run('autoUploadAll()');assert.equal(run('state.autoJob.id'),resumeId);assert.equal(run('state.autoJob.state'),'completed');assert.equal((await inspectQA()).uploads,3);
 assert.equal(calls.slice(beforeLost).filter(row=>row[1]==='/api/auto-upload').length,1);
 stopAfterNext=true;await run('autoUploadAll()');assert.equal(run('state.autoJob.state'),'cancelled');assert.equal(run('state.autoJob.items[1].state'),'skipped');assert.equal((await inspectQA()).uploads,4);
 const result={removedHistoryAndTimer:true,systemRandomGeneration:true,accountSelectionAndMileage:true,accountSwitchIsolation:true,encryptedTokenRefresh:true,directUploadWithoutSave:true,acceptedReceiptSurvivesReadFailure:true,uploadIdempotency:true,accountDeleteAndUndo:true,passwordInputCleared:true,
  quickUsesCurrentSettings:true,quickGeneratesFreshId:true,quickChoosesClickedAccount:true,quickDoubleClickOnlyOneUpload:true,fixedSeedReproducible:true,minusOneSeedRandom:true,invalidSeedBlocked:true,useOrGenerateFailureNeverUploadsOldTrack:true,interleavedAccountChangeBlocked:true,unknownUploadNeverRetried:true,
  perAccountTargetsIndependent:true,restoreSemesterTarget:true,unsavedTargetsBlockAutomatic:true,previewWithoutUpload:true,automaticUsesCalculatedLaps:true,automaticDoubleClickGuard:true,automaticResponseLossDoesNotResend:true,automaticResumeAndStop:true,externalRequests:0,mockUploads:10,domIds:elements.size};
 fs.writeFileSync(path.join(__dirname,'v3-web-verification.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}finally{backend.kill();}})().catch(error=>{console.error(error);process.exitCode=1;});
