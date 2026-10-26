"""Issue #50：Choice 必要取数预算优先 + 原生失败证据。

真锁、真子进程、真父进程等待保留。假 SDK 只通过测试临时目录里的
EmQuantAPI.py 进入子进程 sys.path，不新增生产模块选择配置。
"""
import logging
import multiprocessing
import os
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import pytest

from data_provider.base import DataFetcherManager
from data_provider.choice_adapter import ChoiceAdapter, redact_native_text
from data_provider.cross_source_validator import SourceReadError
from src.core.research_run import ResearchRun, ResearchStopped
from tests.test_choice_integration import choice_transport
from tests.test_ifind_choice_integration import ifind_transport

_FAKE_SDK = r'''
import os
from pathlib import Path
from threading import Event
from time import sleep
from types import SimpleNamespace

def _behavior():
    return os.environ.get("CHOICE_FAKE_BEHAVIOR", "")

def _part(kind):
    for item in _behavior().split(","):
        if item.startswith(kind + "="):
            return item.split("=", 1)[1]
    return ""

def _log(line):
    path = os.environ.get("CHOICE_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

def _mark():
    path = os.environ.get("CHOICE_FAKE_MARKER")
    if path:
        Path(path).write_text("entered", encoding="utf-8")

if _behavior() == "import_hang":
    Event().wait()

class SDK:
    def start(self, options, **kwargs):
        _log("login")
        action = _part("login")
        if action == "hang":
            Event().wait()
        if action.startswith("sleep:"):
            sleep(float(action.split(":", 1)[1]))
        if action.startswith("code:"):
            code, _, message = action[5:].partition(":")
            return SimpleNamespace(ErrorCode=int(code), ErrorMsg=message)
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success")

    def css(self, code, indicators, options):
        _log("css:" + indicators)
        _mark()
        action = _part("query")
        if _part("optional") == "hang" and any(name in indicators for name in ("HISSW2021", "MAINBUSINESS", "AHOLDER", "HOLDNAMER")):
            Event().wait()
        if action == "hang":
            Event().wait()
        if action.startswith("sleep:"):
            sleep(float(action.split(":", 1)[1]))
        if action == "raise":
            raise RuntimeError("bad payload: UserName=fixture-user leaked")
        if action == "badshape":
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=indicators.split(","), Dates=[], Data=1)
        if action == "empty":
            fields = indicators.split(",")
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=fields, Dates=[], Data={code: []})
        if action == "mismatch":
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=["600519.SZ"], Indicators=["OTHER"], Dates=[], Data={"600519.SZ": ["x"]})
        if action == "null":
            fields = indicators.split(",")
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=fields, Dates=[], Data={code: [None] * len(fields)})
        if action.startswith("value:"):
            value = action.split(":", 1)[1]
            fields = indicators.split(",")
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=fields, Dates=[], Data={code: [value] * len(fields)})
        if action.startswith("code:"):
            error_code, _, message = action[5:].partition(":")
            return SimpleNamespace(ErrorCode=int(error_code), ErrorMsg=message)
        values = {
            "NAME": "新莱应材", "LATESTREPORTNEW": "2025-12-31", "ACTUALISSUINGDATE": "2026-01-15",
            "SUMASSETNEW": 1, "SUMLIABNEW": 1, "PETTM": 10, "PB": 2, "MV": 100,
            "RATINGTARGETPRICE": 60.0, "RATINGTARGETPRICEMIN": 50.0, "RATINGTARGETPRICEMAX": 70.0,
            "HISSW2021": "机械设备",
        }
        fields = indicators.split(",")
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=fields, Dates=[], Data={code: [values.get(name) for name in fields]})

    def tradedates(self, start, end, options):
        _log("tradedates")
        from datetime import date, timedelta
        if os.environ.get("CHOICE_FAKE_BEHAVIOR") == "dates=outside":
            days = ["1999-01-01"]
        else:
            days = [(date.fromisoformat(end) - timedelta(days=i)).isoformat() for i in reversed(range(5))]
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[], Indicators=[], Dates=[], Data=days)

    def csd(self, code, indicators, start, end, options):
        if os.environ.get("CHOICE_FAKE_BEHAVIOR") == "csd=bad":
            return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=["OTHER"], Dates=[], Data={code: [[1.0]]})
        return SimpleNamespace(ErrorCode=0, ErrorMsg="success", Codes=[code], Indicators=[indicators], Dates=[], Data={code: [[]]})

    def stop(self):
        pass

c = SDK()
'''


