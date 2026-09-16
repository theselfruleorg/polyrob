import { describe, it, expect, vi } from 'vitest';
import { settingControl } from '../static/app/settings-control.js';
const copy = { read_only:'0', control_saved:'Saved', control_queued:'Removal queued for review:', control_save:'Save', control_confirm:'Confirm', control_cancel:'Cancel', control_confirmation:'Review', unreachable:'Unavailable' };
const reply = body => ({ok:true,status:200,json:async()=>body});
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
function button(root, text) { return [...root.querySelectorAll('button')].find(b=>b.textContent===text); }
describe('real controls', () => {
 it('patches a boolean preference and renders the effective server response', async () => {
  const fetcher=vi.fn(async()=>reply({ok:true,key:'digest.enabled',value:false,source:'pref'}));
  const root=settingControl({key:'digest.enabled',type:'bool',value:true},copy,{fetcher});
  root.querySelector('[role=switch]').click(); button(root,'Save').click(); await flush();
  expect(fetcher.mock.calls[0][0]).toBe('/api/webgate/preferences');
  expect(fetcher.mock.calls[0][1].method).toBe('PATCH');
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({key:'digest.enabled',value:false});
  expect(root.querySelector('[role=status]').textContent).toBe('Saved');
 });
 it('requires explicit guarded confirmation and preserves queued response', async () => {
  const fetcher=vi.fn(async()=>({ ...reply({ok:true,queued:[{entry:'send'}],value:['send']}),status:202 }));
  const root=settingControl({key:'approvals.deny',type:'list',value:['send'],sensitivity:'guarded'},copy,{fetcher});
  root.querySelector('textarea').value=''; button(root,'Save').click();
  expect(fetcher).not.toHaveBeenCalled(); button(root,'Confirm').click(); await flush();
  expect(JSON.parse(fetcher.mock.calls[0][1].body).confirm).toBe(true);
  expect(root.querySelector('[role=status]').textContent).toContain('queued');
  expect(root.querySelector('textarea').value).toBe('send');
 });
 it('patches writable flags with the right route and displays refusal verbatim', async () => {
  const fetcher=vi.fn(async()=>({ok:false,status:403,json:async()=>({error:'operator refused'})}));
  const root=settingControl({name:'MEMORY_ENABLED',type:'bool',value:false,console_writable:true},copy,{flag:true,fetcher});
  button(root,'Save').click();await flush();
  expect(fetcher.mock.calls[0][0]).toBe('/api/webgate/config/MEMORY_ENABLED');
  expect(root.querySelector('[role=status]').textContent).toBe('operator refused');
 });
 it('offers no writes when read-only, denied, or permission missing',()=>{
  for (const options of [{copy:{...copy,read_only:'1'},flag:false,spec:{}},{copy,flag:true,spec:{console_writable:false}},{copy,flag:true,spec:{}}]) {
   const root=settingControl(options.spec,options.copy,{flag:options.flag});expect(root.querySelector('button,input,select')).toBeNull();
  }
 });
 it('saves enum preference and signals theme only on server acceptance', async()=>{
  const fetcher=vi.fn(async()=>reply({ok:true,value:'light'}));
  const listener=vi.fn();document.addEventListener('console-theme',listener,{once:true});
  const root=settingControl({key:'ui.theme',type:'enum',enum_values:['auto','dark','light'],value:'auto'},copy,{fetcher});
  root.querySelector('select').value='light';button(root,'Save').click();await flush();
  expect(JSON.parse(fetcher.mock.calls[0][1].body).value).toBe('light');expect(listener.mock.calls[0][0].detail).toBe('light');
 });
 it('uses schema number bounds and sends invalid input for server validation', async()=>{
  const fetcher=vi.fn(async()=>({ok:false,status:400,json:async()=>({error:'invalid number'})}));
  const root=settingControl({key:'goals.daily_quota',type:'int',min:1,max:100,value:10},copy,{fetcher});
  const input=root.querySelector('input');expect(input.min).toBe('1');expect(input.max).toBe('100');input.value='';button(root,'Save').click();await flush();
  expect(root.querySelector('[role=status]').textContent).toBe('invalid number');
 });
});

it('keeps an unknown boolean unselected until explicitly chosen', async () => {
 const fetcher=vi.fn(async()=>reply({ok:true,value:false}));
 const root=settingControl({key:'unknown',type:'bool',value:null},copy,{fetcher});
 expect(root.querySelector('[role=switch]')).toBeNull();
 button(root,'Save').click(); await flush(); expect(fetcher).not.toHaveBeenCalled();
 root.querySelector('select').value='false'; button(root,'Save').click(); await flush();
 expect(JSON.parse(fetcher.mock.calls[0][1].body).value).toBe(false);
});

it('updates the displayed source from the accepted server answer', async () => {
 const row=document.createElement('tr'); row.innerHTML='<td data-source>default</td><td data-editor><span data-effective>true</span></td>';
 const root=settingControl({key:'digest.enabled',type:'bool',value:true},copy,{fetcher:async()=>reply({ok:true,value:true,source:'pref'})});
 row.querySelector('[data-editor]').append(root); button(root,'Save').click(); await flush();
 expect(row.querySelector('[data-source]').textContent).toBe('pref');
});

it('guarded cancellation never sends a request', () => {
 const fetcher=vi.fn();
 const root=settingControl({key:'approvals.deny',type:'list',value:['send'],sensitivity:'guarded'},copy,{fetcher});
 button(root,'Save').click(); expect(button(root,'Confirm').hidden).toBe(false);
 button(root,'Cancel').click(); expect(button(root,'Confirm').hidden).toBe(true);
 expect(button(root,'Save').hidden).toBe(false); expect(fetcher).not.toHaveBeenCalled();
});
