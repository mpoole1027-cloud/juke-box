"""deploy/party.py: the .env edit has to be exact; it's the night's alert switch."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy'))

import party  # noqa: E402


def test_set_env_replaces_adds_and_keeps_the_rest(tmp_path):
    env = tmp_path / '.env'
    env.write_text('A=1\nPARTY_ALERTS_MODE=test\n# note\nB=two=2\n')
    os.chmod(env, 0o600)
    party.set_env('PARTY_ALERTS_MODE', 'live', str(env))
    party.set_env('NEW', 'x', str(env))
    assert env.read_text() == 'A=1\nPARTY_ALERTS_MODE=live\n# note\nB=two=2\nNEW=x\n'
    assert oct(env.stat().st_mode & 0o777) == '0o600'
    assert [p.name for p in tmp_path.iterdir()] == ['.env']


def test_set_env_collapses_duplicates(tmp_path):
    env = tmp_path / '.env'
    env.write_text('K=1\nK=2\n')
    party.set_env('K', '3', str(env))
    assert env.read_text() == 'K=3\n'
