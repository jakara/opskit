"""固定版本组件的内置管理账号登录桥；不接受浏览器提供的凭据或上游地址。"""
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_LOGIN_URLS = {
    'npm': 'http://proxy-manager:81/api/tokens',
    'spug': 'http://spug/api/account/login/',
    'dbx': 'http://dbx:4224/dbx/api/auth/login',
}
_FIELDS = {'npm': ('identity', 'secret'), 'spug': ('username', 'password'), 'dbx': ('password',)}
_NAMES = {'npm': 'NPM', 'spug': 'Spug', 'dbx': 'DBX'}
_CHALLENGES = ('requires_2fa', 'required_mfa', 'requires_mfa', 'required_captcha',
               'requires_captcha', 'challenge_token', 'challengeToken')


class ComponentAuthError(RuntimeError):
    """可安全显示给用户的固定错误，不携带上游响应或凭据。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 16384 and not any(ord(c) < 32 for c in value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class ComponentAuth:
    def __init__(self, path, gateway_host='proxy-manager'):
        try:
            with Path(path).open('rb') as source:
                data = source.read(16385)
            if len(data) > 16384:
                raise ValueError()
            self.credentials = json.loads(data)
            if not isinstance(self.credentials, dict) or set(self.credentials) - set(_FIELDS):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            raise ComponentAuthError('组件登录配置不可用，请重跑安装器') from None
        self.gateway_host = gateway_host
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def login(self, component):
        if not isinstance(component, str) or component not in _FIELDS:
            raise ComponentAuthError('不支持的组件登录')
        name = _NAMES[component]
        credentials = self.credentials.get(component)
        if not isinstance(credentials, dict) or set(credentials) != set(_FIELDS[component]) or any(
                not isinstance(credentials.get(key), str) or not 0 < len(credentials[key]) <= 1024
                for key in _FIELDS[component]):
            raise ComponentAuthError(f'未配置有效的 {name} 内置管理账号，请重跑安装器')
        payload = dict(credentials)
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'OpsKit/ComponentAuth'}
        if component == 'spug':
            payload['type'] = 'default'
            try:
                gateway_ip = socket.gethostbyname(self.gateway_host)
            except OSError:
                raise ComponentAuthError('Spug 登录网关不可用，请检查内部网络') from None
            # 与 NPM HTTP/WS 转发的 $server_addr 一致，沿用 Spug 的 IP 绑定验证。
            headers.update({'X-Real-IP': gateway_ip, 'X-Forwarded-For': gateway_ip})
        try:
            request = Request(_LOGIN_URLS[component], data=_json(payload).encode(), headers=headers)
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(65537)
                if response.status != 200 or len(raw) > 65536:
                    raise ValueError()
                result = json.loads(raw)
                cookies = response.headers.get_all('Set-Cookie', [])
            if not isinstance(result, dict) or result.get('error'):
                raise ValueError()
            data = result.get('data') if component == 'spug' else result
            if not isinstance(data, dict):
                raise ValueError()
            if any(result.get(key) or data.get(key) for key in _CHALLENGES):
                raise ComponentAuthError(f'{name} 要求额外登录验证，请使用组件原生登录')
            storage, cookie = {}, None
            if component == 'npm':
                token, expires = data.get('token'), data.get('expires')
                if not _text(token) or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token) or not _text(expires):
                    raise ValueError()
                expiry = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc) + timedelta(seconds=60):
                    raise ValueError()
                storage = {'authentications': _json([{'token': token, 'expires': expires}])}
            elif component == 'spug':
                user_id, token = data.get('id'), data.get('access_token')
                nickname, superuser, permissions = data.get('nickname'), data.get('is_supper'), data.get('permissions')
                if (type(user_id) is not int or user_id <= 0 or not isinstance(token, str) or
                        not re.fullmatch(r'[a-f0-9]{32}', token) or not _text(nickname) or type(superuser) is not bool or
                        not isinstance(permissions, list) or not all(_text(value) for value in permissions)):
                    raise ValueError()
                storage = {'id': str(user_id), 'token': token, 'nickname': nickname,
                           'is_supper': 'true' if superuser else 'false', 'permissions': _json(permissions), 'login_type': 'default'}
            else:
                if data.get('ok') is not True:
                    raise ValueError()
                for value in cookies:
                    parsed = SimpleCookie()
                    parsed.load(value)
                    if 'dbx_session' not in parsed:
                        continue
                    session = parsed['dbx_session']
                    if (cookie is not None or set(parsed) != {'dbx_session'} or not session['httponly'] or
                            session['path'] != '/dbx' or session['domain'] or '\r' in value or '\n' in value or
                            not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', session.value)):
                        raise ValueError()
                    cookie = value
                if cookie is None:
                    raise ValueError()
            return {'storage': storage, 'set_cookie': cookie}
        except ComponentAuthError:
            raise
        except HTTPError as exc:
            exc.close()
        except (URLError, OSError, HTTPException, ValueError, TypeError, CookieError):
            pass
        raise ComponentAuthError(f'{name} 内置账号登录未通过，请检查组件状态和登录配置') from None