@pytest.fixture(autouse=True)
def _capture_choice_info_logs(caplog):
    caplog.set_level(logging.INFO, logger="data_provider.choice_adapter")


@pytest.fixture
def fake_sdk_process(monkeypatch, tmp_path):
    """真 spawn + 真 pipe + 真父等待；子进程只因 sys.path 装到假 EmQuantAPI。"""
    ChoiceAdapter.close()
    (tmp_path / "EmQuantAPI.py").write_text(_FAKE_SDK, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("CHOICE_SDK_MODULE", raising=False)
    monkeypatch.setenv("CHOICE_FORCE_LOGIN", "false")
    monkeypatch.setenv("CHOICE_USERNAME", "fixture-user")
    monkeypatch.setenv("CHOICE_PASSWORD", "fixture-password")
    monkeypatch.setenv("CHOICE_FAKE_LOG", str(tmp_path / "calls.log"))
    yield tmp_path
    ChoiceAdapter.close()


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _terminals(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "Choice call terminal:" in record.getMessage()]


def _starts(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "Choice call start:" in record.getMessage()]


def _wait_for(path: Path, timeout: float = 5) -> None:
    deadline = monotonic() + timeout
    while not path.exists() and monotonic() < deadline:
        sleep(0.02)
    assert path.exists()


def test_optional_research_after_required_queries(ifind_transport, monkeypatch):
    """慢 iFinD 主营/股东不能先于 Choice 必要查询；snapshot 内目标价先于行业/公司。"""
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    config.fundamental_stage_timeout_seconds = 25.0
    order = []
    native = ChoiceAdapter._native_request

    def slow_products(self, code, period, deadline):
        order.append(("ifind_products", monotonic()))
        sleep(8)
        raise SourceReadError("ifind", "timeout")

    def slow_holders(self, code, period, deadline):
        order.append(("ifind_holders", monotonic()))
        sleep(7)
        raise SourceReadError("ifind", "timeout")

    def sdk(method, args, options, deadline):
        if method == "css":
            order.append((args[1], monotonic()))
        return native(method, args, options, deadline)

    monkeypatch.setattr(IfindFetcher, "product_segments", slow_products)
    monkeypatch.setattr(IfindFetcher, "shareholders", slow_holders)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    started = monotonic()
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=25)
    choice_calls = [item for item in order if item[0] not in {"ifind_products", "ifind_holders"}]
    target_at = next(index for index, item in enumerate(choice_calls) if "RATINGTARGETPRICE" in item[0])
    industry_at = next(index for index, item in enumerate(choice_calls) if "HISSW2021" in item[0])
    company_at = next(index for index, item in enumerate(choice_calls) if "MAINBUSINESS" in item[0])
    assert target_at < industry_at
    assert target_at < company_at
    assert choice_calls[target_at][1] - started < 20
    assert not any(item[0] in {"ifind_products", "ifind_holders"} and item[1] < choice_calls[target_at][1] for item in order)
    assert "choice:targets:budget_exhausted" not in (result.get("research") or {}).get("errors", [])
    assert "choice:targets:timeout" not in (result.get("research") or {}).get("errors", [])


