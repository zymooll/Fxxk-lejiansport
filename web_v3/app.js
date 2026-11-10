'use strict';
const $=id=>document.getElementById(id);
const state={accounts:[],home:null,plan:null,receipt:null,busy:false,quickAccount:null,trace:[],deleted:null,recovery:null,recoveryFailed:false,targetDrafts:{},autoJob:null,autoRunning:false,stopRequested:false};
const selectionKey='run-mvp-selected-account-v3';
function savedAccount(){try{return sessionStorage.getItem(selectionKey);}catch{return null;}}
function remember(id){try{if(id)sessionStorage.setItem(selectionKey,id);else sessionStorage.removeItem(selectionKey);}catch{}}
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);}
function node(tag,text,cls){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;}
function trace(method,path,status){state.trace.unshift(`${new Date().toLocaleTimeString()} ${method} ${path} · ${status}`);state.trace=state.trace.slice(0,20);$('trace').replaceChildren(...state.trace.map(row=>node('li',row)));}
async function api(path,method='GET',body,allowRecovery=true){
 const response=await fetch(path,{method,credentials:'same-origin',headers:method==='POST'?{'Content-Type':'application/json'}:{},...(body!==undefined?{body:JSON.stringify(body)}:{})});
 const result=await response.json();trace(method,path,response.status);
 if(!response.ok){
  if(method==='GET'&&allowRecovery&&result.code==='LOCAL_SESSION_REQUIRED'&&savedAccount()&&!state.recoveryFailed){
   if(!state.recovery)state.recovery=(async()=>{try{await api(`/api/accounts/${savedAccount()}/use`,'POST',{},false);}catch(error){state.recoveryFailed=true;throw error;}finally{state.recovery=null;}})();
   await state.recovery;return api(path,method,body,false);
  }
  const error=new Error(result.message||'请求失败');error.code=result.code;error.details=result.details;throw error;
 }
 return result.data;
}
function valid(){const value=$('laps').value.trim(),number=Number(value);return value&&Number.isInteger(number)&&number>=1&&number<=60;}
function generationSettings(){return {laps:Number($('laps').value),sigmaMeters:Number($('sigma').value),maxOffsetMeters:Number($('cap').value),correlationSeconds:Number($('correlation').value),seed:Number($('seed').value)};}
function validAutoSettings(){
 if(['sigma','cap','correlation','seed'].some(id=>!$(id).value.trim()))return false;
 const c=generationSettings();return Number.isFinite(c.sigmaMeters)&&c.sigmaMeters>=0&&c.sigmaMeters<=2&&Number.isFinite(c.maxOffsetMeters)&&c.maxOffsetMeters>=c.sigmaMeters&&c.maxOffsetMeters<=5&&Number.isFinite(c.correlationSeconds)&&c.correlationSeconds>=.5&&c.correlationSeconds<=30&&Number.isInteger(c.seed)&&c.seed>=-1&&c.seed<=2147483647;
}
function validSettings(){return Boolean(valid())&&validAutoSettings();}
function activeBatch(){return state.autoJob&&!state.autoJob.preview&&['running','paused'].includes(state.autoJob.state);}
function locked(){return state.busy||Boolean(activeBatch());}
function unsavedTargets(){return state.accounts.some(account=>Object.hasOwn(state.targetDrafts,account.id));}
function estimate(){const meters=state.home?.trajectory.referenceLapMeters;$('estimatedKm').textContent=!valid()?'请输入1–60的整数':meters?`${(meters*Number($('laps').value)/1000).toFixed(3)} km`:'— km';}
function controls(){
 const busy=locked();
 $('settings').disabled=!state.home||busy;$('generate').disabled=!state.home||busy||!validSettings();
 $('upload').disabled=!state.plan||busy||['accepted','unknown','sending'].includes(state.receipt?.state);
 $('upload').textContent=state.receipt?.state==='accepted'?'已提交':state.receipt?.state==='unknown'?'结果待核对':'上传本次轨迹';
 $('export').disabled=!state.plan||busy;$('verify').disabled=state.receipt?.state!=='accepted'||busy;
 for(const id of ['refreshAccounts','importLocal','showAdd','addAccount','refreshCurrent','disconnect'])$(id).disabled=busy;
 $('undoDelete').disabled=busy;
 for(const button of $('accounts').querySelectorAll('button'))button.disabled=busy||(button.name==='quickUpload'&&!validSettings());
 for(const input of $('accounts').querySelectorAll('input'))input.disabled=busy;
 $('previewAuto').disabled=busy||!validAutoSettings()||!state.accounts.length||unsavedTargets();
 $('autoUpload').disabled=state.busy||!validAutoSettings()||!state.accounts.length||unsavedTargets();
 $('autoUpload').textContent=activeBatch()?'继续未完成批次':'自动一键上传';
 $('stopAuto').disabled=!activeBatch();
}
function resetPlan(){state.plan=null;state.receipt=null;$('trackKm').textContent=$('pointCount').textContent=$('plannedDuration').textContent=$('insideRatio').textContent='—';$('planState').textContent='等待生成';$('result').textContent='生成轨迹后即可上传。';$('emptyMap').classList.remove('hidden');controls();draw();}
function renderAccounts(){
 const nodes=state.accounts.map(account=>{
  const card=node('article',undefined,'account-card');card.classList.toggle('selected',state.home?.account.id===account.id);
  const heading=node('div',undefined,'section-head');heading.append(node('h3',account.label),node('span',({ready:'可使用',pending:'待验证',needs_login:'需要重新登录',error:'连接异常'})[account.state]||'待验证','account-status'));card.append(heading);
  card.append(node('p',[account.name,account.school].filter(Boolean).join(' · ')||'尚未验证身份','small muted'));
  card.append(node('strong',`${Number(account.metrics.totalMileageKm||0).toFixed(2)} km`,'account-km'));
  card.append(node('p',`今日 ${Number(account.metrics.dayMileageKm||0).toFixed(2)} km · 本周 ${Number(account.metrics.weekMileageKm||0).toFixed(2)} km`,'small muted'));
  const targetBox=node('div',undefined,'account-target');const targetLabel=node('label','目标里程 / km');
  const targetInput=node('input');targetInput.type='number';targetInput.min='0';targetInput.max='1000000';targetInput.step='0.01';targetInput.name='targetMileage';
  targetInput.value=state.targetDrafts[account.id]??(account.targetMileageKm==null?'':String(account.targetMileageKm));
  targetInput.placeholder='学期目标待读取';targetInput.oninput=()=>{state.targetDrafts[account.id]=targetInput.value;controls();};targetLabel.append(targetInput);
  const targetActions=node('div',undefined,'row wrap');const save=node('button','保存目标','secondary');save.onclick=()=>saveTarget(account.id,targetInput.value);
  const follow=node('button','恢复学期目标','text-button');follow.onclick=()=>saveTarget(account.id,null);targetActions.append(save,follow);
  const targetText=account.targetMileageKm==null?'学期目标待读取':`${account.targetSource==='custom'?'自定目标':'学期目标'} ${Number(account.targetMileageKm).toFixed(2)} km · 剩余 ${Number(account.remainingMileageKm||0).toFixed(2)} km`;
  targetBox.append(targetLabel,targetActions,node('p',targetText,'small muted'));card.append(targetBox);
  if(account.error)card.append(node('p',account.error.message,'small error-text'));
  const actions=node('div',undefined,'row');const use=node('button',state.home?.account.id===account.id?'更新并使用':'使用账号','primary');use.onclick=()=>selectAccount(account.id);
  const quick=node('button',state.quickAccount===account.id?'正在生成并上传…':'生成并上传','secondary quick-upload');quick.name='quickUpload';quick.title='使用当前生成设置，为此账号新生成一条轨迹并立即上传';quick.onclick=()=>quickUpload(account.id);
  const remove=node('button','删除','danger');remove.onclick=()=>deleteAccount(account.id);actions.append(use,quick,remove);card.append(actions);return card;
 });
 $('accounts').replaceChildren(...(nodes.length?nodes:[node('p','账号池为空。添加账号或导入已有本机凭据。','muted')]));controls();
}
async function loadAccounts(){state.accounts=await api('/api/accounts');renderAccounts();}
async function saveTarget(id,value){
 if(locked())return;
 if(value!==null&&(!String(value).trim()||!Number.isFinite(Number(value))||Number(value)<0||Number(value)>1000000)){notice('目标里程应为0–1000000公里。',true);return;}
 state.busy=true;controls();
 try{const account=await api(`/api/accounts/${id}/target`,'POST',{targetMileageKm:value===null?null:Number(value)});delete state.targetDrafts[id];if(state.home?.account.id===id)state.home.account=account;await loadAccounts();notice(value===null?'已恢复跟随学期目标。':'该账号的目标里程已保存。');}
 catch(error){notice(error.message,true);}finally{state.busy=false;controls();}
}
function renderHome(home){
 state.home=home;$('currentPanel').classList.remove('hidden');$('currentName').textContent=`${home.account.label} · ${home.account.name||'已连接'}`;
 const m=home.account.metrics;$('totalKm').textContent=`${Number(m.totalMileageKm||0).toFixed(2)} km`;$('dayKm').textContent=`${Number(m.dayMileageKm||0).toFixed(2)} km`;$('weekKm').textContent=`${Number(m.weekMileageKm||0).toFixed(2)} km`;$('totalParts').textContent=m.totalPart??'—';
 const r=home.runningLimit;$('rules').textContent=`当前学期：${m.semester?.yearRange||'—'} · 单次 ${r.effectiveMileageStart??'—'}–${r.effectiveMileageEnd??'—'} km · 日上限 ${r.dailyMileage??'—'} km · 读取于 ${home.account.lastCheckedAt?new Date(home.account.lastCheckedAt).toLocaleString():'—'}`;
 estimate();renderAccounts();controls();draw();
}
async function selectAccount(id,reset=true){
 if(locked())return;state.busy=true;controls();
 try{const home=await api(`/api/accounts/${id}/use`,'POST',{});remember(home.account.id);state.recoveryFailed=false;if(reset||home.account.id!==state.home?.account.id)resetPlan();renderHome(home);await loadAccounts();notice('账号已连接，凭据及最新里程已检查。');}
 catch(error){notice(error.message,true);await loadAccounts().catch(()=>{});}
 finally{state.busy=false;controls();}
}
async function deleteAccount(id){
 if(locked())return;state.busy=true;controls();
 try{await api(`/api/accounts/${id}/delete`,'POST',{});state.deleted=id;$('undoBox').classList.remove('hidden');if(state.home?.account.id===id){remember(null);state.home=null;$('currentPanel').classList.add('hidden');resetPlan();}await loadAccounts();notice('账号已移除，可撤销。');}
 catch(error){notice(error.message,true);}finally{state.busy=false;controls();}
}
async function addAccount(event){
 event.preventDefault();if(locked())return;
 let payload={label:$('label').value,mode:$('addMode').value};
 if(payload.mode==='password'){payload.phone=$('phone').value;payload.password=$('password').value;if(!payload.phone||!payload.password){notice('请输入账号和密码。',true);return;}}
 else{try{payload.credentials=JSON.parse($('tokenJson').value);}catch{notice('请输入有效的凭据 JSON。',true);return;}}
 $('password').value='';$('tokenJson').value='';state.busy=true;controls();
 try{const result=await api('/api/accounts','POST',payload);$('addForm').classList.add('hidden');await loadAccounts();notice(result.verified?'账号已添加并验证，可以选择使用。':'凭据已加密保存，暂时无法读取账户；可点击“使用账号”重试。',!result.verified);}
 catch(error){notice(error.message,true);}finally{payload=null;state.busy=false;controls();}
}
async function generate(){
 if(locked()||!state.home||!validSettings())return;state.busy=true;controls();
 try{const plan=await api('/api/trajectories/generate','POST',generationSettings());renderPlan(plan);notice(plan.entropySource==='os-csprng'?'轨迹已生成，使用系统安全随机源。':`轨迹已生成，使用种子 ${plan.config.seed}。`);
 }catch(error){notice(error.message,true);}finally{state.busy=false;controls();}
}
function renderPlan(plan){
  if(plan.accountId!==state.home?.account.id){
   state.home=null;remember(null);$('currentPanel').classList.add('hidden');resetPlan();renderAccounts();
   throw new Error('当前账号已变化，请重新选择账号或点击该账号的“生成并上传”。');
  }
  state.plan=plan;state.receipt=null;
  $('trackKm').textContent=`${(plan.observedDistanceMeters/1000).toFixed(3)} km`;$('pointCount').textContent=plan.pointCount;
  const seconds=Math.round(plan.durationSeconds);$('plannedDuration').textContent=`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;$('insideRatio').textContent=`${(plan.regionCheck.ratio*100).toFixed(1)}%`;
  $('planState').textContent='已生成';$('emptyMap').classList.add('hidden');$('result').textContent='本次完整轨迹已生成，可直接上传。';draw();
}
function renderReceipt(){
 const r=state.receipt;if(!r)return;
 const lines=[r.state==='accepted'?'服务器已接收':r.state==='unknown'?'提交结果待核对，禁止重复发送':'提交失败',r.remoteRecordId?`记录 ID：${r.remoteRecordId}`:''];
 if(r.serverRecord){const s=r.serverRecord;lines.push(s.status==='effective'?`服务器判定有效 · ${Number(s.effectiveMileageKm).toFixed(2)} km`:s.status==='ineffective'?`服务器判定无效：${s.uneffectiveReason||'未计入有效里程'}`:s.message||'回查结果待更新');}
 $('result').replaceChildren(...lines.filter(Boolean).map(line=>node('p',line)));
}
async function upload(){
 if(locked()||!state.plan)return;state.busy=true;controls();
 try{await submitPlan(state.plan);notice('提交回执已返回，请查看服务器判定。');}
 catch(error){reportUploadError(error);}
 finally{state.busy=false;controls();}
}
async function submitPlan(plan){state.receipt=await api(`/api/trajectories/${plan.generationId}/upload`,'POST',{});renderReceipt();renderHome(await api('/api/session'));await loadAccounts();}
function reportUploadError(error){
 if(state.receipt?.state!=='accepted'){if(error.details?.outcomeUncertain||error.code==='UPLOAD_OUTCOME_UNKNOWN')state.receipt={state:'unknown'};else state.receipt={state:'failed'};}
 renderReceipt();notice(state.receipt?.state==='accepted'?`服务器已接收，里程更新失败：${error.message}`:error.message,true);
}
async function quickUpload(accountId){
 if(locked())return;
 if(!validSettings()){notice('请检查生成设置；种子应为 -1 或 0–2147483647 的整数。',true);return;}
 const config=generationSettings();let uploading=false;
 state.busy=true;state.quickAccount=accountId;controls();notice('正在检查账号并生成新轨迹…');
 try{
  const home=await api(`/api/accounts/${accountId}/use`,'POST',{});remember(home.account.id);state.recoveryFailed=false;
  resetPlan();renderHome(home);
  const plan=await api('/api/trajectories/generate','POST',config);
  renderPlan(plan);uploading=true;
  await submitPlan(plan);notice('新轨迹已生成并提交，请查看服务器判定。');
 }catch(error){if(uploading)reportUploadError(error);else notice(error.message,true);}
 finally{state.busy=false;state.quickAccount=null;renderAccounts();controls();}
}
function autoSettings(){const config=generationSettings();delete config.laps;return config;}
function renderAuto(job){
 state.autoJob=job;
 if(!job){$('autoResults').classList.add('hidden');$('autoProgress').textContent='尚未开始批量操作。';controls();return;}
 const items=job.items||[];const pending=items.filter(item=>['pending','checking','sending','ready'].includes(item.state)).length;
 const accepted=items.filter(item=>item.state==='accepted').length,skipped=items.filter(item=>item.state==='skipped').length;
 const failed=items.filter(item=>['failed','unknown'].includes(item.state)).length;
 $('autoProgress').textContent=`${job.preview?'计划预览':job.state==='completed'?'批次完成':job.state==='cancelled'?'已停止后续':'批次进度'}：${items.length} 个账号 · 已接收 ${accepted} · 跳过 ${skipped} · 异常 ${failed} · 待处理 ${pending}`;
 $('autoResults').classList.remove('hidden');
 $('autoResults').replaceChildren(...items.map(item=>{
  const row=node('div',undefined,'auto-result');row.append(node('strong',item.label||'账号'));
  const numbers=item.targetMileageKm==null?'':`目标 ${Number(item.targetMileageKm).toFixed(2)} / 累计 ${Number(item.totalMileageKm||0).toFixed(2)} km`;
  row.append(node('span',numbers,'small muted'));
  if(item.laps)row.append(node('span',`本次额度 ${Number(item.plannedMileageKm).toFixed(2)} km · ${item.laps} 圈`,'small'));
  let text=item.reason||'等待处理';const server=item.receipt?.serverRecord;
  if(server)text+=server.status==='effective'?` · 有效 ${Number(server.effectiveMileageKm).toFixed(2)} km`:server.status==='ineffective'?` · 无效：${server.uneffectiveReason||'未计入有效里程'}`:' · 等待官方判定';
  row.append(node('span',text,['failed','unknown'].includes(item.state)?'small error-text':'small'));return row;
 }));controls();
}
async function previewAuto(){
 if(locked()||!validAutoSettings())return;
 if(unsavedTargets()){notice('请先保存或恢复修改中的账号目标。',true);return;}
 state.busy=true;controls();
 try{renderAuto(await api('/api/auto-upload/preview','POST',{settings:autoSettings()}));await loadAccounts();notice('已按最新目标与额度计算计划，尚未上传。');}
 catch(error){notice(error.message,true);}finally{state.busy=false;controls();}
}
async function autoUploadAll(){
 if(state.busy||!validAutoSettings())return;
 if(unsavedTargets()){notice('请先保存或恢复修改中的账号目标。',true);return;}
 state.busy=true;state.autoRunning=true;state.stopRequested=false;controls();
 try{
  let job=activeBatch()?await api(`/api/auto-upload/${state.autoJob.id}`):await api('/api/auto-upload','POST',{settings:autoSettings()});renderAuto(job);
  while(['running','paused'].includes(job.state)){
   if(state.stopRequested){job=await api(`/api/auto-upload/${job.id}/cancel`,'POST',{});renderAuto(job);break;}
   const next=job.items.find(item=>item.state==='pending');if(next)notice(`正在检查 ${next.label} 的目标与额度…`);
   job=await api(`/api/auto-upload/${job.id}/next`,'POST',{});renderAuto(job);
  }
  await loadAccounts();if(state.home){try{renderHome(await api('/api/session'));}catch{}}
  notice(job.state==='cancelled'?'已停止后续账号处理。':'批量处理完成，请查看每个账号的回执与跳过原因。');
 }catch(error){
  try{renderAuto(await api('/api/auto-upload/current'));}catch{}
  notice(`${error.message}；未自动重发，可继续尚未处理的账号。`,true);
 }finally{state.autoRunning=false;state.busy=false;renderAccounts();controls();}
}
async function stopAuto(){
 if(!activeBatch())return;state.stopRequested=true;
 if(state.autoRunning){notice('当前账号处理完成后将停止后续账号。');return;}
 try{renderAuto(await api(`/api/auto-upload/${state.autoJob.id}/cancel`,'POST',{}));notice('已停止未处理账号。');}
 catch(error){notice(error.message,true);}finally{controls();}
}
function draw(){
 const canvas=$('track'),rect=canvas.getBoundingClientRect();if(!rect.width)return;const dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,rect.width,rect.height);
 if(!state.home)return;const ref=state.home.reference,origin=ref[0],project=p=>[(p.longitude-origin.longitude)*111320*Math.cos(origin.latitude*Math.PI/180),-(p.latitude-origin.latitude)*111320];
 const polygons=state.home.region.polygons||[],excluded=state.home.region.excludedPolygons||[];const bound=($('fullRegion').checked?[...ref,...polygons.flat()]:ref).map(project),xs=bound.map(p=>p[0]),ys=bound.map(p=>p[1]);const minX=Math.min(...xs)-20,maxX=Math.max(...xs)+20,minY=Math.min(...ys)-20,maxY=Math.max(...ys)+20,scale=Math.min((rect.width-30)/(maxX-minX),(rect.height-30)/(maxY-minY));
 const position=p=>{const [x,y]=project(p);return [(x-(minX+maxX)/2)*scale+rect.width/2,(y-(minY+maxY)/2)*scale+rect.height/2];};
 function path(points,stroke,fill,width=1){if(!points.length)return;ctx.beginPath();points.forEach((p,i)=>{const [x,y]=position(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});if(fill){ctx.closePath();ctx.fillStyle=fill;ctx.fill();}ctx.strokeStyle=stroke;ctx.lineWidth=width;ctx.stroke();}
 for(const polygon of polygons)path(polygon,'#77a68a','#dceee2');for(const polygon of excluded)path(polygon,'#c97d64','#f8e0d7');path(ref,'#2f7ba7',null,2);if(state.plan)path(state.plan.points,'#c58427',null,1.5);
}
$('showAdd').onclick=()=>$('addForm').classList.toggle('hidden');$('closeAdd').onclick=()=>{$('addForm').classList.add('hidden');$('password').value='';$('tokenJson').value='';};
$('addMode').onchange=()=>{const password=$('addMode').value==='password';$('passwordFields').classList.toggle('hidden',!password);$('tokenFields').classList.toggle('hidden',password);$('password').value='';$('tokenJson').value='';};
$('addForm').onsubmit=addAccount;$('refreshAccounts').onclick=()=>loadAccounts().catch(error=>notice(error.message,true));
$('importLocal').onclick=async()=>{if(state.busy)return;state.busy=true;controls();try{const data=await api('/api/accounts/import-local','POST',{label:'已有本机账号'});await loadAccounts();notice(data.verified?'已有账号已导入。':'账号已保存，需重新连接验证。',!data.verified);}catch(error){notice(error.message,true);}finally{state.busy=false;controls();}};
$('undoDelete').onclick=async()=>{if(!state.deleted||state.busy)return;state.busy=true;controls();try{await api(`/api/accounts/${state.deleted}/restore`,'POST',{});state.deleted=null;$('undoBox').classList.add('hidden');await loadAccounts();notice('账号已恢复。');}catch(error){notice(error.message,true);}finally{state.busy=false;controls();}};
$('refreshCurrent').onclick=()=>state.home&&selectAccount(state.home.account.id,false);
$('disconnect').onclick=async()=>{if(state.busy)return;remember(null);try{await api('/api/auth/logout','POST',{});}catch{}state.home=null;$('currentPanel').classList.add('hidden');resetPlan();renderAccounts();notice('已取消账号选择。');};
$('generate').onclick=generate;$('upload').onclick=upload;
$('previewAuto').onclick=previewAuto;$('autoUpload').onclick=autoUploadAll;$('stopAuto').onclick=stopAuto;
$('verify').onclick=async()=>{if(state.busy||!state.plan)return;state.busy=true;controls();try{state.receipt=await api(`/api/trajectories/${state.plan.generationId}/verify`,'POST',{});renderReceipt();}catch(error){notice(error.message,true);}finally{state.busy=false;controls();}};
for(const id of ['laps','sigma','cap','correlation','seed'])$(id).addEventListener('input',()=>{estimate();resetPlan();});
for(const [id,delta] of [['minus',-1],['plus',1]])$(id).onclick=()=>{if(!valid())return;$('laps').value=String(Math.min(60,Math.max(1,Number($('laps').value)+delta)));estimate();resetPlan();};
$('export').onclick=()=>{if(!state.plan)return;const url=URL.createObjectURL(new Blob([JSON.stringify(state.plan,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download=`trajectory-${state.plan.generationId}.json`;a.click();URL.revokeObjectURL(url);};
$('fullRegion').onchange=draw;window.addEventListener('resize',draw);
(async()=>{try{const health=await api('/api/health');$('importLocal').classList.toggle('hidden',!health.legacyCredentialFileConfigured);await loadAccounts();renderAuto(await api('/api/auto-upload/current'));if(savedAccount()&&state.accounts.some(a=>a.id===savedAccount())){try{renderHome(await api('/api/session'));notice('账号会话已恢复。');}catch(error){notice(error.message,true);}}else{remember(null);notice(activeBatch()?'发现未结束批次，可继续未处理账号或停止后续。':'选择一个账号，或添加新账号。');}estimate();controls();}catch(error){notice(error.message,true);}})();
