'use strict';
(() => {
  const components = {
    spug: {name: 'Spug', home: '/spug/home', keys: ['id', 'token', 'nickname', 'is_supper', 'permissions', 'login_type']},
    npm: {name: 'NPM', home: '/npm/', keys: ['authentications']},
    dbx: {name: 'DBX', home: '/dbx/', keys: []},
  };
  const parameters = new URLSearchParams(location.search);
  const component = parameters.get('component');
  const settings = Object.prototype.hasOwnProperty.call(components, component) ? components[component] : null;
  const panel = document.getElementById('component-panel');
  const title = document.getElementById('component-title');
  const status = document.getElementById('component-status');
  const error = document.getElementById('component-error');
  const retry = document.getElementById('component-retry');
  const nativeActions = document.getElementById('component-native-actions');
  const nativeLogin = document.getElementById('component-native');
  let busy = false;
  function destination(value, native) {
    try {
      if (typeof value !== 'string' || !value.startsWith('/')) return settings.home;
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !url.pathname.startsWith('/' + component + '/')) return settings.home;
      if (!native && new RegExp('^/' + component + '/(?:login|auth)(?:/|$)').test(url.pathname)) return settings.home;
      return url.pathname + url.search + url.hash;
    } catch (_) { return settings.home; }
  }
  async function open(native = false) {
    if (busy || !settings) return;
    busy = true;
    panel.setAttribute('aria-busy', 'true');
    retry.hidden = true;
    nativeActions.hidden = true;
    nativeLogin.disabled = true;
    error.hidden = true;
    status.textContent = native ? '正在打开组件原生登录页，请稍候…' : '正在建立管理会话，请稍候…';
    try {
      const response = await fetch(native ? '/auth/api/component-native' : '/auth/api/component-login', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({component, next: parameters.get('next') || settings.home}),
      });
      if (response.status === 401) {
        location.assign('/auth/login?next=' + encodeURIComponent(location.pathname + location.search + location.hash));
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data && typeof data.error === 'string' ? data.error : '管理会话暂时无法建立，请稍后重试。');
      const storage = native ? {} : data && data.storage;
      if (!data || typeof data.redirect !== 'string' || !storage || typeof storage !== 'object' || Array.isArray(storage) ||
          Object.entries(storage).some(([key, value]) => !settings.keys.includes(key) || typeof value !== 'string') ||
          (!native && ((component === 'npm' && !storage.authentications) || (component === 'spug' && !storage.token)))) {
        throw new Error('组件返回的登录信息无效，请重试或返回工作台。');
      }
      if (settings.keys.length) {
        try {
          for (const key of settings.keys) localStorage.removeItem(key);
          for (const [key, value] of Object.entries(storage)) localStorage.setItem(key, value);
        } catch (_) {
          for (const key of settings.keys) { try { localStorage.removeItem(key); } catch (_) {} }
          throw new Error('浏览器无法保存组件登录状态，请允许本站存储后重试。');
        }
      }
      status.textContent = native ? '正在打开 ' + settings.name + ' 原生登录页…' : '管理会话已就绪，正在打开 ' + settings.name + '…';
      location.replace(destination(data.redirect, native));
    } catch (failure) {
      status.textContent = '暂时无法打开 ' + settings.name + '。';
      error.textContent = failure instanceof TypeError ? '无法连接登录服务，请检查网络后重试。' : failure.message;
      error.hidden = false;
      retry.hidden = false;
      nativeActions.hidden = false;
      retry.focus();
    } finally {
      busy = false;
      nativeLogin.disabled = false;
      panel.removeAttribute('aria-busy');
    }
  }
  retry.addEventListener('click', () => open());
  nativeLogin.addEventListener('click', () => open(true));
  if (settings) {
    title.textContent = '正在打开 ' + settings.name;
    document.title = title.textContent + ' · OpsKit';
    open();
  } else {
    panel.removeAttribute('aria-busy');
    title.textContent = '无法打开应用';
    status.textContent = '请选择工作台中的内置应用入口。';
    error.textContent = '此应用入口无效，请返回工作台重新打开。';
    error.hidden = false;
  }
})();
