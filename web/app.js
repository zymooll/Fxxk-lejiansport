'use strict';
const $=id=>document.getElementById(id);
const state={home:null,plan:null,run:null,result:null,uploading:false,poll:null,lastPoll:0,lastElapsed:0,trace:[],draft:null,authenticating:false,generating:false,starting:false,generationOnly:false,requestSerial:0,savedGenerationId:null,generatedRecordId:null,lastDeleted:null,recordBusy:null,historyButtons:[],limits:{lapsMin:1,lapsMax:60,referenceLapMeters:null}};
const labels={running:'计时中',ready:'可以提交',uploaded:'本地已保存',cancelled:'已取消',interrupted:'已中断',generated:'轨迹已保存（未计时）'};
const fmt=s=>`${String(Math.floor(Math.max(0,s)/60)).padStart(2,'0')}:${String(Math.floor(Math.max(0,s)%60)).padStart(2,'0')}`;
const notice=(message,error=false)=>{$('notice').textContent=message;$('notice').classList.toggle('error',error);};
const savedSessionKey='run-mvp-saved-credential-session';
try{state.savedCredentialSession=sessionStorage.getItem(savedSessionKey)==='1';}catch{state.savedCredentialSession=false;}
function rememberSavedSession(saved){
 state.savedCredentialSession=Boolean(saved);state.sessionRecoveryFailed=false;
 try{if(saved)sessionStorage.setItem(savedSessionKey,'1');else sessionStorage.removeItem(savedSessionKey);}catch{}
}
async function recoverSavedSession(){
 if(state.sessionRecovery)return state.sessionRecovery;
 state.sessionRecovery=(async()=>{
  try{await api('/api/auth/remote-file','POST',{},false,{},false);notice('本地会话已使用本机凭据自动恢复。');}
  catch(error){state.sessionRecoveryFailed=true;throw error;}
 })();
 try{return await state.sessionRecovery;}finally{state.sessionRecovery=null;}
}
function log(method,path,status,ms){state.trace.unshift({method,path,status,ms,time:new Date().toLocaleTimeString()});state.trace=state.trace.slice(0,45);$('trace').replaceChildren(...state.trace.map(r=>{const li=document.createElement('li');const a=document.createElement('span');a.textContent=`${r.time} ${r.method} ${r.path}`;const b=document.createElement('span');b.textContent=`${r.status} · ${r.ms}ms`;b.className=r.status>=200&&r.status<400?'ok':'bad';li.append(a,b);return li;}));}
async function api(path,method='GET',body,quiet=false,extra={},allowRecovery=true){
 const start=performance.now();let response;
 try{
  response=await fetch(path,{method,credentials:'same-origin',headers:{...(method==='POST'?{'Content-Type':'application/json'}:{}),...extra},...(body!==undefined?{body:JSON.stringify(body)}:{})});
  const result=await response.json();if(!quiet)log(method,path,response.status,Math.round(performance.now()-start));
  if(!response.ok){
   if(method==='GET'&&allowRecovery&&response.status===401&&result.code==='LOCAL_SESSION_REQUIRED'&&state.savedCredentialSession&&!state.sessionRecoveryFailed){
    await recoverSavedSession();return api(path,method,body,quiet,extra,false);
   }
   const e=new Error(result.message||'请求失败');e.code=result.code;e.details=result.details;e.status=response.status;throw e;
  }
  return result.data;
 }catch(error){if(!response&&!quiet)log(method,path,0,Math.round(performance.now()-start));throw error;}
}
function config(){return {laps:Number($('laps').value),clockScale:Number($('clock').value),sigmaMeters:Number($('sigma').value),maxOffsetMeters:Number($('cap').value),correlationSeconds:Number($('correlation').value),seed:Number($('seed').value)};}
function active(){return state.run&&['running','ready'].includes(state.run.state);}
function validLaps(){
 const text=$('laps').value.trim(),laps=Number(text);
 return Boolean(text)&&Number.isInteger(laps)&&laps>=state.limits.lapsMin&&laps<=state.limits.lapsMax;
}
function setTrajectoryLimits(value){
 if(value)state.limits={...state.limits,...value};
 $('laps').min=state.limits.lapsMin;$('laps').max=state.limits.lapsMax;updateEstimate();
}
function updateEstimate(){
 const valid=validLaps(),lap=state.limits.referenceLapMeters,output=$('estimatedKm');
 $('laps').setAttribute('aria-invalid',String(!valid));output.classList.toggle('invalid',!valid);
 output.textContent=!valid?`请输入${state.limits.lapsMin}–${state.limits.lapsMax}的整数`:Number.isFinite(lap)?`${(lap*Number($('laps').value)/1000).toFixed(3)} km`:'— km';
 $('lapHint').textContent=Number.isFinite(lap)?`每圈参考约 ${(lap/1000).toFixed(3)} km · 可设 ${state.limits.lapsMin}–${state.limits.lapsMax} 圈；实际生成里程会随偏移略有变化。`:'正在读取参考跑道长度…';
 $('decreaseLaps').disabled=!valid||Number($('laps').value)<=state.limits.lapsMin;
 $('increaseLaps').disabled=!valid||Number($('laps').value)>=state.limits.lapsMax;
}
function controls(){
 const isActive=active(),busy=state.uploading||state.generating||state.starting,invalid=!validLaps();
 $('settings').disabled=!state.home||isActive||busy;$('preview').disabled=invalid||busy;
 $('start').disabled=!state.home||isActive||busy||invalid;$('cancel').disabled=!isActive||busy;
 $('upload').disabled=!state.run||state.run.state!=='ready'||busy;
 $('export').disabled=!(state.result||(state.generationOnly&&state.plan))||busy;
 $('export').textContent=state.generationOnly?'导出完整轨迹 JSON':'导出本地测试记录';
 $('logout').disabled=Boolean(isActive)||busy;$('target').disabled=isActive||busy;
 $('target').options[1].disabled=!state.home?.remote;
 $('commitRemote').disabled=!state.draft||state.draft.state!=='draft'||busy;$('verifyRemote').disabled=state.draft?.state!=='accepted'||busy;
 $('signedFile').disabled=busy;$('refreshRemote').disabled=busy;
 const saved=state.plan&&state.savedGenerationId===state.plan.generationId;
 $('saveGenerated').disabled=!state.generationOnly||!state.plan||saved||busy;
 $('saveGenerated').textContent=saved?'已保存到本地记录':'保存到本地记录';
 $('undoDelete').disabled=!state.lastDeleted||busy;updateHistoryControls(busy);
}
function metrics(){
 const p=state.plan;if(!p)return;
 $('km').textContent=`${(p.observedDistanceMeters/1000).toFixed(3)} km`;$('points').textContent=p.pointCount;
 $('wait').textContent=state.generationOnly?'0 s':`${Math.ceil(state.run?state.run.remainingRealSeconds:p.durationSeconds/p.config.clockScale)} s`;
 $('waitHint').textContent=state.generationOnly?`${p.config.laps}圈 · 轨迹计划时长 ${fmt(p.durationSeconds)} · 已跳过计时，无需等待`:`${p.config.laps}圈 · 模拟${fmt(p.durationSeconds)} · ${p.config.clockScale}×时钟${p.config.clockScale>1?'（仅本地加速测试）':''}`;
 $('emptyMap').classList.add('hidden');
}
function settingsChanged(){
 if(active()||state.generating||state.starting)return;
 ++state.requestSerial;state.generationOnly=false;state.plan=null;state.run=null;state.result=null;state.savedGenerationId=null;state.generatedRecordId=null;
 clearInterval(state.poll);updateEstimate();
 $('km').textContent='—';$('points').textContent='—';$('wait').textContent='—';$('elapsed').textContent='00:00';
 $('runState').textContent='等待生成';$('progress').value=0;
 $('waitHint').textContent='设置已更新，可直接生成轨迹或开始计时测试。';
 $('progressText').textContent='点击“直接生成完整轨迹”，即可跳过计时并导出。';
 $('resultBox').className='result-empty';$('resultBox').textContent='选择圈数后直接生成轨迹，或开始计时测试。';
 $('emptyMap').textContent='点击直接生成完整轨迹';$('emptyMap').classList.remove('hidden');controls();draw();
}
function changeLaps(delta){
 if(!validLaps()||active()||state.generating||state.starting)return;
 $('laps').value=String(Math.min(state.limits.lapsMax,Math.max(state.limits.lapsMin,Number($('laps').value)+delta)));settingsChanged();
}
async function loadHome(quiet=false){
 state.home=await api('/api/home','GET',undefined,quiet);setTrajectoryLimits(state.home.trajectory);
 $('authForm').classList.add('hidden');$('account').classList.remove('hidden');$('homeInfo').classList.remove('hidden');
 const remote=Boolean(state.home.remote);
 $('authState').textContent=remote?'真实鉴权已通过':'本地会话有效';
 $('accountText').textContent=remote?`${state.home.user.name} · 乐健真实服务已连接`:'当前为本地演示会话。';
 $('sourceLabel').textContent=remote?'乐健真实服务':'本地模拟主页';
 $('localRuns').textContent=`${state.home.summary.localRuns} 次`;$('localDistance').textContent=`${state.home.summary.localDistanceKm.toFixed(3)} km`;
 $('remotePanel').classList.toggle('hidden',!remote);$('remoteUploadPanel').classList.toggle('hidden',!remote);
 if(remote){renderRemote();await loadRemoteHistory();}
 renderHistory();controls();
}
async function login(demo,headers,saved=false){
 if(state.authenticating)return;state.authenticating=true;
 for(const id of ['login','savedLogin','demo'])$(id).disabled=true;
 try{
  await api(demo?'/api/auth/demo':saved?'/api/auth/remote-file':'/api/auth/remote','POST',headers||{});
  rememberSavedSession(!demo&&saved);
  await loadHome();notice(demo?'演示会话已建立，主页数据来自本地模拟后端。':'真实鉴权通过，主页与区域已从乐健服务获取。');
  await preview();await restoreActive();
 }catch(e){notice(e.message,true);}finally{state.authenticating=false;for(const id of ['login','savedLogin','demo'])$(id).disabled=false;}
}
async function preview(){
 if(!validLaps()||active()||state.generating||state.starting)return;
 const serial=++state.requestSerial;
 try{
  const plan=await api('/api/preview','POST',config());
  if(serial!==state.requestSerial||active()||state.generating)return;
  state.plan=plan;state.generationOnly=false;state.run=null;state.result=null;
  $('resultBox').className='result-empty';$('resultBox').textContent='可直接生成并导出完整轨迹，或开始计时测试。';
  $('runState').textContent='预览已生成';$('elapsed').textContent='00:00';$('progress').value=0;
  $('progressText').textContent='直接生成可免等待导出；计时测试将播放轨迹。';metrics();controls();draw();
 }catch(e){if(serial===state.requestSerial)notice(e.message,true);}
}
function renderGenerated(){
 const p=state.plan,box=$('resultBox');box.className='success-box';box.replaceChildren();
 const title=document.createElement('strong');title.textContent='完整轨迹已生成 · 已跳过计时';
 const desc=document.createElement('div');desc.className='result-metrics';
 for(const text of [`${p.config.laps} 圈`,`参考预计 ${p.estimatedDistanceKm.toFixed(3)} km`,`生成后 ${(p.observedDistanceMeters/1000).toFixed(3)} km`,`${p.pointCount} 个轨迹点`]){
  const span=document.createElement('span');span.textContent=text;desc.append(span);
 }
 const note=document.createElement('p');note.className='small muted';
 note.textContent=`现在可直接导出 JSON。区域内 ${(p.regionCheck.ratio*100).toFixed(1)}%；${fmt(p.durationSeconds)} 是轨迹计划时长，本次未运行计时。`;
 box.append(title,desc,note);
}
async function generateDirect(){
 if(!state.home||active()||state.uploading||state.generating||state.starting)return;
 if(!validLaps()){notice(`圈数应为${state.limits.lapsMin}–${state.limits.lapsMax}的整数。`,true);return;}
 const serial=++state.requestSerial;state.generating=true;controls();
 try{
  const plan=await api('/api/trajectories/generate','POST',config());
  if(serial!==state.requestSerial)return;
  clearInterval(state.poll);state.plan=plan;state.run=null;state.result=null;state.generationOnly=true;state.savedGenerationId=null;state.generatedRecordId=null;
  $('runState').textContent='轨迹已生成';$('elapsed').textContent='未计时';$('progress').value=1;
  $('progressText').textContent=`完整 ${plan.pointCount} 点轨迹已生成，无需等待。`;
  renderGenerated();metrics();draw();notice(`${plan.config.laps}圈完整轨迹已生成，可立即导出 JSON。`);
 }catch(e){notice(e.message,true);}finally{state.generating=false;controls();}
}
async function start(){
 if(active()||state.generating||state.uploading||state.starting||!validLaps())return;
 ++state.requestSerial;state.starting=true;controls();
 try{
  const data=await api('/api/runs','POST',config());state.generationOnly=false;state.run=data.run;state.plan=data.plan;state.result=null;
  state.lastPoll=performance.now();state.lastElapsed=0;$('resultBox').className='result-empty';$('resultBox').textContent='后端计时中，提前上传会被拒绝。';
  notice(`计时测试已开始。${state.plan.config.clockScale}×时钟，预计实际等待${Math.ceil(state.run.remainingRealSeconds)}秒。`);metrics();controls();beginPolling();
 }catch(e){notice(e.message,true);}finally{state.starting=false;controls();}
}
function beginPolling(){clearInterval(state.poll);state.poll=setInterval(poll,700);poll();}
async function poll(){
 const runId=state.run?.id;if(!runId)return;
 try{
  const prev=state.run.state,data=await api(`/api/runs/${runId}`,'GET',undefined,true);
  if(state.run?.id!==runId)return;
  state.run=data;state.lastPoll=performance.now();state.lastElapsed=state.run.virtualElapsedSeconds;
  $('runState').textContent=labels[state.run.state]||state.run.state;metrics();controls();
  if(state.run.state==='ready'&&prev!=='ready')notice('后端计时已完成，可以保存或准备远端请求。');
  if(state.run.state==='ready'&&$('automatic').checked)await upload();if(!active())clearInterval(state.poll);
 }catch(e){if(state.run?.id!==runId)return;clearInterval(state.poll);notice(`计时状态获取失败：${e.message}`,true);}
}
async function upload(){if(!state.run||state.uploading)return;if($('target').value==='remote')return prepareRemoteRun();state.uploading=true;controls();try{const payload={isSimulated:true,referenceHash:state.home.reference.hash,points:state.plan.points};state.result=await api(`/api/runs/${state.run.id}/upload`,'POST',payload,false,{'X-Idempotency-Key':`${state.run.id}-local-upload`});state.run.state='uploaded';state.run.virtualElapsedSeconds=state.plan.durationSeconds;state.run.remainingRealSeconds=0;state.lastElapsed=state.plan.durationSeconds;clearInterval(state.poll);renderResult();await loadHome();metrics();$('runState').textContent='本地已保存';notice('完整流程已通过：本地鉴权、主页获取、计时、轨迹校验与保存。未提交到外部服务。');}catch(e){notice(e.message+(e.details?.errors?'：'+e.details.errors.join('；'):e.details?.region?`：${e.details.region.insidePoints}/${e.details.region.totalPoints}点在区域内`:''),true);}finally{state.uploading=false;controls();}}
async function cancel(){if(!state.run)return;try{await api(`/api/runs/${state.run.id}/cancel`,'POST',{});clearInterval(state.poll);state.run.state='cancelled';$('runState').textContent='已取消';$('progressText').textContent='此次测试已取消，没有生成已上传记录。';await loadHome();controls();notice('本地测试已取消。');}catch(e){notice(e.message,true);}}
function renderResult(){const r=state.result;if(!r)return;const box=$('resultBox');box.className='success-box';box.replaceChildren();const title=document.createElement('strong');title.textContent=r.productionSubmitted?(r.serverRecord?.status==='effective'?'远端已确认有效':r.serverRecord?.status==='ineffective'?'远端已接收 · 服务器判定无效':'远端已接收 · 状态待确认'):r.generationOnly?'模拟轨迹已保存 · 未计时':'本地校验通过 · 模拟记录已保存';const desc=document.createElement('div');desc.className='result-metrics';for(const text of [`${(r.distanceMeters/1000).toFixed(3)} km`,`${fmt(r.durationSeconds)} 模拟时长`,`${r.pointCount} 个轨迹点`,`${r.clockScale}×测试时钟`,`区域内 ${(r.region.ratio*100).toFixed(1)}%（${r.region.insidePoints}/${r.region.totalPoints}）`]){const span=document.createElement('span');span.textContent=text;desc.append(span);}const note=document.createElement('p');note.className='small muted';note.textContent=r.serverRecord?serverRecordText(r.serverRecord):r.message;const chips=document.createElement('div');r.checks.forEach(c=>{const span=document.createElement('span');span.className='check-chip';span.textContent='✓ '+({'local-auth':'本地鉴权','elapsed-time':'计时完成','simulated-flag':'模拟数据标识','finite-coordinates':'坐标有效','optional-time-order':'采样时间顺序','region-at-least-70-percent':'区域内点数≥70%','timing-skipped':'未运行计时'}[c]||c);chips.append(span);});box.append(title,desc,note,chips);}
function updateHistoryControls(busy=state.uploading||state.generating||state.starting){
 for(const item of state.historyButtons){
  const {button,kind,record}=item;
  if(kind==='delete'){
   button.disabled=busy||record.canDelete===false;
   button.textContent=state.recordBusy?.id===record.id&&state.recordBusy.action==='delete'?'删除中…':'删除';
  }else{
   const submitted=record.result?.productionSubmitted||record.remoteUpload?.state==='accepted';
   const uncertain=['unknown','sending'].includes(record.remoteUpload?.state);
   button.disabled=busy||!state.home?.remote||!record.canUpload;
   button.textContent=state.recordBusy?.id===record.id&&state.recordBusy.action==='upload'?'上传中…':submitted?'已上传':uncertain?'待核对':'上传';
   button.title=!state.home?.remote?'请先连接真实服务器':record.uploadBlockedReason||'上传这一条记录，时间戳按上传时刻回推';
  }
 }
}
function renderHistory(){
 const target=$('history');target.replaceChildren();state.historyButtons=[];
 const rows=state.home?.history||[];
 if(!rows.length){target.textContent='尚无本地记录';return;}
 for(const r of rows){
  const row=document.createElement('div');row.className='history-row';
  const left=document.createElement('div');left.className='history-copy';
  const title=document.createElement('div');title.textContent=`${r.laps}圈 · ${(r.distanceMeters/1000).toFixed(3)} km · ${r.pointCount}点`;
  const status=document.createElement('small');status.className='record-state';
  status.textContent=r.result?.productionSubmitted?serverRecordText(r.result.serverRecord):r.remoteUpload?.state==='unknown'?'上传结果待核对':labels[r.state]||r.state;
  const time=document.createElement('small');time.textContent=new Date(r.createdAt).toLocaleString();
  left.append(title,status,time);
  const actions=document.createElement('div');actions.className='history-actions';
  const uploadButton=document.createElement('button');uploadButton.className='secondary';
  uploadButton.setAttribute('aria-label',`上传 ${r.laps}圈 ${new Date(r.createdAt).toLocaleString()} 的记录`);
  uploadButton.addEventListener('click',()=>uploadHistoryRecord(r));
  const deleteButton=document.createElement('button');deleteButton.className='danger';
  deleteButton.title='仅删除本地记录，可撤销；远端记录不受影响';
  deleteButton.setAttribute('aria-label',`删除 ${r.laps}圈 ${new Date(r.createdAt).toLocaleString()} 的本地记录`);
  deleteButton.addEventListener('click',()=>deleteHistoryRecord(r));
  state.historyButtons.push({button:uploadButton,kind:'upload',record:r},{button:deleteButton,kind:'delete',record:r});
  actions.append(uploadButton,deleteButton);row.append(left,actions);target.append(row);
 }
 updateHistoryControls();
}
async function saveGenerated(){
 const plan=state.plan;
 if(!state.generationOnly||!plan||state.uploading||state.generating||state.savedGenerationId===plan.generationId)return;
 state.uploading=true;controls();
 try{
  const data=await api('/api/trajectories/save','POST',{config:plan.config,generationId:plan.generationId});
  state.savedGenerationId=plan.generationId;state.generatedRecordId=data.run.id;
  await loadHome();notice('完整轨迹已保存到本地记录，可在列表中单独上传或删除。');
 }catch(e){notice(e.message,true);}finally{state.uploading=false;controls();}
}
async function deleteHistoryRecord(record){
 if(state.uploading||state.generating||state.starting)return;
 state.uploading=true;state.recordBusy={id:record.id,action:'delete'};controls();
 try{
  await api(`/api/runs/${record.id}/delete`,'POST',{});
  state.lastDeleted={id:record.id,laps:record.laps};
  if(state.run?.id===record.id||state.generatedRecordId===record.id){
   clearInterval(state.poll);state.run=null;settingsChanged();
  }
  $('deletedRecordText').textContent=`${record.laps}圈记录已从本地删除。`;$('deletedRecordNotice').classList.remove('hidden');
  await loadHome();notice('本地记录已删除，可撤销；远端记录未更改。');
 }catch(e){notice(e.message,true);}finally{state.uploading=false;state.recordBusy=null;controls();}
}
async function undoDelete(){
 if(!state.lastDeleted||state.uploading||state.generating||state.starting)return;
 const record=state.lastDeleted;state.uploading=true;controls();
 try{
  await api(`/api/runs/${record.id}/restore`,'POST',{});state.lastDeleted=null;
  $('deletedRecordNotice').classList.add('hidden');await loadHome();notice('本地记录已恢复。');
 }catch(e){notice(e.message,true);}finally{state.uploading=false;controls();}
}
async function uploadHistoryRecord(record){
 if(state.uploading||state.generating||state.starting||!record.canUpload||!state.home?.remote)return;
 state.uploading=true;state.recordBusy={id:record.id,action:'upload'};controls();let receipt;
 try{
  receipt=await api(`/api/runs/${record.id}/remote-upload`,'POST',{});state.draft=receipt;
  if(state.generatedRecordId===record.id&&state.plan){state.plan.productionSubmitted=true;state.plan.remoteRecordId=receipt.remoteRecordId;state.plan.serverRecord=receipt.summary?.serverRecord;}
  await loadHome();
  if(state.run?.id===record.id){state.run=await api(`/api/runs/${record.id}`);state.result=state.run.result;renderResult();}
  notice(`所选记录已上传，记录 ID：${receipt.remoteRecordId}；${serverRecordText(receipt.summary?.serverRecord)}。`);
 }catch(e){
  notice(receipt?`远端已接收该记录，本地刷新失败：${e.message}`:e.message,true);
  if(!receipt)try{
   await loadHome();const selected=state.home.history.find(r=>r.id===record.id);
   if(selected?.remoteUpload){state.draft=selected.remoteUpload;renderDraft();}
  }catch{}
 }finally{state.uploading=false;state.recordBusy=null;controls();}
}
async function restoreActive(){const r=state.home?.history.find(r=>r.state==='running');if(!r)return;++state.requestSerial;state.generationOnly=false;state.run=await api(`/api/runs/${r.id}`);state.plan=await api(`/api/runs/${r.id}/plan`);for(const [id,key] of [['laps','laps'],['clock','clockScale'],['sigma','sigmaMeters'],['cap','maxOffsetMeters'],['correlation','correlationSeconds'],['seed','seed']])$(id).value=String(state.plan.config[key]);updateEstimate();state.lastElapsed=state.run.virtualElapsedSeconds;state.lastPoll=performance.now();metrics();controls();beginPolling();}
function elapsed(){if(!state.run)return 0;if(state.run.state==='uploaded')return state.plan.durationSeconds;if(state.run.state==='running')return Math.min(state.plan.durationSeconds,state.lastElapsed+(performance.now()-state.lastPoll)/1000*state.plan.config.clockScale);return state.run.virtualElapsedSeconds||0;}
function draw(){const canvas=$('track'),ctx=canvas.getContext('2d'),rect=canvas.getBoundingClientRect(),dpr=devicePixelRatio||1;if(canvas.width!==Math.round(rect.width*dpr)||canvas.height!==Math.round(rect.height*dpr)){canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);}ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,rect.width,rect.height);if(!state.home)return;const ref=state.home.reference.points,a=ref[0],R=6371008.8,rad=Math.PI/180;const xy=p=>[(p.longitude-a.longitude)*rad*R*Math.cos(a.latitude*rad),(p.latitude-a.latitude)*rad*R];const base=ref.map(xy);const obs=(state.plan?.points||[]).map(xy);const polygons=(state.home.region?.polygons||[]).map(poly=>poly.map(xy));const all=$('fullRegion').checked?base.concat(obs,...polygons):base.concat(obs);const xs=all.map(p=>p[0]),ys=all.map(p=>p[1]);const lo=[Math.min(...xs)-12,Math.min(...ys)-12],hi=[Math.max(...xs)+12,Math.max(...ys)+12];const scale=Math.min((rect.width-52)/(hi[0]-lo[0]),(rect.height-38)/(hi[1]-lo[1]));const ox=(rect.width-(hi[0]-lo[0])*scale)/2,oy=(rect.height-(hi[1]-lo[1])*scale)/2;const project=p=>[ox+(p[0]-lo[0])*scale,rect.height-oy-(p[1]-lo[1])*scale];ctx.strokeStyle='#e7eeea';ctx.lineWidth=1;ctx.font='10px system-ui';ctx.fillStyle='#96a59d';const grid=(hi[0]-lo[0])>500?200:25;for(let y=Math.ceil(lo[1]/grid)*grid;y<=hi[1];y+=grid){const p=project([lo[0],y]),q=project([hi[0],y]);ctx.beginPath();ctx.moveTo(...p);ctx.lineTo(...q);ctx.stroke();ctx.fillText(y+'m',p[0]-30,p[1]+3);}for(let x=Math.ceil(lo[0]/grid)*grid;x<=hi[0];x+=grid){ctx.beginPath();ctx.moveTo(...project([x,lo[1]]));ctx.lineTo(...project([x,hi[1]]));ctx.stroke();}function path(points,color,width){if(!points.length)return;ctx.beginPath();points.forEach((p,i)=>{const q=project(p);i?ctx.lineTo(...q):ctx.moveTo(...q);});ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();}for(const poly of polygons){ctx.beginPath();poly.forEach((p,i)=>{const q=project(p);i?ctx.lineTo(...q):ctx.moveTo(...q);});ctx.closePath();ctx.fillStyle='#4eaa7b16';ctx.fill();ctx.strokeStyle='#67a083';ctx.lineWidth=1.1;ctx.stroke();}path(base,'#2874a4',3);let count=obs.length;if(state.run&&['running','ready'].includes(state.run.state)){count=Math.min(obs.length,Math.floor(elapsed()/state.plan.sampleIntervalSeconds)+1);}path(obs.slice(0,count),'#c68a32',1.4);const anchor=project([0,0]);ctx.fillStyle='#195e4e';ctx.beginPath();ctx.arc(...anchor,5,0,Math.PI*2);ctx.fill();ctx.fillText('起点',anchor[0]+9,anchor[1]+3);if(state.run&&count&&obs.length){const current=project(obs[Math.max(0,count-1)]);ctx.beginPath();ctx.arc(...current,5,0,Math.PI*2);ctx.fillStyle='#c68a32';ctx.fill();}}
function animate(){if(state.run&&state.plan){const t=elapsed();$('elapsed').textContent=fmt(t);$('progress').value=t/state.plan.durationSeconds;$('progressText').textContent=`${fmt(t)} / ${fmt(state.plan.durationSeconds)} · ${state.plan.config.clockScale}×${state.plan.config.clockScale>1?'加速仿真':'真实计时'} · ${$('target').value==='remote'?'计时结束后准备远端请求':'数据保存到本机'}`;}draw();requestAnimationFrame(animate);}
$('login').addEventListener('click',()=>{let headers;try{headers=JSON.parse($('authJson').value);}catch{notice('请输入有效的验证 JSON。',true);return;}$('authJson').value='';login(false,headers);});
$('authFile').addEventListener('change',async e=>{const file=e.target.files[0];e.target.value='';if(!file)return;if(file.size>32768){notice('验证文件过大。',true);return;}try{await login(false,JSON.parse(await file.text()));}catch{notice('无法读取验证 JSON 文件。',true);}});
$('demo').addEventListener('click',()=>login(true));$('preview').addEventListener('click',generateDirect);$('start').addEventListener('click',start);$('cancel').addEventListener('click',cancel);$('upload').addEventListener('click',upload);$('refresh').addEventListener('click',()=>loadHome().catch(e=>notice(e.message,true)));
$('logout').addEventListener('click',async()=>{rememberSavedSession(false);try{await api('/api/auth/logout','POST',{});location.reload();}catch(e){if(e.code==='LOCAL_SESSION_REQUIRED')location.reload();else notice(e.message,true);}});
$('export').addEventListener('click',async()=>{
 const generated=state.generationOnly,plan=state.plan,run=state.run;
 if(!(generated&&plan)&&!state.result)return;
 try{
  const data=generated?plan:await api(`/api/runs/${run.id}/export`);
  const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');
  a.href=url;a.download=generated?`generated-trajectory-${plan.config.laps}laps-${plan.config.seed}.json`:`local-simulated-run-${run.id}.json`;
  a.click();URL.revokeObjectURL(url);
 }catch(e){notice(e.message,true);}
});
$('laps').addEventListener('input',settingsChanged);
$('decreaseLaps').addEventListener('click',()=>changeLaps(-1));$('increaseLaps').addEventListener('click',()=>changeLaps(1));
for(const id of ['clock','sigma','cap','correlation','seed'])$(id).addEventListener('change',settingsChanged);
(async()=>{try{const health=await api('/api/health');setTrajectoryLimits(health.trajectory);$('verifierState').textContent=health.remoteCredentialFileConfigured?'已配置本机凭据文件':'可粘贴或导入凭据，也可演示登录';$('savedLogin').disabled=!health.remoteCredentialFileConfigured;notice('本地后端已连接。先登录，再开始验证。');try{await loadHome(true);await preview();await restoreActive();notice(active()?'本地会话与进行中的计时已恢复。':'本地会话已恢复，可以开始新的测试。');}catch(e){if(e.code!=='LOCAL_SESSION_REQUIRED')throw e;}}catch(e){notice(e.message,true);}requestAnimationFrame(animate);})();

