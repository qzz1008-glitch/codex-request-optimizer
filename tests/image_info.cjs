"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync("packet_inspector/ui/app.js","utf8");
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.attributes={};this.disabled=false;this.textContent="";}
  append(...children){this.children.push(...children);}
  setAttribute(name,value){this.attributes[name]=value;}
}
const el=(tag,cls,text)=>Object.assign(new Element(tag),{className:cls||"",textContent:text??""});
const requests=[],errors=[],context={URL,t:value=>value,el,
  state:{status:{open_directory_supported:true,open_image_supported:true}},
  document:{createElementNS:(namespaceURI,tag)=>Object.assign(new Element(tag),{namespaceURI})},
  panel:title=>{const node=el("section","panel");node.append(el("h3","",title));return node;},
  api:async(route,data)=>{requests.push({route,data});return {opened:true};},fail:error=>errors.push(error.message)};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("function localImagePath("),source.indexOf("function renderHeaders(")),context);
const descendants=(node,tag)=>[...(node.tagName===tag?[node]:[]),...node.children.flatMap(child=>descendants(child,tag))];
const buttons=panel=>descendants(panel,"button");
const click=button=>button.onclick({currentTarget:button});
const directory="D:\\ip-scaling\\.codex_runtime\\marketplace_listing\\browser-blue-icon-fix\\";
const paths=[directory+"ip-env-scaling-browser-logo.png",directory+"参考图 & icon.PNG","C:\\Other\\second.webp"];
(async()=>{
  const info=context.renderImageInfo(paths),actions=buttons(info);
  assert.equal(actions.length,5,"One folder action per directory and one viewer action per image");
  assert.deepEqual(actions.map(button=>button.title),["打开路径","查看图片","查看图片","打开路径","查看图片"]);
  assert.deepEqual(descendants(info,"code").map(node=>node.textContent),[directory,"ip-env-scaling-browser-logo.png","参考图 & icon.PNG","C:\\Other\\","second.webp"]);
  for(const button of actions){
    assert.equal(button.type,"button");assert.equal(button.attributes["aria-label"],button.title);
    assert.equal(button.children[0].tagName,"svg");assert.equal(button.children[0].attributes["aria-hidden"],"true");
    assert.equal(button.disabled,false);assert.equal(button.textContent,"");
  }
  await click(actions[0]);await click(actions[1]);await click(actions[2]);
  assert.deepEqual(requests.map(({route,data})=>[route,data.path]),[
    ["/api/open-directory",directory],["/api/open-image",paths[0]],["/api/open-image",paths[1]]]);

  let finish;
  context.api=(route,data)=>{requests.push({route,data});return new Promise(resolve=>{finish=resolve;});};
  const live=actions[1],replacement=buttons(context.renderImageInfo(paths))[1];
  live.onclick=replacement.onclick; // The real DOM reconciler refreshes handlers on retained buttons.
  const pending=click(live);
  assert.equal(live.disabled,true);assert.equal(live.attributes["aria-busy"],"true");
  const refreshed=buttons(context.renderImageInfo(paths))[1];assert.equal(refreshed.disabled,true);
  await click(refreshed);assert.equal(requests.length,4,"Polling and repeated clicks must not open duplicate viewers");
  finish({opened:true});await pending;
  assert.equal(live.disabled,false);assert.equal(live.attributes["aria-busy"],"false");
  assert.equal(buttons(context.renderImageInfo(paths))[1].disabled,false);

  context.api=async()=>{throw Error("图片不存在。");};
  await click(live);assert.deepEqual(errors,["图片不存在。"]);assert.equal(live.disabled,false);
  context.state.status.open_image_supported=false;
  const unsupported=buttons(context.renderImageInfo(paths));
  assert.equal(unsupported[0].disabled,false);assert.equal(unsupported[1].disabled,true);
  await click(unsupported[1]);assert.equal(errors.length,1);
  assert.equal(buttons(context.renderImageInfo([])).length,0);
  console.log("image info actions passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
