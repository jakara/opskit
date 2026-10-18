'use strict';
(() => {
  const form = document.getElementById('login-form');
  const username = document.getElementById('login-username');
  const password = document.getElementById('login-password');
  const submit = document.getElementById('login-submit');
  const error = document.getElementById('login-error');
  function localPath(value) {
    if (typeof value !== 'string' || !value.startsWith('/')) return '/';
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin ? url.pathname + url.search + url.hash : '/';
    } catch (_) { return '/'; }
  }
  const next = localPath(new URLSearchParams(location.search).get('next'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submit.disabled || !form.reportValidity()) return;
    error.textContent = '';
    password.removeAttribute('aria-invalid');
    submit.disabled = true;
    submit.textContent = '正在登录…';
    username.readOnly = password.readOnly = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/auth/api/login', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: username.value.trim(), password: password.value, next}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 401) password.setAttribute('aria-invalid', 'true');
        const messages = {400: '请检查账号与密码后重试。', 401: '账号或密码不正确，请重新输入。',
          429: '尝试次数过多，请稍后再试。', 503: '登录服务暂时不可用，请稍后重试。'};
        throw new Error(typeof data.error === 'string' ? data.error : messages[response.status] || '登录暂时无法完成，请稍后重试。');
      }
      password.value = '';
      for (const key of ['authentications', 'id', 'token', 'nickname', 'is_supper', 'permissions', 'login_type']) {
        try { localStorage.removeItem(key); } catch (_) {}
      }
      location.assign(localPath(data.redirect));
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? '无法连接登录服务，请检查网络后重试。' : failure.message;
      password.value = '';
    } finally {
      submit.disabled = false;
      submit.textContent = '登录并继续';
      username.readOnly = password.readOnly = false;
      form.removeAttribute('aria-busy');
      if (error.textContent) password.focus();
    }
  });
})();
