"""Read Choice native data with one serialized, deadline-bounded SDK session.
The optional SDK is loaded only in an owned child; no password or forced login.
Report mapping stays in this adapter; native hangs terminate the owned process.
"""
from __future__ import annotations

import atexit
import importlib
import json
import math
import multiprocessing
import os
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import Lock
from time import monotonic
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Annotated

from pydantic import BaseModel, ConfigDict, ValidationError, Field, model_validator
from icontract import ensure

from data_provider.base import is_bse_code, normalize_stock_code
from data_provider.cross_source_validator import AnchorQuality, AnchorReading, SourceReadError, adopted_field_record


class NativeReply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    error: str | None = None
    codes: list[str] = Field(default_factory=list)
    indicators: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    data: dict[str, list[float | int | str | None | list[float | int | str | None]]] | list[str] = Field(default_factory=dict)


class AnnualReport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True, allow_inf_nan=False)
    report_date: Annotated[str, Field(pattern=r"^\d{4}-12-31$")]
    disclosure_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    source: Literal["choice"] = "choice"
    currency: Literal["CNY"] = "CNY"
    statement_scope: Literal["consolidated"] = "consolidated"
    adjustment: Literal["unadjusted"] = "unadjusted"
    revenue: Decimal | None = None
    operating_cost: Decimal | None = None
    gross_profit: Decimal | None = None
    net_profit: Decimal | None = None
    roe: Decimal | None = None
    gross_margin: Decimal | None = None
    op_cash_flow: Decimal | None = None
    assets_total: Decimal | None = None
    total_debt: Decimal | None = None
    equity_total: Decimal | None = None
    eps: Decimal | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> AnnualReport:
        report, disclosed = date.fromisoformat(self.report_date), date.fromisoformat(self.disclosure_date)
        if not report < disclosed <= date.today():
            raise ValueError("Annual disclosure must follow the report period and be known today")
        return self

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> AnnualReport:
        payload = {key: record[key] for key in cls.model_fields if key in record}
        payload.update({key: record.get(key) for key in ("source", "currency", "statement_scope", "adjustment")})
        report = cls.model_validate_json(json.dumps(payload))
        if record.get("year") != int(report.report_date[:4]):
            raise ValueError("Annual year and report period disagree")
        return report

    @ensure(lambda self, result: result["gross_profit"] is None or self.revenue is None or self.operating_cost is None or abs(Decimal(str(result["gross_profit"])) - (self.revenue-self.operating_cost)) <= Decimal("0.01"), "Gross profit must agree with revenue minus cost")
    def as_record(self) -> dict[str, Any]:
        record = self.model_dump(mode="json")
        for field in ("revenue", "operating_cost", "gross_profit", "net_profit", "roe", "gross_margin", "op_cash_flow", "assets_total", "total_debt", "equity_total", "eps"):
            value = getattr(self, field)
            record[field] = float(value) if value is not None else None
        if self.gross_profit is not None and self.revenue is not None and self.operating_cost is not None:
            computed = self.revenue-self.operating_cost
            if self.gross_profit is not None and abs(self.gross_profit-computed) > Decimal("0.01"):
                record["gross_profit"] = None
                record["errors"] = ["gross_profit:identity_mismatch"]
            else:
                record["gross_profit"] = float(computed)
        record["year"] = int(self.report_date[:4])
        record["period"] = self.report_date
        record["debt_ratio"] = float(self.total_debt/self.assets_total*100) if self.total_debt is not None and self.assets_total is not None and self.assets_total > 0 else None
        record["roe_method"] = "weighted_roe"
        record["field_meta"] = {field: {"source": self.source, "source_family": "eastmoney", "period": self.report_date, "period_basis": "annual", "disclosed_at": self.disclosure_date, "currency": self.currency, "statement_scope": self.statement_scope, "adjustment": self.adjustment, "unit": "percentage_point" if field in {"roe", "gross_margin", "debt_ratio"} else "CNY/share" if field == "eps" else "currency_base"} for field in ("revenue", "operating_cost", "gross_profit", "net_profit", "roe", "gross_margin", "op_cash_flow", "assets_total", "total_debt", "equity_total", "eps", "debt_ratio")}
        return record


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
    contract_version = "choice-v4"
    _lock = Lock()
    _process: BaseProcess | None = None
    _connection: Connection | None = None

    @staticmethod
    def completed_day() -> date:
        """Daily amounts/valuation use closed sessions, not today's partial session."""
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        return now.date() if now.time() >= time(15) else now.date() - timedelta(days=1)

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
        dates = sorted({self.report_date(d) for d in reply.data if d})
        if any(d < start.isoformat() or d > end.isoformat() for d in dates):
            raise SourceReadError(self.name, "invalid_response")
        return dates

    def capital_series(self, code: str, start: date, end: date, *, deadline: float) -> list[tuple[str, float | None]]:
        reply = self.query("csd", (self.stock_code(code), "NETINFLOW", start.isoformat(), end.isoformat()),
                           "Period=1,AdjustFlag=1,CurType=1,Order=1,Market=CNSESH", deadline=deadline)
        if reply.indicators != ["NETINFLOW"] or not isinstance(reply.data, dict):
            raise SourceReadError(self.name, "invalid_response")
        rows = reply.data.get(self.stock_code(code))
        if rows is None or len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != len(reply.dates):
            raise SourceReadError(self.name, "invalid_response")
        values: dict[str, float | None] = {}
        for raw_date, raw_value in zip(reply.dates, rows[0]):
            day = self.report_date(raw_date)
            if day < start.isoformat() or day > end.isoformat():
                raise SourceReadError(self.name, "invalid_response")
            value = self.number(raw_value)
            if day in values and values[day] != value:
                raise SourceReadError(self.name, "invalid_response")
            values[day] = value
        return sorted(values.items(), reverse=True)

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
                balance = self.css(code, "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE", f"ReportDate={report},Type=1,CurType=2,DataAdjustType=2", deadline=deadline)
                disclosed = self.report_date(balance["ACTUALISSUINGDATE"]) if balance.get("ACTUALISSUINGDATE") else None
                if disclosed and report < disclosed <= today.isoformat():
                    out["growth"]["balance_sheet"] = {"assets": self.number(balance.get("SUMASSETNEW")), "liabilities": self.number(balance.get("SUMLIABNEW")), "period": report, "disclosed_at": disclosed, "source": "choice", "currency": "CNY", "unit": "currency_base", "statement_scope": "consolidated", "adjustment": "restated"}
                else:
                    out["errors"].append("choice:balance_sheet:disclosure_unknown")
            except SourceReadError as exc:
                out["errors"].append(f"choice:balance_sheet:{exc.reason_code}")
        try:
            end = self.completed_day()
            dates = self.trading_dates(end - timedelta(days=14), end, deadline=deadline)
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
        out["research"] = {"source": "choice", "as_of": today.isoformat(), "errors": []}
        for name, indicators, options in (
            ("industry", "HISSW2021", f"EndDate={today},ClassiFication=1"),
            ("company", "MAINBUSINESS,MAINPRODUCT,COMPROFILE,AHOLDER,AHOLDERTYPE", f"EndDate={today}"),
            ("targets", "RATINGTARGETPRICE,RATINGTARGETPRICEMIN,RATINGTARGETPRICEMAX", f"EndDate={today},Cycle=180"),
        ):
            try:
                out["research"][name] = self.css(code, indicators, options, deadline=deadline)
            except SourceReadError as exc:
                out["research"]["errors"].append(f"choice:{name}:{exc.reason_code}")
        out["research"]["target_window_days"] = 180
        if report and disclosure:
            try:
                holders = self.css(code, "HOLDNAMER,HOLDPCTR", f"ReportDate={report},Rank=11", deadline=deadline)
                out["research"]["holders"] = {**holders, "period": report, "disclosed_at": disclosure,
                                              "note": "HOLDPCTR为前十名合计持股比例（%），不可分配为各股东比例"}
            except SourceReadError as exc:
                out["research"]["errors"].append(f"choice:holders:{exc.reason_code}")
        return out

    def annual_reports(self, code: str, limit: int = 4, *, deadline: float) -> dict[str, Any]:
        if not 1 <= limit <= 4:
            raise ValueError("Annual reports limit must be between one and four")
        today = date.today()
        out: dict[str, Any] = {"years": [], "valuation": {}, "as_of": today.isoformat(), "source": self.name, "data_contract": self.contract_version, "errors": []}
        mapping = {"revenue": "OPERATEREVENEW", "operating_cost": "OPERATEEXPNEW", "net_profit": "PARENTNETPROFITNEW", "gross_profit": "GROSSMARGIN", "roe": "ROEWA", "gross_margin": "GPMARGIN", "op_cash_flow": "NETOPERATECASHFLOWNEW", "assets_total": "SUMASSETNEW", "total_debt": "SUMLIABNEW", "equity_total": "SUMPARENTEQUITYNEW", "eps": "BASICEPSNEW"}
        for year in range(today.year-1, today.year-limit-2, -1):
            if len(out["years"]) == limit or monotonic() >= deadline:
                break
            report_date = f"{year}-12-31"
            try:
                values = self.css(code, ",".join((*mapping.values(), "ACTUALISSUINGDATE")), f"ReportDate={report_date},Type=1,CurType=2,DataAdjustType=1", deadline=deadline)
                if not values.get("ACTUALISSUINGDATE"):
                    out["errors"].append(f"choice:{report_date}:disclosure_unknown")
                    continue
                disclosure = self.report_date(values["ACTUALISSUINGDATE"])
                if disclosure > today.isoformat() or disclosure <= report_date:
                    out["errors"].append(f"choice:{report_date}:not_disclosed")
                    continue
                numbers: dict[str, Decimal | None] = {}
                for field, indicator in mapping.items():
                    try:
                        value = self.number(values.get(indicator))
                        numbers[field] = Decimal(str(values[indicator])) if value is not None else None
                    except SourceReadError as error:
                        numbers[field] = None
                        out["errors"].append(f"choice:{report_date}:{field}:{error.reason_code}")
                row = AnnualReport(report_date=report_date, disclosure_date=disclosure, **numbers).as_record()
                out["years"].append(row)
                out["errors"].extend(f"choice:{report_date}:{reason}" for reason in row.get("errors") or [])
            except SourceReadError as error:
                out["errors"].append(f"choice:{report_date}:{error.reason_code}")
                if error.reason_code in {"sdk_unavailable", "authentication_failed", "quota_exhausted", "timeout"}:
                    break
        out["status"] = "ok" if len(out["years"]) == limit and not out["errors"] and all(row.get(field) is not None for row in out["years"] for field in mapping) else "partial"
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
