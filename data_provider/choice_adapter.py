"""Read Choice native data with one serialized, deadline-bounded SDK session.
The optional SDK is loaded only in an owned child; no password or forced login.
Report mapping stays in this adapter; native hangs terminate the owned process.
"""
from __future__ import annotations

import atexit
import importlib
import math
import multiprocessing
import os
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import Lock
from time import monotonic
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, Field

from data_provider.base import is_bse_code, normalize_stock_code
from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record


class NativeReply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    error: str | None = None
    codes: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    data: dict[str, list[float | int | str | None | list[float | int | str | None]]] | list[str] = Field(default_factory=dict)


def _native_error(code: int, message: str) -> str:
    text = message.lower()
    if any(word in text for word in ("login", "password", "token", "认证", "登录", "激活")):
        return "authentication_failed"
    if any(word in text for word in ("quota", "限额", "额度", "次数")):
        return "quota_exhausted"
    if any(word in text for word in ("permission", "无权限", "权限", "授权")):
        return "permission_denied"
    if any(word in text for word in ("timeout", "超时")):
        return "timeout"
    return "business_error"


def _serve_native(connection: Connection) -> None:
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    sdk: Any = None
    logged_in = False
    try:
        while True:
            request = connection.recv()
            if request is None:
                break
            method, args, options, seconds = request
            try:
                if sdk is None:
                    sdk = importlib.import_module("EmQuantAPI").c
                if not logged_in:
                    login = sdk.start(f"ForceLogin=0,RecordLoginInfo=0,HTTPTimeout={max(1, int(seconds))}", logcallback=lambda _: 1)
                    if login.ErrorCode:
                        connection.send({"error": _native_error(login.ErrorCode, login.ErrorMsg)})
                        continue
                    logged_in = True
                result = getattr(sdk, method)(*args, options)
                if result.ErrorCode:
                    connection.send({"error": _native_error(result.ErrorCode, result.ErrorMsg)})
                else:
                    connection.send({"codes": result.Codes, "indicators": result.Indicators,
                                     "dates": [str(d) for d in result.Dates], "data": result.Data})
            except (ImportError, OSError):
                connection.send({"error": "sdk_unavailable"})
            except Exception:
                connection.send({"error": "invalid_response"})
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if logged_in:
            sdk.stop()
        connection.close()


