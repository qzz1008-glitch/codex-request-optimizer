const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('packet_inspector/ui/app.js', 'utf8');
const html = fs.readFileSync('packet_inspector/ui/index.html', 'utf8');
const group = html.match(/id="request-compression"[\s\S]*?<\/div>/)[0];
assert.deepEqual([...group.matchAll(/data-compression="([^"]+)"/g)].map(x=>x[1]), ['disconnected','connected']);
assert(!html.includes('id="header-connect"'));

function fixture(connected) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {id, attrs:{}, dataset:{}, open:false, hidden:true, textContent:'',
      classList:{toggle(){}},
      setAttribute(key,value){this.attrs[key]=value;}, showModal(){this.open=true;}, close(){this.open=false;},
      replaceChildren(...values){this.children=values;}});
    return elements.get(id);
  }
  const buttons = ['disconnected','connected'].map(value=>Object.assign(element(value), {dataset:{compression:value}}));
  const calls = [];
  const context = {
    state:{status:{profile:{active:connected,can_restore:connected}}},
    compressionSaving:false,settingsSaving:false,settingsOpening:false,settingsEditing:false,
    exitSaving:false,connectSaving:false,connectChecking:false,linkSaving:'',compressionTarget:null,
    compressionRevision:0,connectSource:'header',connectionResumeSeen:'',connectionResumeRetrying:false,connectionResumeError:'',
    restartNotices:{connection:(which,result)=>calls.push(['result',which,result]),render(){}},
    $:element,t:value=>value,setText:(el,value)=>{el.textContent=value;},
    uiI18n:{text:(node,value)=>{node.textContent=typeof value==='function'?value():value;}},
    el:(tag,cls,text)=>({tag,text}),
    document:{querySelectorAll:()=>buttons,querySelector:()=>[...elements.values()].find(value=>value.open)||null},
    localStorage:{getItem(){return null;},setItem(){}},
    requestConnection:async source=>calls.push(['connect-prompt',source]),
    applyLinkAction:async action=>{calls.push(['link',action]);context.state.status.profile={active:false,can_restore:false};return {reload_required:true};},
    linkAction:async action=>{calls.push(['link',action]);context.state.status.profile={active:true,can_restore:true};return {reload_required:true};},
    renderStatus(){},renderConnectionActions(){context.renderCompressionMode();},renderConnectPrompt(){},
    message:value=>calls.push(['message',value]),
    api:async()=>{throw Error('Compression settings must not be used as a connection mode');}
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function renderCompressionMode()'), source.indexOf('function renderConnectPrompt()')), context);
  vm.runInContext(source.slice(source.indexOf('async function confirmConnection()'), source.indexOf('function renderExitRequest()')), context);
  return {context,element,buttons,calls};
}