def test_qualified_ifind_research_omits_choice_company_after_targets(ifind_transport, monkeypatch):
    """合格 iFinD 主营/股东决定 Choice 缺项：目标价先执行，被省略字段不再重查。"""
    from types import SimpleNamespace
    from data_provider.ifind_fundamental_adapter import IfindFetcher
    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    events = []
    native = ChoiceAdapter._native_request

    def qualified(payload):
        return SimpleNamespace(items=[object()], errors=[], model_dump=lambda mode="json": payload)

    def products(self, code, period, deadline):
        events.append(("ifind_products", monotonic()))
        return qualified({"items": [{"name": "fixture"}]})

    def holders(self, code, period, deadline):
        events.append(("ifind_holders", monotonic()))
        return qualified({"items": [{"name": "fixture"}]})

    def sdk(method, args, options, deadline):
        if method == "css":
            events.append(("choice", args[1], monotonic()))
        return native(method, args, options, deadline)

    monkeypatch.setattr(IfindFetcher, "financial_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(IfindFetcher, "product_segments", products)
    monkeypatch.setattr(IfindFetcher, "shareholders", holders)
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    result = DataFetcherManager().get_fundamental_context("300260", budget_seconds=25)
    choice = [item for item in events if item[0] == "choice"]
    requested = " ".join(item[1] for item in choice)
    target_at = next(item[2] for item in choice if "RATINGTARGETPRICE" in item[1])
    ifind_at = next(item[1] for item in events if item[0] == "ifind_products")
    assert target_at < ifind_at
    for name in ("MAINBUSINESS", "MAINPRODUCT", "AHOLDER", "AHOLDERTYPE", "HOLDNAMER", "HOLDPCTR"):
        assert name not in requested
    industry = [item for item in choice if "HISSW2021" in item[1]]
    assert industry and industry[0][2] > ifind_at
    assert "choice:targets:budget_exhausted" not in (result.get("research") or {}).get("errors", [])


def test_blocking_company_cannot_consume_target_budget(fake_sdk_process, monkeypatch):
    """合格财务已省略后，可选公司查询阻塞也不能让目标价失去实际执行机会。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "optional=hang")
    started = monotonic()
    result = ChoiceAdapter().snapshot(
        "300260",
        deadline=started + 4,
        omit_indicators=("OPERATEREVENEW", "PARENTNETPROFITNEW", "ROEWA", "GPMARGIN", "YOYOR", "YOYPNI"),
    )
    calls = _calls(fake_sdk_process)
    assert any(line.startswith("css:") and "RATINGTARGETPRICE" in line for line in calls)
    target_at = next(index for index, line in enumerate(calls) if "RATINGTARGETPRICE" in line)
    for name in ("HISSW2021", "MAINBUSINESS"):
        later = [index for index, line in enumerate(calls) if name in line]
        assert not later or later[0] > target_at
    assert "OPERATEREVENEW" not in "\n".join(calls)
    assert result["research"]["targets"]["RATINGTARGETPRICE"] == 60.0
    assert "choice:targets:budget_exhausted" not in result["research"]["errors"]
    assert monotonic() - started < 6


def test_budget_exhausted_is_not_dispatched(choice_transport, monkeypatch, caplog):
    """预算不足零分派：不建子进程，不记供应商连接故障。"""
    spawned = []
    monkeypatch.setattr(multiprocessing.get_context("spawn"), "Process",
                        lambda *a, **k: spawned.append(k.get("name")) or pytest.fail("must not spawn"))
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "RATINGTARGETPRICE"), "", deadline=monotonic() + 0.2)
    assert error.value.reason_code == "budget_exhausted"
    assert not spawned
    assert _starts(caplog) and _terminals(caplog)
    assert "stage=not_dispatched" in _terminals(caplog)[0]
    assert "dispatched=False" in _terminals(caplog)[0]


def test_throwing_log_handler_preserves_budget_exhausted(monkeypatch):
    """日志失败不能把预算不足改写成 RuntimeError。"""
    class Boom(logging.Handler):
        def emit(self, record):
            raise RuntimeError("log failed")

    logger = logging.getLogger("data_provider.choice_adapter")
    handler = Boom()
    logger.addHandler(handler)
    try:
        with pytest.raises(SourceReadError) as error:
            ChoiceAdapter().query("css", ("300260.SZ", "RATINGTARGETPRICE"), "", deadline=monotonic() + 0.2)
    finally:
        logger.removeHandler(handler)
    assert type(error.value) is SourceReadError
    assert error.value.reason_code == "budget_exhausted"
    assert "log failed" not in str(error.value)


def test_lock_wait_timeout_does_not_touch_owner_session(monkeypatch, caplog):
    """锁被占用：等待超时记 lock_wait_timeout，不向占用方会话发请求。"""
    from threading import Lock
    held = Lock()
    held.acquire()
    monkeypatch.setattr(ChoiceAdapter, "_lock", held)
    sent = []
    monkeypatch.setattr(ChoiceAdapter, "_process", type("P", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(ChoiceAdapter, "_connection", type("C", (), {"send": lambda self, payload: sent.append(payload)})())
    try:
        with pytest.raises(SourceReadError) as error:
            ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 1.5)
    finally:
        held.release()
    assert error.value.reason_code == "lock_wait_timeout"
    assert not sent
    terminal = _terminals(caplog)[0]
    assert "stage=lock_wait_timeout" in terminal
    assert "execute_s=0.0" in terminal
    assert "wait_s=0.0" not in terminal


def test_real_lock_wait_does_not_send_on_owner_pipe(fake_sdk_process, monkeypatch):
    """真锁被占用时，等待方超时且不向占用方子进程再发请求。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=hang")
    marker = fake_sdk_process / "entered"
    monkeypatch.setenv("CHOICE_FAKE_MARKER", str(marker))
    owner_error: list[BaseException] = []

    def owner() -> None:
        try:
            ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 8)
        except BaseException as exc:
            owner_error.append(exc)

    thread = Thread(target=owner)
    thread.start()
    try:
        _wait_for(marker)
        owner_process = ChoiceAdapter._process
        assert owner_process is not None and owner_process.is_alive()
        with pytest.raises(SourceReadError) as error:
            ChoiceAdapter().query("css", ("300260.SZ", "PETTM"), "", deadline=monotonic() + 1.5)
        assert error.value.reason_code == "lock_wait_timeout"
        assert [line for line in _calls(fake_sdk_process) if line.startswith("css:")] == ["css:NAME"]
        assert owner_process.is_alive()
    finally:
        ChoiceAdapter.close()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_login_hang_is_bounded_and_stage_visible(fake_sdk_process, monkeypatch, caplog):
    """登录挂起：沿原绝对截止时间终止，终态阶段是 waiting_login。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "login=hang")
    started = monotonic()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 1.5)
    assert error.value.reason_code == "login_timeout"
    assert monotonic() - started < 3
    assert _terminals(caplog)[0].count("stage=waiting_login") == 1
    assert "css:" not in "\n".join(_calls(fake_sdk_process))


def test_query_hang_after_login_is_bounded(fake_sdk_process, monkeypatch, caplog):
    """查询挂起：登录完成后终态阶段是 waiting_query，时限不重置。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=hang")
    started = monotonic()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 1.5)
    assert error.value.reason_code == "query_timeout"
    assert monotonic() - started < 3
    assert "stage=waiting_query" in _terminals(caplog)[0]


