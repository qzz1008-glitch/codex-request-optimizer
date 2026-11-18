const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const app=fs.readFileSync('packet_inspector/ui/app.js','utf8');
const connections=app.slice(0,app.indexOf('\n// Saved configuration'));
assert(connections.includes('globalThis.InspectorConnections ='),'Connection controls load through the existing app.js route');
const section=(start,end)=>app.slice(app.indexOf(start),app.indexOf(end,app.indexOf(start)));
const localControls=[
  app.match(/^const \$ = .*$/m)[0],
  `const state={status:null}, restartNotices={connection(){}};
   let settingsOpening=false, settingsEditing=false, settingsSaving=false, compressionSaving=false,
       exitSaving=false, connectSaving=false, connectChecking=false, linkSaving='', compressionTarget=null,
       connectSource=null, exitFinished=false, startupConnectChecked=true, desktopOpenRevision=0,
       compressionRevision=0, linkRevision=0;
   function setText(node,value) {node.textContent=value;}
   function fail(error) {throw error;}
   function updateSettingsSaveState() {}
   function renderConnectionActions() {renderCompressionMode();}
   function renderStatus() {renderCompressionMode();renderConnectPrompt();}
   async function refreshStatus() {state.status=await api('/api/status');renderStatus();}`,
  app.match(/^function action\(id, fn\).*$/m)[0],
  section('function renderCompressionMode()','function renderExitRequest()'),
  section('async function applyLinkAction(which)','$("session-search").addEventListener'),
  ...app.split(/\r?\n/).filter(line=>/^action\("(compression-|connect-confirm-)/.test(line))
].join('\n');

(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    for(const fallback of [false,true]) {
      const page=await browser.newPage({viewport:{width:1200,height:800}});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      const html=fs.readFileSync('packet_inspector/ui/index.html','utf8').replace(/<script[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link[^>]*>/g,'');
      assert(!fs.readFileSync('packet_inspector/ui/index.html','utf8').includes('/connections.js'),'No new asset route is required for live reload');
      await page.setContent(html);
      await page.addStyleTag({path:'packet_inspector/ui/style.css'});
      await page.addScriptTag({path:'packet_inspector/ui/i18n.js'});
      if(fallback)await page.evaluate(()=>{delete HTMLElement.prototype.popover;});
      await page.addScriptTag({content:connections});
      await page.addScriptTag({content:localControls});
      await page.evaluate(()=>{
        window.fixture={rows:[{id:'local',name:'本机',kind:'local',state:'disconnected'},
          {id:'remote-a',name:'Server 176',kind:'ssh',state:'disconnected',codex_restart:{supported:true}}],
          calls:[],localCalls:[],local:{profile:{active:false,can_restore:false}},failRemote:false,failReconnect:false};
        window.api=async(path,data)=>{
            if(path==='/api/status')return structuredClone(fixture.local);
            if(path==='/api/link') {
              fixture.localCalls.push(data.action);
              const active=data.action==='activate';
              fixture.local.profile={active,can_restore:active};
              fixture.rows[0].state=active?'connected':'disconnected';
              return {active,reload_required:true};
            }
            if(path!=='/api/connections')throw Error('Unexpected fixture path '+path);
            if(fixture.unavailable)throw Object.assign(Error('ui_route_not_found'),{status:404});
            if(fixture.networkError)throw Object.assign(Error('forwarding_service_unavailable'),{status:502});
            if(!data)return {targets:structuredClone(fixture.rows),error:'',revision:'fixture-revision'};
            fixture.calls.push(structuredClone(data));
            const accepted={};
            for(const id of data.targets){
              const row=fixture.rows.find(row=>row.id===id),operation=String(Math.random());accepted[id]=operation;
              Object.assign(row,{busy:true,action:data.action,operation_id:operation,error:''});
              if(data.action==='reconnect')delete row.codex_reconnect;
              setTimeout(()=>{
                if(data.action==='reconnect'){
                  Object.assign(row,{busy:false,codex_reconnect:fixture.failReconnect?
                    {state:'unconfirmed',error:'Native restart timed out; reminder retained'}:{state:'reloaded',error:''}});
                  if(!fixture.failReconnect)row.reload_required=false;
                  return;
                }
                const fail=id==='remote-a'&&fixture.failRemote;
                Object.assign(row,{busy:false,state:fail?'error':data.action==='activate'?'connected':'disconnected',error:fail?'Synthetic SSH failure':'',reload_required:!fail});
              },40);
            }
            return {accepted};
        };
        window.InspectorConnectionControls=InspectorConnections.create({request:api});
        return refreshStatus();
      });
      await page.waitForFunction(()=>document.querySelectorAll('.connection-device').length===1);
      const controls=page.locator('#connection-controls'),panel=page.locator('#connection-panel');
      const reopenPanel=async()=>{
        await page.mouse.move(0,page.viewportSize().height-1);
        await page.keyboard.press('Escape');await panel.waitFor({state:'hidden'});
        await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      };
      assert.equal(await page.locator('#connection-devices-open').count(),0,'The extra dropdown triangle must be removed');
      await controls.focus();await page.keyboard.press('Escape');
      for(const id of ['compression-disconnect','compression-connect']){
        await page.mouse.move(0,790);
        await page.locator('#'+id).hover();await panel.waitFor({state:'visible'});
        await page.keyboard.press('Escape');await panel.waitFor({state:'hidden'});
      }
      await page.mouse.move(0,790);
      await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      assert.doesNotMatch(await panel.textContent(),/本机/);
      const bounds=await panel.boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=1201&&bounds.y>=0&&bounds.y+bounds.height<=801);
      assert.equal(await page.evaluate(()=>document.activeElement.id),'connection-controls','Hover must not move keyboard focus');
      await page.locator('.connection-device').last().getByRole('button',{name:'接入',exact:true}).click();
      await page.locator('#devices-confirm-dialog').waitFor({state:'visible'});
      assert.equal(await page.locator('#devices-confirm-targets li').count(),1);
      assert.equal(await page.evaluate(()=>fixture.calls.length),0,'No changes before confirmation');
      assert.match(await page.locator('#devices-confirm-description').textContent(),/先验证连接，再备份并更新此服务器/);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls[0]),{targets:['remote-a'],action:'activate',revision:'fixture-revision'});
      assert.equal(await page.evaluate(()=>fixture.local.profile.active),false,'A remote action leaves local state unchanged');
      await page.locator('#devices-confirm-cancel').click();
      assert.equal(await page.locator('#compression-disconnect').getAttribute('aria-pressed'),'true','A connected server does not affect the local pill');
      await page.evaluate(()=>{fixture.rows[1].busy=true;});
      await reopenPanel();
      await page.locator('.connection-device[aria-busy="true"]').waitFor({state:'visible'});
      await page.locator('#compression-connect').click();
      await page.locator('#connect-confirm-dialog').waitFor({state:'visible'});
      assert.match(await page.locator('#connect-confirm-description').textContent(),/本机.*WSL/);
      assert.equal(await page.evaluate(()=>fixture.localCalls.length),0,'The local pill still requires confirmation');
      await page.locator('#connect-confirm-apply').click();
      await page.locator('#connect-confirm-dialog').waitFor({state:'hidden'});
      assert.equal(await page.locator('#compression-connect').getAttribute('aria-pressed'),'true');
      // Borrowed quota uses separate recovery data; the local endpoint is still connected.
      await page.evaluate(async()=>{fixture.local.profile.can_restore=false;await refreshStatus();});
      await page.locator('#compression-disconnect').click();
      await page.locator('#compression-confirm-dialog').waitFor({state:'visible'});
      assert.match(await page.locator('#compression-confirm-description').textContent(),/本机.*WSL/);
      await page.locator('#compression-confirm-apply').click();
      await page.locator('#compression-confirm-dialog').waitFor({state:'hidden'});
      assert.deepEqual(await page.evaluate(()=>fixture.localCalls),['activate','restore']);
      assert.equal(await page.evaluate(()=>fixture.calls.length),1,'Neither local button calls the SSH device API');
      assert.equal(await page.evaluate(()=>fixture.rows[1].state),'connected');
      assert.equal(await page.locator('#compression-disconnect').getAttribute('aria-pressed'),'true');
      await page.evaluate(()=>{fixture.rows[1].busy=false;fixture.failRemote=true;});
      await reopenPanel();
      await page.locator('.connection-device').getByRole('button',{name:'断开',exact:true}).click();
      assert.match(await page.locator('#devices-confirm-description').textContent(),/中转服务保持运行/);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').textContent==='重试');
      assert.equal(await page.evaluate(()=>fixture.local.profile.active),false);
      assert.match(await page.locator('#devices-confirm-targets').textContent(),/Synthetic SSH failure/);
      await page.evaluate(()=>fixture.failRemote=false);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls.at(-1).targets),['remote-a'],'Retry only the failed scope');
      await page.locator('#devices-confirm-cancel').click();
      await page.setViewportSize({width:700,height:700});
      await controls.focus();await page.keyboard.press('ArrowDown');await panel.waitFor({state:'visible'});
      assert.equal(await page.evaluate(()=>document.getElementById('connection-panel').contains(document.activeElement)),true,'ArrowDown enters the device controls');
      const narrow=await panel.boundingBox();assert(narrow.x>=0&&narrow.x+narrow.width<=701&&narrow.y>=0&&narrow.y+narrow.height<=701);
      await page.keyboard.press('Escape');await panel.waitFor({state:'hidden'});
      await page.evaluate(()=>fixture.unavailable=true);
      await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      await page.waitForFunction(()=>document.getElementById('connection-devices-status').textContent.includes('请更新 EXE'));
      assert.equal(await page.locator('#request-compression').getAttribute('aria-busy'),'false','A missing backend feature must not appear to be loading');
      assert.equal(await page.locator('.connection-device').count(),0,'Unavailable device states must not remain actionable');
      assert.equal(await page.locator('#compression-connect').isDisabled(),false,'Unavailable SSH management leaves local controls available');
      await page.evaluate(()=>{fixture.unavailable=false;fixture.networkError=true;});
      await reopenPanel();
      await page.waitForFunction(()=>document.getElementById('connection-devices-status').textContent==='forwarding_service_unavailable');
      await page.evaluate(()=>fixture.networkError=false);
      await reopenPanel();
      await page.waitForFunction(()=>document.querySelectorAll('.connection-device').length===1);
      assert.equal(await page.locator('#connection-devices-status').isHidden(),true);
      const reconnect=page.getByRole('button',{name:'重连 Codex · Server 176',exact:true});
      assert.equal(await page.locator('.connection-device-reconnect:visible').count(),1,'Only SSH devices offer daemon reconnect');
      const beforeReconnect=await page.evaluate(()=>({calls:fixture.calls.length,states:fixture.rows.map(row=>row.state)}));
      await reconnect.click();
      await page.locator('#devices-confirm-dialog').waitFor({state:'visible'});
      assert.equal(await page.evaluate(()=>document.activeElement.id),'devices-confirm-cancel','The safe action receives initial focus');
      assert.match(await page.locator('#devices-confirm-description').textContent(),/当前账号.*正在执行或排队的任务可能中断/);
      assert.match(await page.locator('#devices-confirm-hint').textContent(),/中转服务保持运行/);
      assert.equal(await page.locator('#devices-confirm-targets li').count(),1);
      assert.equal(await page.evaluate(()=>fixture.calls.length),beforeReconnect.calls,'Opening a restart dialog does not execute it');
      await page.keyboard.press('Escape');
      await page.locator('#devices-confirm-dialog').waitFor({state:'hidden'});
      await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      await reconnect.click();await page.locator('#devices-confirm-apply').click();
      assert.equal(await page.locator('#devices-confirm-apply').isDisabled(),true);
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls.at(-1)),{targets:['remote-a'],action:'reconnect',revision:'fixture-revision'});
      assert.match(await page.locator('#devices-confirm-targets').textContent(),/后台已重载，等待 ChatGPT 恢复连接/);
      assert.deepEqual(await page.evaluate(()=>fixture.rows.map(row=>row.state)),beforeReconnect.states,'Daemon restart does not alter provider connection states');
      assert.equal(await page.evaluate(()=>fixture.local.profile.active),false);
      await page.locator('#devices-confirm-cancel').click();
      await page.evaluate(()=>{fixture.failReconnect=true;fixture.rows[1].reload_required=true;});
      await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      await reconnect.click();await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').textContent==='重试');
      assert.match(await page.locator('#devices-confirm-targets').textContent(),/Native restart timed out/);
      assert.equal(await page.evaluate(()=>fixture.rows[1].reload_required),true,'Unconfirmed restart preserves the reload reminder');
      const failedCalls=await page.evaluate(()=>fixture.calls.length);
      await page.evaluate(()=>fixture.failReconnect=false);
      assert.equal(await page.evaluate(()=>fixture.calls.length),failedCalls,'Retry requires another explicit click');
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls.at(-1).targets),['remote-a']);
      assert.equal(await page.evaluate(()=>fixture.rows[1].reload_required),false);
      await page.locator('#devices-confirm-cancel').click();
      await page.evaluate(()=>fixture.rows[1].codex_restart={supported:false,reason:'该服务器没有可重载的原生后台'});
      await page.locator('#compression-connect').hover();await panel.waitFor({state:'visible'});
      await reopenPanel();
      await page.waitForFunction(()=>document.querySelector('.connection-device-reconnect:not([hidden])').getAttribute('aria-disabled')==='true');
      assert.equal(await reconnect.evaluate(node=>node.disabled),false,'Unsupported action remains keyboard discoverable');
      await reconnect.focus();assert.equal(await reconnect.evaluate(node=>node===document.activeElement),true);
      assert.equal(await reconnect.getAttribute('aria-description'),'该服务器没有可重载的原生后台');
      const unsupportedCalls=await page.evaluate(()=>fixture.calls.length);
      await reconnect.click({force:true});assert.equal(await page.evaluate(()=>fixture.calls.length),unsupportedCalls);
      assert.equal(await page.locator('#devices-confirm-dialog').isVisible(),false);
      await page.evaluate(()=>{fixture.rows.pop();uiI18n.setLanguage('en',{persist:false});});
      await reopenPanel();
      await page.waitForFunction(()=>document.getElementById('connection-devices-status').textContent==='No saved servers');
      assert.equal(await panel.getAttribute('aria-label'),'Server connections');
      assert.equal(await page.locator('.connection-device').count(),0);
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('Remote connection UI: native/fallback hover, keyboard, scoped actions, daemon restart confirmation/failure, unsupported backend and recovery passed.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