(async()=>{
  let {context,element,buttons,calls}=fixture(false);
  context.renderCompressionMode();
  assert.equal(buttons[0].attrs['aria-pressed'],'true');
  assert.equal(buttons[1].disabled,false);
  await context.requestCompressionChange('connected');
  assert.deepEqual(calls,[['connect-prompt','header']]);
  await context.confirmConnection();
  assert.equal(context.state.status.profile.active,true);
  assert.equal(calls.find(call=>call[0]==='result')[1],'activate');

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    return {reload_required:true,loopback_proxy:{state:'repaired',changed:true}};
  };
  await context.confirmConnection();
  assert.equal(calls.find(call=>call[0]==='result')[2].loopback_proxy.changed,true);

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    return {reload_required:true,warnings:[{file:'config.toml',provider:'optional',reason:'unsupported_upstream',detail:'非本机的上游必须使用 HTTPS'}]};
  };
  await context.confirmConnection();
  assert.equal(calls.find(call=>call[0]==='result')[2].warnings[0].provider,'optional');
  assert.equal(element('connect-confirm-message').hidden,true);

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    context.state.status.connection_reload_required=true;
    return {reload_required:false};
  };
  await context.confirmConnection();
  assert.equal(calls.find(call=>call[0]==='result')[2].reload_required,false,'An unchanged connection must not require another restart');
  assert.equal(calls.some(call=>call[0]==='message'),false,'Success belongs in the inline result');

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{throw Error('接入失败');};
  await context.confirmConnection();
  assert.equal(element('connect-confirm-message').textContent,'接入失败');
  assert.equal(element('connect-confirm-message').hidden,false);
  assert.equal(calls.some(call=>call[0]==='result'),false);
  assert.equal(calls.some(call=>call[0]==='message'),false,'Connection failures stay in the confirmation');

  ({context,element,calls}=fixture(false));
  context.updateSettingsSaveState=()=>{};
  context.applyLinkAction=async action=>{
    calls.push(['link',action]);context.state.status.profile={active:true,can_restore:true};
    return {reload_required:true,loopback_proxy:{changed:true}};
  };
  vm.runInContext(source.slice(source.indexOf('async function linkAction(which)'),source.indexOf('$("session-search").addEventListener')),context);
  await context.linkAction('activate');
  assert.equal(calls.some(call=>call[0]==='message'),false,'The low-level link action must not create a banner');

  ({context,element,buttons,calls}=fixture(true));
  context.renderCompressionMode();
  assert.equal(buttons[1].attrs['aria-pressed'],'true');
  assert.equal(buttons[0].disabled,false);
  context.requestCompressionChange('off');
  assert.equal(element('compression-confirm-dialog').open,false);
  context.requestCompressionChange('disconnected');
  assert.equal(element('compression-confirm-dialog').open,true);
  assert.equal(calls.length,0);
  await context.confirmCompressionChange();
  assert.deepEqual(calls.filter(call=>call[0]==='link'),[['link','restore']]);
  assert.equal(context.state.status.profile.active,false);
  assert.equal(element('compression-confirm-dialog').open,false);
  assert.equal(calls.find(call=>call[0]==='result')[1],'restore');
  assert.equal(context.compressionSaving,false);
  assert.equal(calls.some(call=>call[0]==='message'),false,'Disconnect must not create a banner');

  ({context,element,buttons,calls}=fixture(true));
  context.showConnectionFeedback('activate',{reload_required:true,wsl:{targets:[{distribution:'Ubuntu',active:true,changed:true}],
    warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'bash: connection refused'}]}});
  const result=calls.find(call=>call[0]==='result')[2];
  assert.equal(result.wsl.targets[0].distribution,'Ubuntu');
  assert.equal(result.wsl.warnings[0].detail,'bash: connection refused','WSL diagnostics stay structured');

  ({context,buttons}=fixture(true));
  context.state.status.provider_sync_error='OSError';
  context.renderCompressionMode();
  assert.equal(buttons[0].disabled,false,'A failed background scan must not prevent disconnecting');
  context.state.status.profile.config_error='unreadable';
  context.renderCompressionMode();
  assert(buttons.every(button=>button.disabled));
  ({context,element,buttons,calls}=fixture(true));
  context.state.status.connection_resume={id:'upgrade-1',state:'complete',result:{reload_required:true,changed:true}};
  context.renderConnectionResume();
  assert.equal(element('connection-resume-status').textContent,'接入配置已自动同步');
  assert.equal(calls.filter(call=>call[0]==='result').length,1);
  assert(!html.includes('id="connection-resume-dialog"'),'Automatic results belong in settings');
  context.rememberConnectionResume();context.renderConnectionResume();
  assert.equal(calls.filter(call=>call[0]==='result').length,1,'The same event is delivered once');
  context.state.status.connection_resume={id:'upgrade-2',state:'complete',result:{reload_required:true,changed:false}};
  context.renderConnectionResume();
  assert.equal(calls.filter(call=>call[0]==='result').length,1,'Sticky legacy flags must not create new notices');
  context.state.status.connection_resume={id:'upgrade-3',state:'attention',result:{wsl:{warnings:[{distribution:'HCE',reason:'relay_unreachable',detail:'connection refused'}]}}};
  context.renderConnectionResume();
  assert.equal(element('connection-resume-retry').hidden,false);
  assert(calls.filter(call=>call[0]==='result')[1][2].wsl.warnings[0].detail.includes('connection refused'));
  context.api=async(path)=>{assert.equal(path,'/api/connection/resume');return {id:'retry-1',state:'pending'};};
  await context.retryConnectionResume();
  context.renderCompressionMode();
  assert(buttons.every(button=>button.disabled),'Wait for the automatic synchronization before changing connection mode');
  context.state.status.connection_resume={id:'retry-1',state:'complete',result:{reload_required:false}};
  context.renderConnectionResume();
  assert.equal(element('connection-resume-status').textContent,'接入配置已自动同步');
  assert.equal(element('connection-resume-retry').hidden,true,'A successful retry leaves no stale error or loading state');
  context.api=async()=>{throw Error('Synthetic retry failure');};
  await context.retryConnectionResume();
  assert.equal(element('connection-resume-status').textContent,'Synthetic retry failure');
  console.log('Connection controls: manual confirmations and automatic upgrade synchronization covered.');
})().catch(error=>{console.error(error);process.exitCode=1;});
