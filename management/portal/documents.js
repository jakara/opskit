'use strict';
let documents = [];
let documentRequest = 0;
let activePDF;
const documentKinds = {markdown: 'Markdown', text: '文本', pdf: 'PDF', word: 'Word', 'legacy-word': '旧版 Word', image: '图片'};

function documentRoute(path = '', anchor = '') {
  const url = new URL('/hub/docs', location.origin);
  if (path) url.searchParams.set('path', path);
  if (anchor) url.hash = anchor;
  return url.pathname + url.search + url.hash;
}
function documentContentURL(path, download = false) {
  const url = new URL('/hub/api/documents/content', location.origin);
  url.searchParams.set('path', path);
  if (download) url.searchParams.set('download', '1');
  return url.pathname + url.search;
}
function selectedDocument() { return new URL(location.href).searchParams.get('path') || ''; }
function documentLink(item, className = '') {
  const element = node('a', 'docs-link ' + className);
  element.href = documentRoute(item.path);
  element.append(node('span', 'document-icon', item.kind === 'word' ? 'W' : item.kind === 'pdf' ? 'P' : '≡'), node('span', '', item.title));
  return element;
}
function renderDocumentList() {
  const query = $('documents-search').value.trim().toLowerCase();
  const visible = documents.filter((item) => [item.title, item.path, item.group].join(' ').toLowerCase().includes(query));
  $('documents-total').textContent = visible.length + ' / ' + documents.length + ' 份文档';
  $('documents-count').textContent = String(documents.length);
  $('document-shortcuts').replaceChildren(...documents.slice(0, 4).map((item) => {
    const entry = documentLink(item, 'document-shortcut');
    entry.append(node('small', '', item.group + ' · ' + documentKinds[item.kind]));
    return entry;
  }));
  if (!documents.length) $('document-shortcuts').append(node('p', 'muted', '暂无交付文档。将资料放入管理模块的 documents 目录，刷新即可查看。'));
  $('document-list').replaceChildren();
  const groups = [...new Set(visible.map((item) => item.group))];
  for (const group of groups) {
    $('document-list').append(node('h3', 'document-group', group));
    for (const item of visible.filter((value) => value.group === group)) {
      const entry = documentLink(item, 'document-item' + (item.path === selectedDocument() ? ' selected' : ''));
      entry.title = item.path;
      if (item.path === selectedDocument()) entry.setAttribute('aria-current', 'page');
      entry.append(node('small', '', documentKinds[item.kind] + ' · ' + Math.max(1, Math.ceil(item.size / 1024)) + ' KiB'), node('small', 'document-relative-path', item.path));
      $('document-list').append(entry);
    }
  }
  if (!visible.length) $('document-list').append(node('p', 'empty', documents.length ? '没有匹配的文档。' : '尚未添加文档。'));
}
async function loadDocuments() {
  $('documents-refresh').disabled = true;
  try {
    documents = (await api('/documents')).documents;
    renderDocumentList();
    await routeDocuments();
  } catch (error) {
    $('document-shortcuts').replaceChildren(node('p', 'muted', '文档暂时无法读取：' + error.message));
    $('document-content').replaceChildren(node('p', 'notice', error.message));
  } finally { $('documents-refresh').disabled = false; }
}
function relativeDocumentURL(value, current) {
  if (!value || value.includes('\\') || /^[\u0000-\u0020]/.test(value)) return null;
  if (value.startsWith('#')) return {path: current, anchor: value.slice(1)};
  if (value.startsWith('/') || /^[a-z][a-z0-9+.-]*:/i.test(value)) return null;
  try {
    const target = new URL(value, 'https://documents.invalid/' + current.split('/').map(encodeURIComponent).join('/'));
    if (target.origin !== 'https://documents.invalid' || target.search) return null;
    const path = decodeURIComponent(target.pathname.slice(1));
    if (!documents.some((item) => item.path === path)) return null;
    return {path, anchor: decodeURIComponent(target.hash.slice(1))};
  } catch (_) { return null; }
}
function scrollDocumentAnchor(anchor) {
  if (!anchor) return;
  try { anchor = decodeURIComponent(anchor.replace(/^#/, '')); } catch (_) { return; }
  const headings = [...$('document-content').querySelectorAll('[data-anchor]')];
  const target = headings.find((item) => item.dataset.anchor === anchor);
  if (target) target.scrollIntoView({block: 'start'});
}
function safeDocumentHTML(html, current, word = false) {
  const fragment = DOMPurify.sanitize(html, {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: ['a', 'p', 'br', 'hr', 'div', 'span', 'strong', 'em', 'b', 'i', 's', 'del', 'sub', 'sup', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'code', 'blockquote', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'img', 'details', 'summary'],
    ALLOWED_ATTR: ['href', 'title', 'src', 'alt', 'width', 'height', 'colspan', 'rowspan', 'align', 'start', 'id'],
    ALLOW_DATA_ATTR: false,
  });
  const anchors = new Map();
  for (const element of fragment.querySelectorAll('[id]')) {
    element.dataset.anchor = element.id;
    element.removeAttribute('id');
  }
  for (const heading of fragment.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    const slug = heading.textContent.toLowerCase().trim().replace(/\s+/g, '-').replace(/[^\p{L}\p{N}_-]/gu, '') || 'heading';
    const index = anchors.get(slug) || 0; anchors.set(slug, index + 1);
    if (!heading.dataset.anchor) heading.dataset.anchor = slug + (index ? '-' + index : '');
  }
  for (const image of fragment.querySelectorAll('img')) {
    const source = image.getAttribute('src') || '';
    const embedded = word && /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i.test(source);
    const target = relativeDocumentURL(source, current);
    if (embedded) {
      image.src = source;
    } else if (target && documents.some((item) => item.path === target.path && item.kind === 'image')) {
      image.src = documentContentURL(target.path);
    } else {
      image.replaceWith(node('span', 'document-image-unavailable', '[图片未加载：仅支持随包图片]'));
      continue;
    }
    image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
  }
  for (const anchor of fragment.querySelectorAll('a')) {
    const href = anchor.getAttribute('href') || '';
    const target = relativeDocumentURL(href, current);
    if (target) {
      anchor.href = documentRoute(target.path, target.anchor); anchor.classList.add('docs-link');
    } else if (/^https?:\/\//i.test(href)) {
      try {
        const url = new URL(href);
        if (url.username || url.password) throw new Error('credentials');
        anchor.href = url.href; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
      } catch (_) { anchor.removeAttribute('href'); }
    } else {
      anchor.removeAttribute('href');
    }
  }
  for (const table of fragment.querySelectorAll('table')) {
    const wrapper = node('div', 'document-table'); table.replaceWith(wrapper); wrapper.append(table);
  }
  return fragment;
}
function closePDF() {
  if (activePDF) { activePDF.destroy().catch(() => {}); activePDF = null; }
}
async function renderPDF(data, request) {
  const pdfjs = await import('/hub/vendor/pdfjs-6.4.299/build/pdf.mjs');
  if (request !== documentRequest) return;
  pdfjs.GlobalWorkerOptions.workerSrc = '/hub/vendor/pdfjs-6.4.299/build/pdf.worker.mjs';
  const loading = pdfjs.getDocument({
    data: new Uint8Array(data), isEvalSupported: false, enableXfa: false, useWasm: false,
    disableFontFace: true, useSystemFonts: true,
    cMapUrl: '/hub/vendor/pdfjs-6.4.299/cmaps/', cMapPacked: true,
    standardFontDataUrl: '/hub/vendor/pdfjs-6.4.299/standard_fonts/',
    wasmUrl: '/hub/vendor/pdfjs-6.4.299/wasm/',
  });
  activePDF = loading;
  const pdf = await loading.promise;
  if (request !== documentRequest) return;
  if (pdf.numPages > 1000) throw new Error('PDF 超过 1000 页，请下载原文件查看');
  const controls = node('div', 'document-pdf-controls');
  const pageLabel = node('span', 'muted');
  const previous = button('← 上一页', 'secondary', () => page(number - 1));
  const next = button('下一页 →', 'secondary', () => page(number + 1));
  const canvas = node('canvas', 'document-pdf-canvas');
  canvas.setAttribute('role', 'img');
  controls.append(previous, pageLabel, next);
  const panel = node('div', 'document-pdf-viewer'); panel.append(controls, canvas);
  $('document-content').replaceChildren(panel);
  let number = 1;
  async function page(value) {
    previous.disabled = true; next.disabled = true;
    try {
      const current = await pdf.getPage(value);
      if (request !== documentRequest) return;
      const base = current.getViewport({scale: 1});
      const ratio = Math.min(devicePixelRatio || 1, 2);
      const scale = Math.min(Math.max(200, panel.clientWidth) / base.width * ratio,
        16384 / Math.max(base.width, base.height), Math.sqrt(12000000 / (base.width * base.height)));
      const viewport = current.getViewport({scale});
      canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height);
      canvas.setAttribute('aria-label', 'PDF 第 ' + value + ' 页');
      await current.render({canvasContext: canvas.getContext('2d'), viewport}).promise;
      if (request !== documentRequest) return;
      number = value; pageLabel.textContent = '第 ' + number + ' / ' + pdf.numPages + ' 页';
    } catch (error) {
      if (request === documentRequest) toast('此页无法显示：' + error.message);
    } finally {
      previous.disabled = number <= 1; next.disabled = number >= pdf.numPages;
    }
  }
  await page(1);
}
async function openDocument(path) {
  closePDF();
  const request = ++documentRequest;
  const item = documents.find((entry) => entry.path === path);
  $('document-download').hidden = !item;
  $('document-title').textContent = item ? item.title : (path ? '文档不存在' : '选择一份文档');
  $('document-path').textContent = item ? item.path + ' · ' + documentKinds[item.kind] : '';
  $('document-content').replaceChildren(node('div', 'empty', item ? '正在打开文档…' : (path ? '文档已移除或不在此交付包中，请从目录重新选择。' : '从左侧目录选择要查看的文档。')));
  if (!item) return;
  $('document-download').href = documentContentURL(path, true);
  $('document-download').setAttribute('download', path.split('/').pop());
  document.title = item.title + ' · OpsKit 文档';
  try {
    if (item.kind === 'image') {
      const image = node('img', 'document-image'); image.alt = item.title; image.src = documentContentURL(path);
      image.addEventListener('error', () => { if (request === documentRequest) $('document-content').replaceChildren(node('p', 'notice', '图片无法显示，请下载原文件检查。')); });
      $('document-content').replaceChildren(image); return;
    }
    if (item.kind === 'legacy-word') {
      $('document-content').replaceChildren(node('p', 'notice', '旧版 .doc 暂不支持在线预览。请下载后用 Word 或 LibreOffice 转为 .docx / PDF，再放入 documents 目录。'));
      return;
    }
    const response = await fetch(documentContentURL(path), {cache: 'no-store'});
    if (!response.ok) {
      const error = await response.json(); throw new Error(error.error || '无法读取文档');
    }
    if (item.kind === 'pdf') {
      await renderPDF(await response.arrayBuffer(), request);
      return;
    }
    let content;
    if (item.kind === 'word') {
      const result = await mammoth.convertToHtml({arrayBuffer: await response.arrayBuffer()}, {externalFileAccess: false});
      content = safeDocumentHTML(result.value, path, true);
    } else if (item.kind === 'markdown') {
      content = safeDocumentHTML(marked.parse(await response.text(), {gfm: true, breaks: false}), path);
    } else {
      content = node('pre', 'document-text', await response.text());
    }
    if (request !== documentRequest) return;
    const article = node('article', 'document-body'); article.append(content);
    $('document-content').replaceChildren(article);
    scrollDocumentAnchor(location.hash);
  } catch (error) {
    if (request === documentRequest) $('document-content').replaceChildren(node('p', 'notice', '无法预览此文档：' + error.message + '。可下载原文件查看。'));
  }
}
async function routeDocuments() {
  const active = ['/hub/docs', '/hub/docs/'].includes(location.pathname);
  $('documents-view').hidden = !active;
  $('home-view').hidden = active;
  if (active) {
    renderDocumentList();
    await openDocument(selectedDocument());
  } else {
    closePDF();
    documentRequest++;
    document.title = '应用工作台 · OpsKit Hub';
  }
}
document.addEventListener('click', (event) => {
  const anchor = event.target.closest('a.docs-link');
  if (!anchor || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const url = new URL(anchor.href);
  if (url.origin !== location.origin) return;
  event.preventDefault(); history.pushState(null, '', url.pathname + url.search + url.hash); routeDocuments();
});
$('documents-search').addEventListener('input', renderDocumentList);
$('documents-refresh').addEventListener('click', loadDocuments);
window.addEventListener('popstate', routeDocuments);
window.addEventListener('hashchange', () => scrollDocumentAnchor(location.hash));
routeDocuments();
loadDocuments();
