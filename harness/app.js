const $=id=>document.getElementById(id);
const labels=['抓取扫码枪','调整姿态','抓瓶并朝向标签','对准并读码','固定码值映射','放置瓶子','归位与复位'];
const statusLabels={idle:'就绪',starting:'准备执行',running:'执行中',paused:'已暂停',stopped:'已停止',completed:'流程完成',test_completed:'单步测试结束',test_failed:'单步测试异常'};
let state={};let submitting=false;let refreshing=false;
async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const value=await r.json();if(!r.ok)throw Error(value.error||'请求失败');return value;}
async function request(path,data={}){if(submitting)return;submitting=true;$('error').textContent='';try{const d=await api(path,data);if(path==='/api/reference')$('error').textContent='初始位姿已记录';await refresh();return d;}catch(e){$('error').textContent=e.message;}finally{submitting=false;}}
function action(path,data={}){return request('/api/session/'+path,{session_id:state.session_id,step:state.step,...data});}
function testData(){return {step:Number($('testStep').value),target:$('testTarget').value,code:$('code').value};}
$('step3Left').onclick=()=>request('/api/test/step3-left');
$('testStart').onclick=()=>request('/api/test/start',testData());
$('checkOnly').onclick=()=>request('/api/vision/check',testData());
$('reference').onclick=()=>request('/api/reference');
$('start').onclick=()=>action('start');$('observe').onclick=()=>action('complete');
$('stop').onclick=()=>api('/api/session/stop',{session_id:state.session_id}).then(refresh).catch(e=>{$('error').textContent=e.message;});$('retry').onclick=()=>action('retry');
$('scan').onclick=()=>action('scan',{code:$('code').value});
$('code').onkeydown=e=>{if(e.key==='Enter'&&!$('scan').disabled)$('scan').click();};
$('testStep').onchange=()=>{const step=Number($('testStep').value);$('testTarget').style.display=step===6?'':'none';updateControls();};
function updateControls(){const active=['running','starting','paused'].includes(state.status)||state.vision_busy;
 for(const id of ['start','testStart','step3Left','reference'])$(id).disabled=active;
 $('checkOnly').disabled=active||[4,5].includes(Number($('testStep').value));
 $('observe').disabled=state.status!=='running'||state.vision_busy||state.step===4;
 $('retry').disabled=state.single_step||!(state.status==='paused'||state.phase==='awaiting_check')||state.vision_busy;
 $('stop').disabled=!['running','starting','paused','test_failed'].includes(state.status);
 $('scan').disabled=state.status!=='running'||state.step!==4;}
async function refresh(){if(refreshing)return;refreshing=true;try{
 state=await api('/api/state');
 $('status').textContent=`${statusLabels[state.status]||state.status} · ${state.test_profile==='step3_left'?'锁右手 · 左手抓瓶':state.check_only?'画面检测':state.single_step?'单步测试':'完整流程'} · Step ${state.step} ${labels[state.step-1]||''}`;
 const observedHands=state.judgment?.held_objects||state.held_objects;
 $('detail').textContent=`固定手臂 ${state.hold_arm||'无'} · 抓枪夹爪锁定 ${state.scanner_gripper_lock?.arm||'无'} · 扫码枪 ${observedHands?.scanner||'未持有'} · 瓶子 ${observedHands?.bottle||'未持有'} · ${state.phase||'idle'} · VLM调用 ${state.vision_calls} 次${state.vision_busy?' · 请求中':''}${state.last_error?' · '+state.last_error:''}`;
 $('steps').replaceChildren(...labels.map((text,i)=>{const d=document.createElement('div');d.className='step'+(state.step===i+1?' active':!state.single_step&&state.step>i+1?' done':'');d.textContent=`${i+1}. ${text}`;return d;}));
 updateControls();
 $('target').textContent=`本轮码值：${state.scan?.code||'未收到'} · 目标：${state.target||'待确定'}`;
 $('judgment').textContent=state.raw_judgment||(state.judgment?JSON.stringify(state.judgment,null,2):'尚未调用VLM');
 $('vlaInstruction').textContent=state.vla_instruction||'尚未下发';
 $('preflight').textContent=state.preflight?JSON.stringify(state.preflight,null,2):'尚未预检';
 $('responseInfo').textContent=state.vision_response?`${state.vision_response.model||''} · ${state.vision_response.response_id||''}`:'';
 $('events').textContent=state.events.slice(-15).map(e=>`${new Date(e.ts*1000).toLocaleTimeString()} ${JSON.stringify(e)}`).join('\n');
 }finally{refreshing=false;}}
// Each camera refreshes independently. This endpoint never invokes a model or starts motion.
for(const [name,label] of [['head','头部'],['left','左腕'],['right','右腕'],['bev1','机身 BEV-1'],['bev2','机身 BEV-2']]){
 const box=document.createElement('div');box.className='panel';const title=document.createElement('h2');title.textContent=label;
 const img=document.createElement('img');img.alt=label+'实时画面';const note=document.createElement('p');note.className='note';note.textContent='连接中…';
 box.append(title,img,note);$('liveCameras').append(box);
 const next=()=>{img.src='/api/camera/'+name+'?t='+Date.now();};
 img.onload=()=>{img.style.display='block';note.textContent='更新于 '+new Date().toLocaleTimeString();setTimeout(next,name==='head'?180:350);};
 img.onerror=()=>{img.style.display='none';note.textContent='画面不可用，正在重连';setTimeout(next,1800);};next();
}
$('testStep').onchange();
(async()=>{try{const c=await api('/api/config');$('mode').textContent='LIVE · '+c.vision_model;$('intro').textContent='真实机器人与真实API。选择步骤后点击执行；执行期间每隔2.5秒自动检测。单步测试成功后停止，保留VLM返回JSON。';await refresh();}catch(e){$('error').textContent=e.message;}})();
setInterval(()=>refresh().catch(e=>{$('error').textContent=e.message;}),1000);
