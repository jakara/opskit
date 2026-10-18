'use strict';
(() => {
  let redirecting = false;
  function login() {
    if (!redirecting) {
      redirecting = true;
      location.assign('/auth/login?next=' + encodeURIComponent(location.pathname + location.search + location.hash));
    }
  }
  window.opskitFetch = async (path, options = {}) => {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin) throw new Error('工作台请求必须使用本站地址');
    const headers = new Headers(options.headers);
    if (!['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) headers.set('Content-Type', 'application/json');
    const response = await fetch(url.pathname + url.search, {...options, headers, credentials: 'same-origin', cache: 'no-store'});
    if (response.status === 401) {
      login();
      const error = new Error('登录已过期，正在返回登录页');
      error.name = 'OpsKitAuthenticationError';
      throw error;
    }
    return response;
  };
  const controls = document.getElementById('session-controls');
  const user = document.getElementById('session-user');
  const logout = document.getElementById('session-logout');
  const error = document.getElementById('session-error');
  logout.addEventListener('click', async () => {
    logout.disabled = true;
    error.textContent = '';
    for (const key of ['authentications', 'id', 'token', 'nickname', 'is_supper', 'permissions', 'login_type']) {
      try { localStorage.removeItem(key); } catch (_) {}
    }
    try {
      const response = await window.opskitFetch('/auth/api/logout', {method: 'POST', body: '{}'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '退出暂时未完成，请重试');
      location.assign('/auth/login');
    } catch (failure) {
      if (failure.name !== 'OpsKitAuthenticationError') error.textContent = '退出失败，请稍后重试。';
    } finally { logout.disabled = false; }
  });
  (async () => {
    try {
      const response = await window.opskitFetch('/auth/api/session');
      if (!response.ok) throw new Error('session unavailable');
      const session = await response.json();
      if (!session.authenticated) { login(); return; }
      const name = typeof session.user === 'string' ? session.user : session.user && (session.user.username || session.user.name);
      if (name) { user.textContent = name; user.title = '当前登录账号：' + name; controls.hidden = false; }
    } catch (failure) {
      if (failure.name !== 'OpsKitAuthenticationError') {
        error.textContent = '暂时无法确认登录状态，请刷新重试。';
        error.hidden = false;
      }
    }
  })();
})();
