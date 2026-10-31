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
    if (!elements.has(id)) elements.set(id, {id, attrs:{}, dataset:{}, open:false, hidden:id==='settings-restart-feedback', textContent:'',
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
    compressionRevision:0,connectionRestartNotified:false,connectSource:'header',
    $:element,t:value=>value,setText:(el,value)=>{el.textContent=value;},
    uiI18n:{text:(node,value)=>{node.textContent=typeof value==='function'?value():value;}},
    el:(tag,cls,text)=>({tag,text}),
    document:{querySelectorAll:()=>buttons},
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
  assert.equal(element('settings-restart-feedback').hidden,false);
  assert.equal(context.connectionRestartNotified,true);

  ({context,element}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    return {reload_required:true,loopback_proxy:{state:'repaired',changed:true}};
  };
  await context.confirmConnection();
  assert.equal(element('settings-restart-feedback').hidden,false);
  assert.deepEqual(element('settings-restart-items').children.map(item=>item.text),['Codex 接入与原生压缩','本机代理例外']);
  assert.equal(element('settings-restart-summary').textContent,'已接入。已修复本机代理例外。');

  ({context,element}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    return {reload_required:true,warnings:[{file:'config.toml',provider:'optional',reason:'unsupported_upstream',detail:'非本机的上游必须使用 HTTPS'}]};
  };
  await context.confirmConnection();
  assert.equal(element('settings-restart-feedback').hidden,false);
  assert.equal(element('settings-restart-feedback').dataset.feedbackTarget,'connection-inline-feedback');
  assert.equal(element('settings-restart-summary').textContent,'已接入。\n未接管 optional（config.toml）：非本机的上游必须使用 HTTPS。');
  assert.equal(element('connect-confirm-message').hidden,true);

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{
    context.state.status.profile={active:true,can_restore:true};
    context.state.status.connection_reload_required=true;
    return {reload_required:false};
  };
  await context.confirmConnection();
  assert.equal(element('settings-restart-feedback').hidden,false);
  assert.equal(element('settings-restart-title').textContent,'操作已完成');
  assert.equal(element('settings-restart-description').textContent,'配置无需更改，无需重启。');
  assert.equal(element('settings-restart-items').hidden,true);
  assert.equal(calls.some(call=>call[0]==='message'),false,'Success belongs in the inline result');
  assert.equal(element('settings-restart-feedback').dataset.feedbackTarget,'connection-inline-feedback');
  element('settings-dialog').open=true;
  context.showConfigurationFeedback(['自动压缩阈值']);
  assert.equal(element('settings-restart-feedback').dataset.feedbackTarget,'settings-inline-feedback');
  assert.equal(element('settings-restart-summary').hidden,true,'Later settings saves clear the connection summary');
  assert.equal(element('settings-restart-title').textContent,'重启 ChatGPT App 后生效');
  assert.equal(element('settings-restart-items').hidden,false);
  context.showConnectionFeedback('activate',{});
  assert.equal(element('settings-restart-title').textContent,'重启 ChatGPT App 后生效','Older responses fall back to the actual configuration status');

  ({context,element,calls}=fixture(false));
  context.linkAction=async()=>{throw Error('接入失败');};
  await context.confirmConnection();
  assert.equal(element('connect-confirm-message').textContent,'接入失败');
  assert.equal(element('connect-confirm-message').hidden,false);
  assert.equal(element('settings-restart-feedback').hidden,true);
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
  assert.equal(element('settings-restart-feedback').hidden,false);
  assert.equal(context.compressionSaving,false);
  assert.equal(calls.some(call=>call[0]==='message'),false,'Disconnect must not create a banner');

  ({context,buttons}=fixture(true));
  context.state.status.provider_sync_error='OSError';
  context.renderCompressionMode();
  assert.equal(buttons[0].disabled,false,'A failed background scan must not prevent disconnecting');
  context.state.status.profile.config_error='unreadable';
  context.renderCompressionMode();
  assert(buttons.every(button=>button.disabled));
  console.log('Connection controls: confirmations retained and results stay inline.');
})().catch(error=>{console.error(error);process.exitCode=1;});