def test_slow_login_then_query_shares_original_deadline(fake_sdk_process, monkeypatch, caplog):
    """慢登录后再查询仍用原截止时间，不重新发给查询一份完整预算。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "login=sleep:1.2,query=hang")
    started = monotonic()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=started + 2)
    assert error.value.reason_code == "query_timeout"
    assert monotonic() - started < 3.0
    assert _calls(fake_sdk_process).count("login") == 1
    assert [line for line in _calls(fake_sdk_process) if line.startswith("css:")] == ["css:NAME"]
    assert "stage=waiting_query" in _terminals(caplog)[0]


def test_login_rejection_stage_is_carried(fake_sdk_process, monkeypatch, caplog):
    """登录业务拒绝的终态阶段来自子进程，不按原因猜成 query。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "login=code:10002001:permission denied for fixture-user")
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert error.value.reason_code == "permission_denied"
    terminal = _terminals(caplog)[0]
    assert "stage=login " in terminal
    assert "stage=query " not in terminal
    assert "native_code=10002001" in terminal
    assert "fixture-user" not in caplog.text
    assert "fixture-password" not in caplog.text


def test_native_nonzero_code_keeps_redacted_evidence(fake_sdk_process, monkeypatch, caplog):
    """查询非零码：保留错误码与脱敏正文，凭据和已知密钥形态不进日志。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=code:10001013:quota exhausted for fixture-user api_key=sk-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert error.value.reason_code == "quota_exhausted"
    terminal = _terminals(caplog)[0]
    assert "stage=query " in terminal
    assert "native_code=10001013" in terminal
    assert "fixture-user" not in caplog.text
    assert "fixture-password" not in caplog.text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in caplog.text


def test_child_exception_keeps_chain_without_secrets(fake_sdk_process, monkeypatch, caplog):
    """子进程异常：异常类型链保留，凭据不进日志。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=raise")
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert error.value.reason_code == "invalid_response"
    assert "RuntimeError" in _terminals(caplog)[0]
    assert "fixture-user" not in caplog.text


