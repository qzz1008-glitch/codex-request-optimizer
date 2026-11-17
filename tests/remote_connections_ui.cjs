const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');

(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    for(const fallback of [false,true]) {
      const page=await browser.newPage({viewport:{width:1200,height:800}});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      const html=fs.readFileSync('packet_inspector/ui/index.html','utf8').replace(/<script[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link[^>]*>/g,'');
      await page.setContent(html);
      await page.addStyleTag({path:'packet_inspector/ui/style.css'});
      await page.addScriptTag({path:'packet_inspector/ui/i18n.js'});
      if(fallback)await page.evaluate(()=>{delete HTMLElement.prototype.popover;});
      await page.addScriptTag({path:'packet_inspector/ui/connections.js'});
      await page.evaluate(()=>{
        window.fixture={rows:[{id:'local',name:'本机',kind:'local',state:'disconnected'},
          {id:'remote-a',name:'Server 176',kind:'ssh',state:'disconnected'}],calls:[],local:{profile:{active:false}},failRemote:false};
        window.controller=InspectorConnections.create({
          status:()=>fixture.local,localBusy:()=>false,refreshLocal:async()=>{},
          request:async(path,data)=>{
            if(!data)return {targets:structuredClone(fixture.rows),error:'',revision:'fixture-revision'};
            fixture.calls.push(structuredClone(data));
            const accepted={};
            for(const id of data.targets){
              const row=fixture.rows.find(row=>row.id===id),operation=String(Math.random());accepted[id]=operation;
              Object.assign(row,{busy:true,action:data.action,operation_id:operation,error:''});
              setTimeout(()=>{
                const fail=id==='remote-a'&&fixture.failRemote;
                Object.assign(row,{busy:false,state:fail?'error':data.action==='activate'?'connected':'disconnected',error:fail?'Synthetic SSH failure':'',reload_required:!fail});
                if(id==='local')fixture.local.profile.active=row.state==='connected';
              },40);
            }
            return {accepted};
          }
        });
      });
      await page.waitForFunction(()=>document.querySelectorAll('.connection-device').length===2);
      const trigger=page.locator('#connection-devices-open'),panel=page.locator('#connection-panel');
      await trigger.focus();await page.keyboard.press('Escape');
      await page.mouse.move(0,790);
      await trigger.hover();await panel.waitFor({state:'visible'});
      assert.equal(await trigger.getAttribute('aria-expanded'),'true');
      const bounds=await panel.boundingBox();assert(bounds.x>=0&&bounds.x+bounds.width<=1201&&bounds.y>=0&&bounds.y+bounds.height<=801);
      assert.equal(await page.evaluate(()=>document.activeElement.id),'connection-devices-open','Hover must not move keyboard focus');
      await page.locator('.connection-device').last().getByRole('button',{name:'接入',exact:true}).click();
      await page.locator('#devices-confirm-dialog').waitFor({state:'visible'});
      assert.equal(await page.locator('#devices-confirm-targets li').count(),1);
      assert.equal(await page.evaluate(()=>fixture.calls.length),0,'No changes before confirmation');
      assert.match(await page.locator('#devices-confirm-description').textContent(),/各设备/);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls[0]),{targets:['remote-a'],action:'activate',revision:'fixture-revision'});
      assert.equal(await page.evaluate(()=>fixture.local.profile.active),false,'A remote action leaves local state unchanged');
      await page.locator('#devices-confirm-cancel').click();
      await page.locator('#compression-connect').click();
      assert.equal(await page.locator('#devices-confirm-targets li').count(),2);
      assert.match(await page.locator('#devices-confirm-hint').textContent(),/WSL/);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls[1].targets),['local','remote-a']);
      assert.equal(await page.locator('#compression-connect').getAttribute('aria-pressed'),'true');
      await page.locator('#devices-confirm-cancel').click();
      await page.evaluate(()=>fixture.failRemote=true);
      await page.locator('#compression-disconnect').click();
      assert.match(await page.locator('#devices-confirm-description').textContent(),/中转服务保持运行/);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').textContent==='重试失败设备');
      assert.equal(await page.evaluate(()=>fixture.local.profile.active),false);
      assert.match(await page.locator('#devices-confirm-targets').textContent(),/Synthetic SSH failure/);
      await page.evaluate(()=>fixture.failRemote=false);
      await page.locator('#devices-confirm-apply').click();
      await page.waitForFunction(()=>document.querySelector('#devices-confirm-apply').hidden);
      assert.deepEqual(await page.evaluate(()=>fixture.calls.at(-1).targets),['remote-a'],'Retry only the failed scope');
      await page.locator('#devices-confirm-cancel').click();
      await page.setViewportSize({width:700,height:700});
      await trigger.click();await panel.waitFor({state:'visible'});
      const narrow=await panel.boundingBox();assert(narrow.x>=0&&narrow.x+narrow.width<=701&&narrow.y>=0&&narrow.y+narrow.height<=701);
      await page.keyboard.press('Escape');await panel.waitFor({state:'hidden'});
      assert.equal(await trigger.getAttribute('aria-expanded'),'false');
      assert.deepEqual(errors,[]);
      await page.close();
    }
    console.log('Remote connection UI: native/fallback hover, keyboard, bounds, independent/bulk confirmation and partial failure retry passed.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
