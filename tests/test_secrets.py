"""Host password storage and the refuse-to-start checks for unsafe settings."""
import os
import sqlite3
import tempfile

import pytest

GOOD_SECRET = 'x' * 64


@pytest.fixture
def fresh_db(monkeypatch):
    """A DB path that init_db hasn't touched yet, with no env leaking in from .env."""
    import database
    monkeypatch.setattr(database, 'DB_PATH', os.path.join(tempfile.mkdtemp(), 'test.db'))
    monkeypatch.delenv('HOST_PASSWORD', raising=False)
    return database


def raw_settings(database):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return dict(conn.execute("SELECT key, value FROM settings").fetchall())
    finally:
        conn.close()


def test_password_is_stored_only_as_a_hash(fresh_db, monkeypatch):
    monkeypatch.setenv('HOST_PASSWORD', 'correct horse')
    fresh_db.init_db()
    settings = raw_settings(fresh_db)
    assert 'host_password' not in settings
    assert 'correct horse' not in settings['host_password_hash']
    assert fresh_db.check_host_password('correct horse')
    assert not fresh_db.check_host_password('wrong')
    assert not fresh_db.check_host_password('')


def test_legacy_plaintext_password_is_migrated(fresh_db):
    fresh_db.init_db()
    conn = sqlite3.connect(fresh_db.DB_PATH)
    conn.execute("DELETE FROM settings WHERE key = 'host_password_hash'")
    conn.execute("INSERT INTO settings (key, value) VALUES ('host_password', 'old-plaintext')")
    conn.commit()
    conn.close()

    fresh_db.init_db()
    assert 'host_password' not in raw_settings(fresh_db)
    assert fresh_db.check_host_password('old-plaintext')


def test_env_password_replaces_the_default_but_not_a_custom_one(fresh_db, monkeypatch):
    fresh_db.init_db()
    assert fresh_db.host_password_is_placeholder()

    monkeypatch.setenv('HOST_PASSWORD', 'from-env-1')
    fresh_db.init_db()
    assert fresh_db.check_host_password('from-env-1')

    fresh_db.set_host_password('set-in-panel')
    monkeypatch.setenv('HOST_PASSWORD', 'from-env-2')
    fresh_db.init_db()
    assert fresh_db.check_host_password('set-in-panel')


@pytest.fixture
def app_module(fresh_db, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.qm, 'start_background_thread', lambda: None)
    return app_module


def test_refuses_to_start_without_a_secret_key(app_module, monkeypatch):
    monkeypatch.setenv('HOST_PASSWORD', 'a-real-password')
    monkeypatch.delenv('FLASK_SECRET_KEY', raising=False)
    monkeypatch.setattr(app_module, 'DEV_MODE', False)
    with pytest.raises(SystemExit):
        app_module.create_app()


def test_refuses_to_start_with_the_default_password(app_module, monkeypatch):
    monkeypatch.setenv('FLASK_SECRET_KEY', GOOD_SECRET)
    monkeypatch.setattr(app_module, 'DEV_MODE', False)
    with pytest.raises(SystemExit):
        app_module.create_app()


def test_starts_with_safe_settings(app_module, monkeypatch):
    monkeypatch.setenv('FLASK_SECRET_KEY', GOOD_SECRET)
    monkeypatch.setenv('HOST_PASSWORD', 'a-real-password')
    monkeypatch.setattr(app_module, 'DEV_MODE', False)
    assert app_module.create_app() is app_module.app


def test_dev_mode_only_warns(app_module, monkeypatch):
    monkeypatch.delenv('FLASK_SECRET_KEY', raising=False)
    monkeypatch.setattr(app_module, 'DEV_MODE', True)
    assert app_module.create_app() is app_module.app


def test_login_and_password_change(app_module, monkeypatch):
    monkeypatch.setenv('HOST_PASSWORD', 'a-real-password')
    app_module.db.init_db()
    c = app_module.app.test_client()
    assert c.post('/api/host/login', json={'password': 'nope'}).status_code == 401
    assert c.post('/api/host/login', json={'password': 'a-real-password'}).status_code == 200

    assert c.post('/api/host/settings', json={'host_password': 'short'}).status_code == 400
    assert c.post('/api/host/settings', json={'host_password': 'party2024'}).status_code == 400
    assert c.post('/api/host/settings', json={'host_password': 'brand-new-pass'}).status_code == 200
    assert app_module.db.check_host_password('brand-new-pass')


def test_password_header_no_longer_grants_host_access(app_module, monkeypatch):
    monkeypatch.setenv('HOST_PASSWORD', 'a-real-password')
    app_module.db.init_db()
    res = app_module.app.test_client().post(
        '/api/host/skip', headers={'X-Host-Password': 'a-real-password'})
    assert res.status_code == 401