class ChoiceAdapter:
    name = "choice"
    contract_version = "choice-v1"
    _lock = Lock()
    _process: BaseProcess | None = None
    _connection: Connection | None = None

    @classmethod
    def close(cls) -> None:
        process, connection = cls._process, cls._connection
        cls._process, cls._connection = None, None
        if connection is not None:
            try:
                connection.send(None)
            except (BrokenPipeError, OSError):
                pass
            connection.close()
        if process is not None:
            process.join(timeout=0.2)
            if process.is_alive():
                process.terminate()
            process.join(timeout=0.1)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.1)
            process.close()

    @classmethod
    def _native_request(cls, method: Literal["css", "csd", "tradedates"], args: tuple[str, ...], options: str, deadline: float) -> object:
        remaining = deadline - monotonic()
        if remaining <= 0 or not cls._lock.acquire(timeout=max(0.0, remaining)):
            raise SourceReadError(cls.name, "timeout")
        try:
            if deadline <= monotonic():
                raise SourceReadError(cls.name, "timeout")
            if cls._process is None or not cls._process.is_alive():
                cls.close()
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                cls._process = context.Process(target=_serve_native, args=(child,), daemon=True, name="choice-native")
                cls._connection = parent
                cls._process.start()
                child.close()
            assert cls._connection is not None
            cls._connection.send((method, args, options, max(0.0, deadline - monotonic())))
            if not cls._connection.poll(max(0.0, deadline - monotonic())):
                cls.close()
                raise SourceReadError(cls.name, "timeout")
            payload: object = cls._connection.recv()
            return payload
        except (EOFError, BrokenPipeError, OSError) as exc:
            cls.close()
            raise SourceReadError(cls.name, "source_error") from exc
        finally:
            cls._lock.release()

    def query(self, method: Literal["css", "csd", "tradedates"], args: tuple[str, ...], options: str, *, deadline: float) -> NativeReply:
        remaining = int(deadline - monotonic())
        if remaining < 1:
            raise SourceReadError(self.name, "timeout")
        raw = self._native_request(method, args, f"{options},RECVtimeout={remaining}".strip(","), deadline)
        try:
            reply = NativeReply.model_validate(raw)
        except ValidationError as exc:
            raise SourceReadError(self.name, "invalid_response") from exc
        if reply.error:
            raise SourceReadError(self.name, reply.error)
        return reply

    @staticmethod
    def stock_code(code: str) -> str:
        code = normalize_stock_code(code)
        if not code.isdigit() or len(code) != 6:
            raise ValueError("Choice integration currently requires a six-digit A-share code")
        return f"{code}.{'BJ' if is_bse_code(code) else 'SH' if code.startswith(('6', '9')) else 'SZ'}"

    def trading_dates(self, start: date, end: date, *, deadline: float) -> list[str]:
        reply = self.query("tradedates", (start.isoformat(), end.isoformat()), "Market=CNSESH", deadline=deadline)
        if not isinstance(reply.data, list):
            raise SourceReadError(self.name, "invalid_response")
        return sorted({self.report_date(d) for d in reply.data if d})

    @staticmethod
    def report_date(value: object) -> str:
        parts = str(value).strip().replace("/", "-").split("-")
        try:
            return date(*(int(part) for part in parts)).isoformat()
        except (ValueError, TypeError) as exc:
            raise SourceReadError("choice", "invalid_response") from exc

    def css(self, code: str, indicators: str, options: str, *, deadline: float) -> dict[str, object]:
        reply = self.query("css", (self.stock_code(code), indicators), options, deadline=deadline)
        if not isinstance(reply.data, dict):
            raise SourceReadError(self.name, "invalid_response")
        values = reply.data.get(self.stock_code(code))
        if values is None or len(values) != len(reply.indicators) or reply.indicators != indicators.split(","):
            raise SourceReadError(self.name, "invalid_response")
        return dict(zip(reply.indicators, values))

    @staticmethod
    def number(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise SourceReadError("choice", "invalid_response")
        try:
            number = float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise SourceReadError("choice", "invalid_response") from exc
        if not math.isfinite(number):
            raise SourceReadError("choice", "invalid_response")
        return number

    def snapshot(self, code: str, *, deadline: float) -> dict[str, Any]:
        today = date.today()
        out: dict[str, Any] = {"growth": {}, "valuation": {}, "field_meta": {}, "errors": [], "contract": self.contract_version}
        report, disclosure, trade_day = None, None, None
        try:
            header = self.css(code, "NAME,LATESTREPORTNEW", f"EndDate={today}", deadline=deadline)
            out["stock_name"] = header.get("NAME")
            if header.get("LATESTREPORTNEW"):
                report = self.report_date(header["LATESTREPORTNEW"])
            else:
                out["errors"].append("choice:period_unknown")
        except SourceReadError as exc:
            out["errors"].append(str(exc))
        if report is not None:
            try:
                fields = self.css(code, "ROEWA,GPMARGIN,YOYOR,YOYPNI,ACTUALISSUINGDATE", f"ReportDate={report},Type=1,CurType=2,DataAdjustType=2", deadline=deadline)
                if fields.get("ACTUALISSUINGDATE"):
                    disclosure = self.report_date(fields["ACTUALISSUINGDATE"])
                for field, indicator, caliber in (("roe", "ROEWA", "weighted_roe"), ("gross_margin", "GPMARGIN", "gross_margin"), ("revenue_yoy", "YOYOR", "operating_revenue_yoy"), ("net_profit_yoy", "YOYPNI", "parent_net_profit_yoy")):
                    self._adopt(out, "growth", field, fields.get(indicator), "percentage_point", caliber, report, disclosure, "restated", indicator,
                                None if disclosure and disclosure <= today.isoformat() and report <= today.isoformat() else "disclosure_unknown")
                out["growth"].update(report_date=report, disclosure_date=disclosure)
            except SourceReadError as exc:
                out["errors"].append(str(exc))
        try:
            dates = self.trading_dates(today - timedelta(days=14), today, deadline=deadline)
            if not dates:
                raise SourceReadError(self.name, "calendar_unavailable")
            trade_day = dates[-1]
            fields = self.css(code, "PETTM,PB,MV", f"TradeDate={trade_day},EndDate={trade_day},type=8,AdjustFlag=1", deadline=deadline)
            for field, indicator, caliber in (("pe_ratio", "PETTM", "TTM"), ("pb_ratio", "PB", "MRQ"), ("total_mv", "MV", "total_market_cap")):
                self._adopt(out, "valuation", field, fields.get(indicator), "currency_base" if field == "total_mv" else "multiple", caliber, None, trade_day, "unadjusted", indicator)
            out["valuation"]["trade_date"] = trade_day
        except SourceReadError as exc:
            out["errors"].append(str(exc))
        out.update(as_of=trade_day, report_date=report, disclosure_date=disclosure)
        return out

    def _adopt(self, out: dict[str, Any], block: str, field: str, value: object, unit: Literal["currency_base", "percentage_point", "multiple"], caliber: str, period: str | None, observed: str | None, adjustment: str, indicator: str, error: str | None = None) -> None:
        try:
            number = self.number(value)
        except SourceReadError as exc:
            out["errors"].append(str(exc))
            number = None
        if number is None:
            out["field_meta"][field] = {"quality": {"status": "missing", "reason_codes": ["null_value"]}}
            return
        reading = AnchorReading(self.name, number, caliber=caliber, period=period, observed_at=observed,
                                fetched_at=datetime.now(timezone.utc).isoformat(), unit=unit, currency="CNY" if unit == "currency_base" else None,
                                raw_value=str(value), raw_label=indicator, normalization_error=error,
                                statement_scope="consolidated", adjustment=adjustment, disclosed_at=observed if block == "growth" else None)
        out[block][field] = number
        out["field_meta"][field] = adopted_field_record(field, reading, AnchorQuality(status="single_source", reason_codes=("single_family",)))


atexit.register(ChoiceAdapter.close)
