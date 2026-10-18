'use strict';
const $ = (id) => document.getElementById(id);
let state = {entries: [], containers: [], http_services: []};
let toastTimer;
let removing;
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function button(text, className, action) {
  const element = node('button', className, text);
  element.type = 'button'; element.addEventListener('click', action);
  return element;
}
function link(text, url) {
  const element = node('a', 'open-link', text);
  element.href = url; element.target = '_blank'; element.rel = 'noopener noreferrer';
  return element;
}
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3000);
}
async function api(path = '/state', options = {}) {
  const response = await window.opskitFetch('/hub/api' + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试');
  return data;
}
async function load() {
  $('refresh').disabled = true;
  try {
    state = await api();
    if (!location.pathname.startsWith('/hub/docs')) document.title = '应用工作台 · OpsKit Hub';
    $('title').textContent = '应用工作台';
    $('intro').textContent = '本机 Docker、HTTP 服务与常用入口，一处查找。';
    $('discovery-label').textContent = '本机服务发现';
    $('footer-version').textContent = 'OpsKit v0.5 · Hub';
    const notices = [...new Set([state.docker_error, state.http_error].filter(Boolean))];
    $('notice').textContent = notices.join(' '); $('notice').hidden = !notices.length;
    $('updated').textContent = '更新于 ' + new Date(state.updated_at).toLocaleTimeString('zh-CN', {hour12: false});
    render();
  } catch (error) {
    if (error.name === 'OpsKitAuthenticationError') return;
    $('notice').textContent = '工作台暂时无法加载：' + error.message; $('notice').hidden = false;
  } finally { $('refresh').disabled = false; }
}
function matches(value) {
  const query = $('search').value.trim().toLowerCase();
  return !query || value.toLowerCase().includes(query);
}
function render() {
  const running = state.containers.filter((item) => item.state === 'running');
  const tools = state.entries.filter((item) => item.builtin);
  const bookmarks = state.entries.filter((item) => !item.builtin);
  const metrics = [[tools.length, '内置运维工具'], [running.length, '运行容器'], [state.http_services.length, '其他 HTTP 服务']];
  $('stats').replaceChildren(...metrics.map(([value, title]) => {
    const element = node('div', 'stat'); element.append(node('span', '', title), node('strong', '', String(value))); return element;
  }));
  $('builtin-section').hidden = !tools.length;
  $('builtin-count').textContent = String(tools.length);
  const builtinOrder = ['spug', 'dbx', 'logs', 'npm'];
  const visibleTools = tools.filter((item) => matches([item.name, item.url, item.description].join(' '))).sort((a, b) => builtinOrder.indexOf(a.id) - builtinOrder.indexOf(b.id));
  $('builtin-entries').replaceChildren(...visibleTools.map(entryCard));
  if (!visibleTools.length && tools.length) $('builtin-entries').append(node('div', 'empty', '没有匹配的内置工具。'));
  $('entry-count').textContent = String(bookmarks.length);
  const entries = bookmarks.filter((item) => matches([item.name, item.group, item.url, item.description].join(' ')));
  $('entries').replaceChildren(...entries.map(entryCard));
  if (!entries.length) $('entries').append(node('div', 'empty', bookmarks.length ? '没有匹配的入口，试试其他关键词。' : '还没有添加入口。点击“添加入口”，或收藏下方自动发现的服务。'));
  $('group-options').replaceChildren(...[...new Set(['业务系统', '运维工具', '本机服务', ...state.entries.map((item) => item.group)])].map((group) => {
    const option = node('option'); option.value = group; return option;
  }));
  renderContainers();
  renderServices();
}
function entryCard(item) {
  const card = node('article', 'entry' + (item.builtin ? ' builtin-entry' : ''));
  const top = node('div', 'entry-top'); const name = node('div');
  name.append(node('h3', '', item.name), node('div', 'group-label', item.builtin ? 'OPSKIT · 内置工具' : item.group));
  top.append(node('span', 'app-icon', item.name.slice(0, 1)), name);
  const bottom = node('div', 'entry-bottom');
  const url = new URL(item.url, location.origin);
  // 重装后组件 HTML 可能仍在浏览器缓存，入口必须先建立新的原生会话。
  const bridge = item.builtin && ['spug', 'npm', 'dbx'].includes(item.id) &&
    url.origin === location.origin && url.pathname.startsWith('/' + item.id + '/');
  const opening = bridge ? '/auth/open?component=' + item.id + '&next=' + encodeURIComponent(url.pathname + url.search + url.hash) : item.url;
  bottom.append(link('打开应用 ↗', opening), button('编辑', 'text-button', () => edit(item)));
  if (!item.builtin) bottom.append(button('移除', 'text-button danger', () => remove(item)));
  card.append(top, node('p', '', item.description || item.url));
  if (item.builtin) {
    card.append(node('div', 'builtin-address', '端口 ' + (url.port || (url.protocol === 'https:' ? '443' : '80')) + ' · ' + url.pathname));
  }
  card.append(bottom);
  return card;
}
function renderContainers() {
  const filtered = state.containers.filter((item) => (!$('running').checked || item.state === 'running') && matches([item.name, item.project, item.image, item.ports].join(' ')));
  const groups = new Map();
  for (const item of filtered) {
    if (!groups.has(item.project)) groups.set(item.project, []);
    groups.get(item.project).push(item);
  }
  $('containers').replaceChildren();
  for (const [group, items] of groups) {
    const section = node('details'); section.open = true;
    const summary = node('summary', '', group); summary.append(node('span', 'muted', items.length + ' 个容器'));
    const table = node('table'); const head = node('tr');
    for (const title of ['应用 / 容器', '状态', '访问入口', '端口映射']) head.append(node('th', '', title));
    const thead = node('thead'); thead.append(head); table.append(thead); const body = node('tbody');
    for (const item of items) {
      const row = node('tr'); const name = node('td', 'name', item.name);
      name.append(node('div', 'container-name', item.image));
      const status = node('td');
      const statusLabel = {running: '运行中', exited: '已停止', paused: '已暂停', restarting: '重启中', created: '已创建', dead: '异常', removing: '移除中'}[item.state] || item.state;
      status.append(node('span', 'status ' + (item.state === 'running' ? 'running' : ''), statusLabel));
      const actions = node('td'); const actionList = node('div', 'port-actions');
      for (const port of item.published_ports.filter((port) => port.url)) {
        actionList.append(discoveredActions(port, item.name, item.project, 'Docker 应用'));
      }
      if (!actionList.children.length) actionList.append(node('span', 'muted', item.state === 'running' ? '无已识别 Web 入口' : '已停止'));
      actions.append(actionList); const ports = node('td', 'ports');
      for (const port of item.published_ports) ports.append(node('div', '', port.binding + ' → ' + port.target));
      if (!item.published_ports.length) ports.append(node('span', 'muted', '未发布到本机'));
      row.append(name, status, actions, ports); body.append(row);
    }
    table.append(body); const wrapper = node('div', 'table-scroll'); wrapper.append(table); section.append(summary, wrapper); $('containers').append(section);
  }
  if (!filtered.length) $('containers').append(node('div', 'empty', state.docker_error ? 'Docker 连接恢复后，点击刷新重新读取。' : '没有匹配的容器。'));
}
function discoveredActions(item, name, group, description) {
  const action = node('div');
  const url = new URL(item.url);
  const localBrowser = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
  const loopback = ['127.0.0.1', '::1'].includes(item.bind);
  if (loopback && !localBrowser) {
    action.append(node('span', 'muted', '仅宿主机访问；可经 NPM 配置代理'));
  } else {
    if (['0.0.0.0', '::'].includes(item.bind)) url.hostname = location.hostname;
    action.append(link('打开 :' + item.port + ' ↗', url.href));
  }
  action.append(button('收藏入口', 'text-button', () => edit({name, group, url: url.href, description})));
  return action;
}
function renderServices() {
  const services = state.http_services.filter((item) => matches([item.name, item.bind, item.port, item.url].join(' ')));
  $('http-count').textContent = String(state.http_services.length);
  $('http-services').replaceChildren(...services.map((item) => {
    const card = node('article', 'entry');
    const top = node('div', 'entry-top');
    top.append(node('span', 'app-icon', '↗'), node('h3', '', item.name + ' :' + item.port));
    const actions = node('div', 'entry-bottom');
    actions.append(discoveredActions(item, item.name + ' :' + item.port, '本机服务', '自动发现的 HTTP 服务'));
    card.append(top, node('p', '', item.url), node('p', 'muted', 'HTTP ' + item.status + ' · 监听 ' + item.bind), actions);
    return card;
  }));
  if (!services.length) $('http-services').append(node('div', 'empty', state.http_error ? '监听端口发现恢复后，点击刷新重试。' : (state.http_services.length ? '没有匹配的 HTTP 服务，试试其他关键词。' : '没有发现其他 HTTP 服务，已发布的容器端口列在 Docker 区域。')));
}
function edit(item = {}) {
  $('entry-form').reset(); $('entry-id').value = item.id || ''; $('dialog-title').textContent = item.id ? '编辑入口' : '添加入口';
  for (const field of ['name', 'group', 'url', 'description']) $('entry-' + field).value = item[field] || '';
  $('form-error').textContent = ''; $('editor').showModal(); $('entry-name').focus();
}
function remove(item) {
  removing = item;
  $('remove-name').textContent = '移除“' + item.name + '”的工作台入口？';
  $('remove-error').textContent = '';
  $('remover').showModal();
}
$('remove-cancel').addEventListener('click', () => $('remover').close());
$('remove-confirm').addEventListener('click', async () => {
  $('remove-confirm').disabled = true;
  try { await api('/entries/' + removing.id, {method: 'DELETE'}); $('remover').close(); await load(); toast('入口已移除，原配置已备份'); }
  catch (error) { $('remove-error').textContent = error.message; }
  finally { $('remove-confirm').disabled = false; }
});
$('entry-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const id = $('entry-id').value; const values = {};
  for (const field of ['name', 'group', 'url', 'description']) values[field] = $('entry-' + field).value.trim();
  $('save').disabled = true;
  try {
    await api('/entries' + (id ? '/' + id : ''), {method: id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)});
    $('editor').close(); await load(); toast('入口已保存');
  } catch (error) { $('form-error').textContent = error.message; }
  finally { $('save').disabled = false; }
});
$('add').addEventListener('click', () => edit());
$('close').addEventListener('click', () => $('editor').close());
$('cancel').addEventListener('click', () => $('editor').close());
$('refresh').addEventListener('click', load);
$('search').addEventListener('input', render);
$('running').addEventListener('change', renderContainers);
load(); setInterval(load, 30000);
