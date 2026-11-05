/**
 * rich-text.js — a model's reply with its bold, code and code blocks drawn.
 *
 * Builds DOM nodes only and never sets HTML, so a reply cannot add markup or a script.
 * Links stay plain text: a link in a reply can come from a page the agent read.
 */
const FENCE = /```[^\n]*\n([\s\S]*?)```/g;
const INLINE = /\*\*([^*\n]+)\*\*|`([^`\n]+)`/g;

function appendInline(parent, text) {
  let at = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > at) parent.appendChild(document.createTextNode(text.slice(at, match.index)));
    const node = document.createElement(match[1] !== undefined ? 'strong' : 'code');
    node.textContent = match[1] !== undefined ? match[1] : match[2];
    parent.appendChild(node);
    at = match.index + match[0].length;
  }
  if (at < text.length) parent.appendChild(document.createTextNode(text.slice(at)));
}

function appendParagraphs(parent, text) {
  text.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean).forEach((part) => {
    const p = document.createElement('p');
    appendInline(p, part);
    parent.appendChild(p);
  });
}

/** Add *text* to *parent* as paragraphs and code blocks. */
export function appendRichText(parent, text) {
  const source = String(text || '');
  let at = 0;
  for (const match of source.matchAll(FENCE)) {
    appendParagraphs(parent, source.slice(at, match.index));
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.textContent = match[1].replace(/\n$/, '');
    pre.appendChild(code);
    parent.appendChild(pre);
    at = match.index + match[0].length;
  }
  appendParagraphs(parent, source.slice(at));
}