def test_bad_shape_has_one_terminal_even_if_logging_throws(fake_sdk_process, monkeypatch, caplog):
    """坏响应形状：一条开始、一条 parse 终态；日志失败不改变 invalid_response。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=badshape")
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert error.value.reason_code == "invalid_response"
    assert len(_starts(caplog)) == 1
    assert len(_terminals(caplog)) == 1
    assert "stage=parse " in _terminals(caplog)[0]
    assert "ValidationError" in _terminals(caplog)[0]

    class Boom(logging.Handler):
        def emit(self, record):
            raise RuntimeError("log failed")

    logger = logging.getLogger("data_provider.choice_adapter")
    handler = Boom()
    logger.addHandler(handler)
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=badshape")
    try:
        with pytest.raises(SourceReadError) as again:
            ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    finally:
        logger.removeHandler(handler)
    assert again.value.reason_code == "invalid_response"


def test_css_mismatch_terminal_matches_parse_failure(fake_sdk_process, monkeypatch, caplog):
    """公开 css 解析失败时，唯一终态是 parse，不能先记 completed/ok。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=mismatch")
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().css("300260", "NAME", "", deadline=monotonic() + 5)
    assert error.value.reason_code == "invalid_response"
    assert len(_starts(caplog)) == 1
    assert len(_terminals(caplog)) == 1
    terminal = _terminals(caplog)[0]
    assert "stage=parse " in terminal
    assert "reason=invalid_response" in terminal
    assert "reason=ok" not in terminal


def test_calendar_mismatch_terminal_is_parse(fake_sdk_process, monkeypatch, caplog):
    """交易日历越界与 css 一样，唯一终态跟随解析结果。"""
    from datetime import date, timedelta
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "dates=outside")
    end = date.today()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().trading_dates(end - timedelta(days=14), end, deadline=monotonic() + 5)
    assert error.value.reason_code == "invalid_response"
    assert len(_terminals(caplog)) == 1
    assert "stage=parse " in _terminals(caplog)[0]
    assert "reason=ok" not in _terminals(caplog)[0]


def test_capital_series_bad_shape_terminal_is_parse(fake_sdk_process, monkeypatch, caplog):
    """资金序列形状不对时，终态同样是 parse，不是 completed。"""
    from datetime import date, timedelta
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "csd=bad")
    end = date.today()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().capital_series("300260", end - timedelta(days=5), end, deadline=monotonic() + 5)
    assert error.value.reason_code == "invalid_response"
    assert len(_terminals(caplog)) == 1
    assert "stage=parse " in _terminals(caplog)[0]
    assert "reason=ok" not in _terminals(caplog)[0]


def test_css_null_value_remains_completed_empty_coverage(fake_sdk_process, monkeypatch, caplog):
    """指标对齐的空值是有效空覆盖，终态仍是完成。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=null")
    assert ChoiceAdapter().css("300260", "NAME", "", deadline=monotonic() + 5) == {"NAME": None}
    assert len(_terminals(caplog)) == 1
    assert "stage=completed " in _terminals(caplog)[0]
    assert "reason=ok" in _terminals(caplog)[0]


def test_start_and_terminal_include_run_id_and_wait_execute(fake_sdk_process, monkeypatch, caplog):
    """有研究运行时，开始和终态都带 run_id，终态区分等锁与执行耗时。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=null")
    run = ResearchRun(10, run_id="fixture-run-id")
    with run.bind():
        ChoiceAdapter().css("300260", "NAME", "", deadline=monotonic() + 5)
    assert len(_starts(caplog)) == 1
    assert len(_terminals(caplog)) == 1
    assert "run_id=fixture-run-id" in _starts(caplog)[0]
    assert "run_id=fixture-run-id" in _terminals(caplog)[0]
    assert "wait_s=" in _terminals(caplog)[0]
    assert "execute_s=" in _terminals(caplog)[0]
    assert "dim=" in _starts(caplog)[0]


def test_empty_result_is_completed_not_parse_error(fake_sdk_process, monkeypatch, caplog):
    """有效空结果是完成，不是解析失败。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=empty")
    reply = ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert reply.data == {"300260.SZ": []}
    assert len(_starts(caplog)) == 1
    assert len(_terminals(caplog)) == 1
    assert "stage=completed " in _terminals(caplog)[0]
    assert "reason=ok" in _terminals(caplog)[0]


def test_no_response_is_distinct_from_query_timeout(fake_sdk_process, monkeypatch, caplog):
    """子进程在回执阶段前无响应：终态记未收到响应，而不是查询超时。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "import_hang")
    started = monotonic()
    with pytest.raises(SourceReadError) as error:
        ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=started + 1.5)
    assert error.value.reason_code == "timeout"
    assert monotonic() - started < 3
    terminal = _terminals(caplog)[0]
    assert "stage=no_response " in terminal
    assert "未收到响应" in terminal
    assert ChoiceAdapter._process is None or not ChoiceAdapter._process.is_alive()


