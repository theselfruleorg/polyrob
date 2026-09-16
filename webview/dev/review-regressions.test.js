import { it, expect, vi } from 'vitest';
import { load } from '../static/app/chats.js';
import { bindCreate } from '../static/app/work-create.js';
import { Transcript } from '../static/app/transcript.js';
import { renderKb } from '../static/app/agent.js';
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
const reply = body => ({ ok: true, status: 200, json: async () => body });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
function dialog() {
 document.body.innerHTML='<div id="chats-copy" data-unreadable="Unavailable" data-more="Older"></div><dialog><div id="chats-list"></div><p id="chats-state"></p></dialog>';
 return document.querySelector('dialog');
}
it('ignores a late catalog response after a newer refresh', async () => {
 const root=dialog(); const old=deferred();
 const first=load(root,0,()=>old.promise);
 await load(root,0,async()=>reply({sessions:[{id:'new',task:'New'}]}));
 old.resolve(reply({sessions:[{id:'old',task:'Old'}],next_offset:50})); await first;
 expect(root.querySelector('a').getAttribute('href')).toBe('/c/new');
 expect(root.querySelector('[data-chats-more]')).toBeNull();
});
it('deduplicates shifted pages and keeps failed-page retry available', async () => {
 const root=dialog();
 const fetcher=vi.fn().mockResolvedValueOnce(reply({sessions:[{id:'a'}],next_offset:50}))
 .mockResolvedValueOnce({ok:false,status:503}).mockResolvedValueOnce(reply({sessions:[{id:'a'},{id:'b'}]}));
 await load(root,0,fetcher);root.querySelector('button').click();await flush();
 expect(root.querySelector('button').disabled).toBe(false);
 expect(root.querySelectorAll('a')).toHaveLength(1);
 root.querySelector('button').click();await flush();
 expect(root.querySelectorAll('a')).toHaveLength(2);
 expect(fetcher.mock.calls.slice(1).every(([url])=>url.includes('offset=50'))).toBe(true);
});
it('does not turn a malformed catalog into an empty success',async()=>{
 const root=dialog();await load(root,0,async()=>reply({error:'broken'}));
 expect(root.querySelector('#chats-state').textContent).toBe('Unavailable');
});
function form() {
 const f=document.createElement('form');f.dataset.create='cron';
 f.innerHTML='<textarea name="task"></textarea><input name="schedule"><button type="submit">Create</button><p role="status"></p>';
 f.elements.task.value='Task'; f.elements.schedule.value='every 1h';return f;
}
it('coalesces repeated submits and clears only the accepted draft',async()=>{
 const f=form(); const wait=deferred(); const fetcher=vi.fn(()=>wait.promise);bindCreate(f,{fetcher});
 f.dispatchEvent(new Event('submit',{cancelable:true})); f.dispatchEvent(new Event('submit',{cancelable:true}));
 expect(fetcher).toHaveBeenCalledTimes(1);wait.resolve(reply({ok:true,message:'Created'}));await flush();
 expect(f.elements.task.value).toBe('');expect(f.getAttribute('aria-busy')).toBeNull();
});
it('preserves a new draft typed while the previous task is saving',async()=>{
 const f=form();const wait=deferred();bindCreate(f,{fetcher:()=>wait.promise});
 f.dispatchEvent(new Event('submit',{cancelable:true}));f.elements.task.value='Next task';
 wait.resolve(reply({ok:true}));await flush();expect(f.elements.task.value).toBe('Next task');
});
it('prevents concurrent transcript sends and permits retry after refusal',async()=>{
 const wait=deferred(); const fetcher=vi.fn().mockReturnValueOnce(wait.promise).mockResolvedValue(reply({ok:true}));
 const tx=new Transcript(document.createElement('div'),{}, {sessionId:'s',fetcher});
 const first=tx.send('hello');expect((await tx.send('hello')).pending).toBe(true);expect(fetcher).toHaveBeenCalledTimes(1);
 wait.resolve({ok:false,status:403,json:async()=>({error:'Refused'})});await first;
 await tx.send('hello');expect(fetcher).toHaveBeenCalledTimes(2);
});
it('does not show zero knowledge sources when the reader failed',()=>{
 const root=document.createElement('div');renderKb(root,{error:'offline'},{mem_kb_aside:'{count} sources'});
 expect(root.textContent).toContain('— sources');expect(root.textContent).not.toContain('0 sources');
});
it('loads socket.io once for simultaneous consumers and retries failures',async()=>{
 const { loadSocketIo }=await import('../static/app/socket-client.js');delete window.io;
 const first=loadSocketIo();const second=loadSocketIo();expect(first).toBe(second);
 const scripts=[...document.head.querySelectorAll('script[src="/static/js/socket.io.min.js"]')];expect(scripts).toHaveLength(1);
 scripts[0].dispatchEvent(new Event('error'));await expect(first).rejects.toThrow();
 const retry=loadSocketIo();window.io=()=>{};document.head.querySelector('script:last-child').dispatchEvent(new Event('load'));
 expect(await retry).toBe(window.io);delete window.io;
});
it('cold-open Enter respects composition and cannot create duplicate sessions',async()=>{
 vi.resetModules();
 localStorage.setItem('auth_token','stale.jwt.value');
 document.body.dataset.isNew='true';
 document.body.innerHTML='<form id="chat-composer" data-unreachable="Unavailable"><textarea id="chat-input"></textarea><button id="chat-send-btn">Send</button></form><p id="chat-create-status"></p>';
 const wait=deferred();const fetcher=vi.fn(()=>wait.promise);vi.stubGlobal('fetch',fetcher);
 try {
  await import('../static/app/chat-open.js');
  const f=document.querySelector('form');const input=document.querySelector('textarea');input.value='A draft';
  input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true}));
  expect(fetcher).not.toHaveBeenCalled();
  f.dispatchEvent(new Event('submit',{cancelable:true}));f.dispatchEvent(new Event('submit',{cancelable:true}));
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1].headers.Authorization).toBeUndefined();
  wait.resolve({ok:false,status:403,json:async()=>({error:'Refused'})});await flush();
  expect(input.value).toBe('A draft');expect(document.querySelector('#chat-create-status').textContent).toBe('Refused');
 } finally { localStorage.removeItem('auth_token');vi.unstubAllGlobals();delete document.body.dataset.isNew; }
});
it('keeps actionable server validation detail visible',async()=>{
 const { serverAnswer }=await import('../static/app/http.js');
 expect(serverAnswer({message:'Invalid schedule.',detail:'Use a positive interval.'})).toBe('Invalid schedule. Use a positive interval.');
});
