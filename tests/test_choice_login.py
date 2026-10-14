"""Exercise login selection and error redaction across the native process boundary."""
from time import monotonic

import pytest

from data_provider.choice_adapter import ChoiceAdapter
from data_provider.cross_source_validator import SourceReadError


@pytest.fixture
def native_sdk(monkeypatch, tmp_path):
    ChoiceAdapter.close()
    monkeypatch.delenv('CHOICE_USERNAME', raising=False)
    monkeypatch.delenv('CHOICE_PASSWORD', raising=False)
    (tmp_path / 'EmQuantAPI.py').write_text('''
import os
from types import SimpleNamespace
class SDK:
    logins = 0
    def start(self, options, **kwargs):
        self.logins += 1
        assert 'ForceLogin=0' in options and 'RecordLoginInfo=0' in options
        if os.environ.get('CHOICE_USERNAME'):
            assert 'UserName=fixture-user' in options
            assert 'PassWord=fixture-password' in options
        else:
            assert 'UserName=' not in options and 'PassWord=' not in options
        if os.environ.get('TEST_CHOICE_DENIED'):
            return SimpleNamespace(ErrorCode=1, ErrorMsg='password fixture-password rejected')
        return SimpleNamespace(ErrorCode=0)
    def css(self, code, fields, options):
        return SimpleNamespace(ErrorCode=0, Codes=[code], Indicators=['LOGIN_COUNT'], Dates=[], Data={code:[self.logins]})
    def stop(self):
        pass
c = SDK()
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    yield
    ChoiceAdapter.close()


@pytest.mark.parametrize('password_login', [False, True])
def test_login_mode_reuses_single_session(native_sdk, monkeypatch, password_login):
    if password_login:
        monkeypatch.setenv('CHOICE_USERNAME', 'fixture-user')
        monkeypatch.setenv('CHOICE_PASSWORD', 'fixture-password')
    adapter = ChoiceAdapter()
    for _ in range(2):
        reply = adapter.query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
        assert reply.data == {'300260.SZ': [1]}


@pytest.mark.parametrize('username,password', [('fixture-user',''), ('','fixture-password'), ('fixture-user','x,ForceLogin=1'), ('fixture-user','x\n')])
def test_invalid_credentials_do_not_fall_back_to_device(native_sdk, monkeypatch, username, password):
    monkeypatch.setenv('CHOICE_USERNAME', username)
    monkeypatch.setenv('CHOICE_PASSWORD', password)
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query('css', ('300260.SZ','NAME'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'invalid_configuration'
    assert 'fixture' not in str(error.value)


def test_supplier_login_error_is_redacted(native_sdk, monkeypatch):
    monkeypatch.setenv('CHOICE_USERNAME', 'fixture-user')
    monkeypatch.setenv('CHOICE_PASSWORD', 'fixture-password')
    monkeypatch.setenv('TEST_CHOICE_DENIED', '1')
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query('css', ('300260.SZ','NAME'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'authentication_failed'
    assert 'fixture-password' not in str(error.value)


@pytest.mark.parametrize('key', ['CHOICE_USERNAME', 'CHOICE_PASSWORD'])
def test_credentials_are_sensitive_deployment_fields(key):
    from src.core.config_registry import get_field_definition
    field = get_field_definition(key)
    assert field['is_sensitive'] is True
    assert field['is_editable'] is False
