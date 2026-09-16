/** Shared vendored Socket.IO loader; concurrent consumers share one script. */
let pending;
export function loadSocketIo() {
  if (window.io) return Promise.resolve(window.io);
  if (pending) return pending;
  pending = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/static/js/socket.io.min.js';
    const fail = () => { script.remove(); reject(new Error('socket.io failed to load')); };
    script.onload = () => window.io ? resolve(window.io) : fail();
    script.onerror = fail;
    document.head.appendChild(script);
  }).catch(error => { pending = undefined; throw error; });
  return pending;
}