function serverRecordText(record){
 if(record?.status==='effective')return record.scoringType===2?`远端有效 · ${record.effectivePart}段`:`远端有效 · ${Number(record.effectiveMileageKm).toFixed(2)} km`;
 if(record?.status==='ineffective')return `远端无效：${record.uneffectiveReason||'有效里程或段数为0'}`;
 return '远端已接收 · 有效性待确认';
}
function renderRemote(){
 const r=state.home.remote,l=r.runningLimit,t=r.totals;
 const clock=ms=>`${String(Math.floor(Number(ms)/3600000)).padStart(2,'0')}:${String(Math.floor(Number(ms)/60000)%60).padStart(2,'0')}`;
 const hours=(l.runningTimeLimit?.openTimes||[]).map(w=>`${clock(w.dayStartTime)}–${clock(w.dayEndTime)}`).join(' / ');
 $('remoteTotal').textContent=`${Number(t.totalMileage||0).toFixed(2)} km`;$('remoteParts').textContent=String(t.totalPart??'—');
 $('remoteSemester').textContent=`${r.semester.yearRange} · 第${r.semester.index}学期`;
 $('remoteRegions').textContent=`${(r.runningRange?.rangeRunningRegion||[]).length} 个区域`;
 $('remoteRuleText').textContent=l.hasRule?`远端要求：单次 ${l.effectiveMileageStart}–${l.effectiveMileageEnd} km；配速 ${Number(l.averageSpeedStart)/60000}–${Number(l.averageSpeedEnd)/60000} 分/km；每日上限 ${l.dailyMileage} km；开放时段 ${hours||'以官方规则为准'}。有效性以服务器回查结果为准。`:'远端当前未返回跑步规则。';
 $('remoteFetched').textContent=`读取时间：${new Date(r.fetchedAt).toLocaleString()} · cpes.legym.cn`;
 $('remoteRaw').textContent=JSON.stringify({runningLimit:r.runningLimit,runningRange:r.runningRange},null,2);
}
const remoteLabels={draft:'待提交',sending:'提交中',accepted:'远端已接收',rejected:'远端拒绝',unknown:'结果待核对',superseded:'已由新请求替换'};
function renderDraft(){
 const d=state.draft,box=$('remoteDraft');box.replaceChildren();if(!d){box.textContent='尚未准备真实上传请求。';controls();return;}
 const title=document.createElement('strong');title.textContent=remoteLabels[d.state]||d.state;box.append(title);
 const s=d.summary,p=document.createElement('p');p.textContent=`${s.startTime} → ${s.endTime} · ${s.distanceKm} km · ${s.pointCount}点 · 有效里程 ${s.effectiveMileage} km`;box.append(p);
 const note=document.createElement('p');note.className='small muted';note.textContent=d.remoteRecordId?`记录 ID：${d.remoteRecordId} · ${serverRecordText(d.summary?.serverRecord)}`:d.error?.message||s.uneffectiveReason||s.message;box.append(note);controls();
}
async function loadRemoteHistory(){
 const rows=await api('/api/remote/uploads','GET',undefined,true);const box=$('remoteHistory');box.replaceChildren();
 state.draft=rows.find(row=>row.id===state.draft?.id)||rows[0]||null;
 for(const row of rows){const button=document.createElement('button');button.className='text-button';button.textContent=`${remoteLabels[row.state]||row.state} · ${row.summary.startTime} · ${row.summary.distanceKm} km`;button.addEventListener('click',()=>{state.draft=row;renderDraft();});box.append(button);}
 renderDraft();
}
async function prepareRemoteRun(){
 state.uploading=true;controls();try{
  state.draft=await api(`/api/runs/${state.run.id}/remote-prepare`,'POST',{});clearInterval(state.poll);
  state.run=await api(`/api/runs/${state.run.id}`);state.result=state.run.result;renderResult();renderDraft();
  await loadHome();$('runState').textContent='请求已准备';$('remoteUploadPanel').scrollIntoView({behavior:'smooth',block:'start'});notice('签名请求已准备。尚未写入真实服务；请核对下方请求后点击提交。');
 }catch(e){$('automatic').checked=false;notice(e.message,true);}finally{state.uploading=false;controls();}
}
$('saveGenerated').addEventListener('click',saveGenerated);$('undoDelete').addEventListener('click',undoDelete);
$('savedLogin').addEventListener('click',()=>login(false,{},true));
$('target').addEventListener('change',()=>{const remote=$('target').value==='remote';if(remote)$('clock').value='1';$('clock').disabled=remote;$('upload').textContent=remote?'准备远端请求':'提交本地';settingsChanged();if(state.home)preview();});
$('refreshRemote').addEventListener('click',async()=>{try{await api('/api/remote/home');await loadHome();notice('真实主页、规则和区域已更新。');}catch(e){notice(e.message,true);}});
$('signedFile').addEventListener('change',async e=>{
 const file=e.target.files[0];e.target.value='';if(!file)return;if(file.size>1800000){notice('签名请求文件过大。',true);return;}
 try{state.draft=await api('/api/remote/uploads/prepare','POST',JSON.parse(await file.text()));await loadRemoteHistory();renderDraft();notice('完整签名请求已准备，尚未向远端提交。');}catch(e){notice(e.message,true);}
});
$('commitRemote').addEventListener('click',async()=>{
 if(!state.draft||state.draft.state!=='draft'||state.uploading)return;state.uploading=true;controls();
 try{
  state.draft=await api(`/api/remote/uploads/${state.draft.id}/commit`,'POST',{});renderDraft();
  notice(`服务器已返回记录 ID：${state.draft.remoteRecordId}；${serverRecordText(state.draft.summary?.serverRecord)}。`);
  try{await api('/api/remote/home');await loadHome();if(state.run){state.run=await api(`/api/runs/${state.run.id}`);state.result=state.run.result;renderResult();}}catch(e){notice(`远端已接收，刷新统计失败：${e.message}`,true);}
 }catch(e){notice(e.message,true);try{const rows=await api('/api/remote/uploads');state.draft=rows.find(d=>d.id===state.draft.id)||state.draft;renderDraft();}catch{}}
 finally{state.uploading=false;controls();}
});

$('verifyRemote').addEventListener('click',async()=>{
 if(state.draft?.state!=='accepted'||state.uploading)return;
 const id=state.draft.id;state.uploading=true;controls();
 try{state.draft=await api(`/api/remote/uploads/${id}/verify`,'POST',{});await loadHome();renderDraft();notice(serverRecordText(state.draft.summary.serverRecord));}
 catch(e){notice(e.message,true);}finally{state.uploading=false;controls();}
});