def test_cancellation_before_dispatch_is_not_supplier_failure(fake_sdk_process):
    """取消发生在分派前：不是供应商失败，也不必杀掉尚未发出的请求。"""
    run = ResearchRun(10)
    started = monotonic()
    with run.bind():
        run.stop("cancelled")
        with pytest.raises(ResearchStopped):
            ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 30)
    assert run.stop_reason == "cancelled"
    assert monotonic() - started < 2


def test_cancel_after_dispatch_releases_process_and_next_request_is_clean(fake_sdk_process, monkeypatch):
    """分派后取消必须终止占用的子进程；下一请求看不到旧响应。"""
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=hang")
    marker = fake_sdk_process / "entered"
    monkeypatch.setenv("CHOICE_FAKE_MARKER", str(marker))
    run = ResearchRun(30)
    errors: list[BaseException] = []

    def call() -> None:
        with run.bind():
            try:
                ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 20)
            except BaseException as exc:
                errors.append(exc)

    thread = Thread(target=call)
    thread.start()
    _wait_for(marker)
    owned = ChoiceAdapter._process
    assert owned is not None and owned.is_alive()
    owned_pid = owned.pid
    run.stop("cancelled")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], ResearchStopped)
    with pytest.raises(ProcessLookupError):
        os.kill(owned_pid, 0)
    assert ChoiceAdapter._process is None
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=value:77")
    reply = ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert reply.data == {"300260.SZ": ["77"]}
    assert ChoiceAdapter._process is not None
    assert ChoiceAdapter._process.pid != owned_pid


def test_sdk_module_env_is_not_a_production_selector(fake_sdk_process, monkeypatch):
    """生产代码忽略 CHOICE_SDK_MODULE，测试替换只靠 sys.path 上的 EmQuantAPI。"""
    monkeypatch.setenv("CHOICE_SDK_MODULE", "does.not.exist")
    monkeypatch.setenv("CHOICE_FAKE_BEHAVIOR", "query=value:sdk-path")
    reply = ChoiceAdapter().query("css", ("300260.SZ", "NAME"), "", deadline=monotonic() + 5)
    assert reply.data == {"300260.SZ": ["sdk-path"]}
    assert "CHOICE_SDK_MODULE" not in Path("data_provider/choice_adapter.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("secret", ["fixture-password"])
def test_redaction_reuses_existing_sanitizer_and_known_secrets(secret):
    text = redact_native_text(
        f"login rejected: {secret}; UserName=fixture-user; Bearer abcdef; api_key=sk-abcdefghijklmnopqrstuvwxyz; https://host/path?token=abc",
        known_secrets=("fixture-user", "fixture-password"),
    )
    assert secret not in text
    assert "fixture-user" not in text
    assert "Bearer abcdef" not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert "https://" not in text
    assert len(text) <= 303


def test_budget_exhausted_target_summary_is_not_connection_failure():
    """页面/报告摘要与原因一致：预算耗尽不提示成供应商连接故障。"""
    from datetime import date
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.scenarios_dim import build_scenarios_dim
    ctx = SharedContext("300260", "fixture", date.today().isoformat())
    ctx.fundamental = {"research": {"source": "choice", "as_of": ctx.as_of, "target_window_days": 180,
                                    "targets": {}, "errors": ["choice:targets:budget_exhausted"]}}
    scenario = build_scenarios_dim(ctx, 27.73)
    assert scenario.status == "degraded"
    assert "预算已用尽" in scenario.degraded_reason
    assert "恢复数据连接" not in scenario.degraded_reason
    assert "近180日窗口未提供机构目标价" not in scenario.degraded_reason


def test_subquery_deadlines_share_one_absolute_budget(ifind_transport, monkeypatch):
    """子查询共享同一绝对截止时间：不各自重新获得完整预算。"""
    config, response, calls = ifind_transport
    config.financial_primary_source = "ifind"
    native = ChoiceAdapter._native_request
    deadlines = []

    def sdk(method, args, options, deadline):
        if method == "css":
            deadlines.append(deadline)
        return native(method, args, options, deadline)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(sdk))
    started = monotonic()
    DataFetcherManager().get_fundamental_context("300260", budget_seconds=25)
    assert len(deadlines) >= 3
    assert max(deadlines) <= started + 25.5
    assert max(deadlines) - min(deadlines) < 1
