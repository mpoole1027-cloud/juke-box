"""Rate limits on the endpoints a script would hammer."""
import pytest


@pytest.fixture
def app_module(db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.sc, 'is_authenticated', lambda: True)
    monkeypatch.setattr(app_module.sc, 'search_tracks', lambda q, limit=10: [])
    return app_module


def test_login_guessing_is_throttled(app_module):
    c = app_module.app.test_client()
    codes = [c.post('/api/host/login', json={'password': f'guess{i}'}).status_code
             for i in range(6)]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    assert c.post('/api/host/login', json={'password': 'x'}).get_json()['code'] == 'rate_limited'


def test_join_code_guessing_is_throttled(app_module):
    c = app_module.app.test_client()
    codes = [c.post('/api/join', json={'code': f'AAAA{i:02d}'}).status_code for i in range(11)]
    assert codes[-1] == 429


def test_search_limit_is_per_guest(app_module):
    code = app_module.db.get_setting('party_code')
    a, b = app_module.app.test_client(), app_module.app.test_client()
    for c in (a, b):
        c.get(f'/?p={code}')
        c.get('/')
    for _ in range(30):
        assert a.get('/api/search?q=x').status_code == 200
    assert a.get('/api/search?q=x').status_code == 429
    assert b.get('/api/search?q=x').status_code == 200


def test_proxy_fix_uses_the_forwarded_client_ip(monkeypatch):
    """Behind cloudflared every request comes from 127.0.0.1; the limiter must
    key on the real client from X-Forwarded-For instead."""
    from flask import Flask, request
    from werkzeug.middleware.proxy_fix import ProxyFix
    probe = Flask('probe')
    probe.wsgi_app = ProxyFix(probe.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @probe.route('/ip')
    def ip():
        return f'{request.remote_addr} {request.scheme}'

    # A client-supplied X-Forwarded-For is prepended to; only the last hop counts.
    res = probe.test_client().get('/ip', headers={
        'X-Forwarded-For': '127.0.0.1, 203.0.113.9', 'X-Forwarded-Proto': 'https'})
    assert res.get_data(as_text=True) == '203.0.113.9 https'
