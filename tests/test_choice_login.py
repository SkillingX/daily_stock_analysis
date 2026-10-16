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
    monkeypatch.delenv('CHOICE_FORCE_LOGIN', raising=False)
    monkeypatch.setenv('TEST_EXPECT_FORCE_LOGIN', '0')
    (tmp_path / 'EmQuantAPI.py').write_text('''
import os
from types import SimpleNamespace
class SDK:
    logins = 0
    kicked = False
    def start(self, options, **kwargs):
        self.logins += 1
        assert 'ForceLogin=' + os.environ['TEST_EXPECT_FORCE_LOGIN'] in options
        assert 'RecordLoginInfo=0' in options
        if os.environ.get('CHOICE_USERNAME'):
            assert 'UserName=fixture-user' in options
            assert 'PassWord=fixture-password' in options
        else:
            assert 'UserName=' not in options and 'PassWord=' not in options
        if os.environ.get('TEST_CHOICE_DENIED'):
            return SimpleNamespace(ErrorCode=1, ErrorMsg='password fixture-password rejected')
        if os.environ.get('TEST_CHOICE_SLOT_OCCUPIED') and 'ForceLogin=1' not in options:
            return SimpleNamespace(ErrorCode=10001009, ErrorMsg='账号登陆数达到上限')
        if os.environ.get('TEST_CHOICE_EXPIRED'):
            return SimpleNamespace(ErrorCode=10001020, ErrorMsg='userInfo has expired, please reactivate')
        return SimpleNamespace(ErrorCode=0)
    def css(self, code, fields, options):
        if fields == 'SIMULATE_KICK':
            self.kicked = True
        if self.kicked:
            return SimpleNamespace(ErrorCode=10001011, ErrorMsg='用户登陆掉线')
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


@pytest.mark.parametrize('failure,code', [
    ('TEST_CHOICE_SLOT_OCCUPIED', 10001009),
    ('TEST_CHOICE_EXPIRED', 10001020),
    ('TEST_CHOICE_DENIED', 1),
])
def test_login_diagnostics_keep_native_code_without_credentials(native_sdk, monkeypatch, caplog, failure, code):
    monkeypatch.setenv('CHOICE_USERNAME', 'fixture-user')
    monkeypatch.setenv('CHOICE_PASSWORD', 'fixture-password')
    monkeypatch.setenv(failure, '1')
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'authentication_failed'
    assert f'native_code={code}' in caplog.text
    assert 'reason=authentication_failed' in caplog.text
    assert 'fixture-user' not in caplog.text
    assert 'fixture-password' not in caplog.text


@pytest.mark.parametrize('password_login', [False, True])
@pytest.mark.parametrize('flag', ['true', '1'])
def test_force_login_claims_occupied_slot_and_reuses_session(native_sdk, monkeypatch, password_login, flag):
    if password_login:
        monkeypatch.setenv('CHOICE_USERNAME', 'fixture-user')
        monkeypatch.setenv('CHOICE_PASSWORD', 'fixture-password')
    monkeypatch.setenv('CHOICE_FORCE_LOGIN', flag)
    monkeypatch.setenv('TEST_EXPECT_FORCE_LOGIN', '1')
    monkeypatch.setenv('TEST_CHOICE_SLOT_OCCUPIED', '1')
    adapter = ChoiceAdapter()
    for _ in range(2):
        reply = adapter.query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
        assert reply.data == {'300260.SZ': [1]}


@pytest.mark.parametrize('flag', [None, 'false', '0'])
def test_default_login_does_not_claim_occupied_slot(native_sdk, monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv('CHOICE_FORCE_LOGIN', flag)
    monkeypatch.setenv('TEST_CHOICE_SLOT_OCCUPIED', '1')
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'authentication_failed'


def test_kicked_session_is_released_before_next_request(native_sdk, monkeypatch, caplog):
    monkeypatch.setenv('CHOICE_FORCE_LOGIN', 'true')
    monkeypatch.setenv('TEST_EXPECT_FORCE_LOGIN', '1')
    adapter = ChoiceAdapter()
    adapter.query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
    with pytest.raises(SourceReadError) as error:
        adapter.query('css', ('300260.SZ', 'SIMULATE_KICK'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'authentication_failed'
    assert 'native_code=10001011' in caplog.text
    reply = adapter.query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
    assert reply.data == {'300260.SZ': [1]}


@pytest.mark.parametrize('flag', ['maybe', '1,RecordLoginInfo=1'])
def test_invalid_force_login_is_configuration_error(native_sdk, monkeypatch, flag):
    monkeypatch.setenv('CHOICE_FORCE_LOGIN', flag)
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query('css', ('300260.SZ', 'NAME'), '', deadline=monotonic()+10)
    assert error.value.reason_code == 'invalid_configuration'


def test_force_login_is_explicit_readonly_deployment_field():
    from src.core.config_registry import get_field_definition
    field = get_field_definition('CHOICE_FORCE_LOGIN')
    assert field['default_value'] == 'false'
    assert field['is_editable'] is False
    assert field['data_type'] == 'boolean'


@pytest.mark.parametrize('key', ['CHOICE_USERNAME', 'CHOICE_PASSWORD'])
def test_credentials_are_sensitive_deployment_fields(key):
    from src.core.config_registry import get_field_definition
    field = get_field_definition(key)
    assert field['is_sensitive'] is True
    assert field['is_editable'] is False
