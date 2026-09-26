# -*- coding: utf-8 -*-
"""
===================================
BaostockFetcher - 备用数据源 2 (Priority 3)
===================================

数据来源：证券宝（Baostock）
特点：免费、无需 Token、需要登录管理
优点：稳定、无配额限制

关键策略：
1. 管理 bs.login() 和 bs.logout() 生命周期
2. 使用上下文管理器防止连接泄露
3. 失败后指数退避重试
"""

import logging
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Generator, Any, Dict

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    is_bse_code,
    normalize_stock_code,
    _is_hk_market,
)
import os
from datetime import datetime, timedelta
from typing import cast  # added by mypy_codemod

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 进程级 TTL 缓存：避免 96 次/股票 的重复 Baostock API 调用
# key = (stock_code, start_year), val = (DataFrame, fetched_at)
# --------------------------------------------------------------------------- #
_FINANCIAL_CACHE: dict[tuple[str, str], tuple[pd.DataFrame, datetime]] = {}
_CACHE_TTL_HOURS = 24


def _get_cached_financial_data(
    key: tuple[str, str]
) -> Optional[pd.DataFrame]:
    """从进程缓存中获取财务数据，未命中或过期返回 None。"""
    cached = _FINANCIAL_CACHE.get(key)
    if cached is None:
        return None
    df, fetched_at = cached
    if datetime.now() - fetched_at > timedelta(hours=_CACHE_TTL_HOURS):
        del _FINANCIAL_CACHE[key]
        return None
    return df


def _set_cached_financial_data(key: tuple[str, str], df: pd.DataFrame) -> None:
    """写入进程缓存（带当前时间戳）。"""
    _FINANCIAL_CACHE[key] = (df, datetime.now())


def _is_us_code(stock_code: str) -> bool:
    """
    判断代码是否为美股

    美股代码规则：
    - 1-5个大写字母，如 'AAPL', 'TSLA'
    - 可能包含 '.'，如 'BRK.B'
    """
    code = stock_code.strip().upper()
    return bool(re.match(r"^[A-Z]{1,5}(\.[A-Z])?$", code))


class BaostockFetcher(BaseFetcher):
    """
    Baostock 数据源实现

    优先级：3
    数据来源：证券宝 Baostock API

    关键策略：
    - 使用上下文管理器管理连接生命周期
    - 每次请求都重新登录/登出，防止连接泄露
    - 失败后指数退避重试

    Baostock 特点：
    - 免费、无需注册
    - 需要显式登录/登出
    - 数据更新略有延迟（T+1）
    """

    name = "BaostockFetcher"
    priority = int(os.getenv("BAOSTOCK_PRIORITY", "3"))

    def __init__(self) -> None:
        """初始化 BaostockFetcher"""
        self._bs_module: Optional[Any] = None
        self._stock_name_cache: Dict[str, str] = {}

    def _get_baostock(self) -> Any:
        """
        延迟加载 baostock 模块

        只在首次使用时导入，避免未安装时报错

        Returns:
            baostock 模块对象（运行时 Any，避开了 stub）
        """
        if self._bs_module is None:
            import baostock as bs

            self._bs_module = bs
        return self._bs_module

    @contextmanager
    def _baostock_session(self) -> Generator[Any, Any, Any]:
        """
        Baostock 连接上下文管理器

        确保：
        1. 进入上下文时自动登录
        2. 退出上下文时自动登出
        3. 异常时也能正确登出

        使用示例：
            with self._baostock_session():
                # 在这里执行数据查询
        """
        bs = self._get_baostock()
        login_result = None

        try:
            # 登录 Baostock
            login_result = bs.login()

            if login_result.error_code != "0":
                raise DataFetchError(f"Baostock 登录失败: {login_result.error_msg}")

            logger.debug("Baostock 登录成功")

            yield bs

        finally:
            # 确保登出，防止连接泄露
            try:
                logout_result = bs.logout() if bs is not None else None
                if logout_result is not None and logout_result.error_code == "0":
                    logger.debug("Baostock 登出成功")
                elif logout_result is not None:
                    logger.warning(f"Baostock 登出异常: {logout_result.error_msg}")
                else:
                    logger.debug("Baostock 登出返回空")
            except Exception as e:
                logger.warning(f"Baostock 登出时发生错误: {e}")

    def _convert_stock_code(self, stock_code: str) -> str:
        """
        转换股票代码为 Baostock 格式

        Baostock 要求的格式：
        - 沪市：sh.600519
        - 深市：sz.000001

        Args:
            stock_code: 原始代码，如 '600519', '000001'

        Returns:
            Baostock 格式代码，如 'sh.600519', 'sz.000001'
        """
        raw_code = stock_code.strip()
        upper = raw_code.upper()

        # HK stocks are not supported by Baostock
        if _is_hk_market(raw_code):
            raise DataFetchError(
                f"BaostockFetcher 不支持港股 {raw_code}，请使用 AkshareFetcher"
            )

        # 保留既有小写 baostock 格式输入的内部容错，但用户配置仍推荐 6 位裸代码。
        if raw_code.startswith(("sh.", "sz.")):
            return raw_code.lower()

        exchange_hint = None
        if upper.startswith(("SH", "SS")) or upper.endswith((".SH", ".SS")):
            exchange_hint = "sh"
        elif upper.startswith("SZ") or upper.endswith(".SZ"):
            exchange_hint = "sz"

        code = normalize_stock_code(raw_code)

        if exchange_hint in ("sh", "sz") and code.isdigit() and len(code) == 6:
            return f"{exchange_hint}.{code}"

        # ETF: Shanghai ETF (51xx, 52xx, 56xx, 58xx) -> sh; Shenzhen ETF (15xx, 16xx, 18xx) -> sz
        if len(code) == 6:
            if code.startswith(("51", "52", "56", "58")):
                return f"sh.{code}"
            if code.startswith(("15", "16", "18")):
                return f"sz.{code}"

        # 根据代码前缀判断市场
        if code.startswith(("600", "601", "603", "605", "688")):
            return f"sh.{code}"
        elif code.startswith(("000", "001", "002", "003", "300", "301")):
            return f"sz.{code}"
        else:
            logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
            return f"sz.{code}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """
        从 Baostock 获取原始数据

        使用 query_history_k_data_plus() 获取日线数据

        流程：
        1. 检查是否为美股（不支持）
        2. 使用上下文管理器管理连接
        3. 转换股票代码格式
        4. 调用 API 查询数据
        5. 将结果转换为 DataFrame
        """
        # 美股不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if _is_us_code(stock_code):
            raise DataFetchError(
                f"BaostockFetcher 不支持美股 {stock_code}，请使用 AkshareFetcher 或 YfinanceFetcher"
            )

        # 港股不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if _is_hk_market(stock_code):
            raise DataFetchError(
                f"BaostockFetcher 不支持港股 {stock_code}，请使用 AkshareFetcher"
            )

        # 北交所不支持，抛出异常让 DataFetcherManager 切换到其他数据源
        if is_bse_code(stock_code):
            raise DataFetchError(
                f"BaostockFetcher 不支持北交所 {stock_code}，将自动切换其他数据源"
            )

        # 转换代码格式
        bs_code = self._convert_stock_code(stock_code)

        logger.debug(
            f"调用 Baostock query_history_k_data_plus({bs_code}, {start_date}, {end_date})"
        )

        with self._baostock_session() as bs:
            try:
                # 查询日线数据
                # adjustflag: 1-后复权，2-前复权，3-不复权
                rs = bs.query_history_k_data_plus(
                    code=bs_code,
                    fields="date,open,high,low,close,volume,amount,pctChg",
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",  # 日线
                    adjustflag="2",  # 前复权
                )

                if rs.error_code != "0":
                    raise DataFetchError(f"Baostock 查询失败: {rs.error_msg}")

                # 转换为 DataFrame
                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())

                if not data_list:
                    raise DataFetchError(f"Baostock 未查询到 {stock_code} 的数据")

                df = pd.DataFrame(data_list, columns=rs.fields)

                return df

            except Exception as e:
                if isinstance(e, DataFetchError):
                    raise
                raise DataFetchError(f"Baostock 获取数据失败: {e}") from e

    @staticmethod
    def _strip_code_suffix(code: object) -> str:
        """Strip the exchange suffix from a Baostock code field (e.g. 'sh.600000' -> '600000')."""
        text = str(code)
        return text.split(".", 1)[1] if "." in text else text

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Baostock 数据

        Baostock 返回的列名：
        date, open, high, low, close, volume, amount, pctChg

        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()

        # 列名映射（只需要处理 pctChg）
        column_mapping = {
            "pctChg": "pct_chg",
        }

        df = df.rename(columns=column_mapping)

        # 数值类型转换（Baostock 返回的都是字符串）
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "pct_chg"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 添加股票代码列
        df["code"] = stock_code

        # 只保留需要的列
        keep_cols = ["code"] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        # Use .loc[:, ...] so the result stays a DataFrame even with a single column.
        df = df.loc[:, existing_cols]

        return df

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称

        使用 Baostock 的 query_stock_basic 接口获取股票基本信息

        Args:
            stock_code: 股票代码

        Returns:
            股票名称，失败返回 None
        """
        # 检查缓存
        if hasattr(self, "_stock_name_cache") and stock_code in self._stock_name_cache:
            return cast(Optional[str], self._stock_name_cache[stock_code])

        # 初始化缓存
        if not hasattr(self, "_stock_name_cache"):
            self._stock_name_cache = {}

        try:
            bs_code = self._convert_stock_code(stock_code)

            with self._baostock_session() as bs:
                # 查询股票基本信息
                rs = bs.query_stock_basic(code=bs_code)

                if rs.error_code == "0":
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())

                    if data_list:
                        # Baostock 返回的字段：code, code_name, ipoDate, outDate, type, status
                        fields = rs.fields
                        name_idx = (
                            fields.index("code_name") if "code_name" in fields else None
                        )
                        if name_idx is not None and len(data_list[0]) > name_idx:
                            name = data_list[0][name_idx]
                            self._stock_name_cache[stock_code] = name
                            logger.debug(
                                f"Baostock 获取股票名称成功: {stock_code} -> {name}"
                            )
                            return cast(Optional[str], name)

        except Exception as e:
            logger.warning(f"Baostock 获取股票名称失败 {stock_code}: {e}")

        return None

    def _map_financial_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """将 Baostock 原始列名映射到项目标准字段名"""
        mapping = {
            # 盈利能力 (profit)
            "gpMargin":      "gross_margin_pct",
            "npMargin":      "net_profit_margin_pct",
            "roeAvg":        "roe_pct",
            "MBRevenue":     "revenue",
            "netProfit":     "net_profit",
            "epsTTM":        "eps_ttm",
            "totalShare":    "total_shares",
            "liqaShare":     "float_shares",
            # 营运能力 (operation)
            "NRTurnDays":    "receivable_turnover_days",
            "INVTurnDays":   "inventory_turnover_days",
            "NRTurnRatio":   "receivable_turnover_ratio",
            "INVTurnRatio":  "inventory_turnover_ratio",
            "CATurnRatio":   "current_asset_turnover",
            "AssetTurnRatio": "asset_turnover_ratio",
            # 成长能力 (growth)
            "YOYNI":         "revenue_yoy_pct",
            "YOYEquity":     "equity_yoy_pct",
            "YOYAsset":      "asset_yoy_pct",
            "YOYEPSBasic":   "eps_yoy_pct",
            "YOYPNI":        "net_profit_yoy_pct",
            # 偿债能力 (balance)
            "currentRatio":  "current_ratio",
            "quickRatio":    "quick_ratio",
            "cashRatio":     "cash_ratio",
            "YOYLiability":  "liability_yoy_pct",
            "liabilityToAsset": "debt_to_asset_pct",
            "assetToEquity": "asset_to_equity",
            # 现金流量 (cash_flow)
            "CAToAsset":     "current_asset_to_total_asset",
            "NCAToAsset":    "non_current_asset_to_total_asset",
            "tangibleAssetToAsset": "tangible_asset_ratio",
            "ebitToInterest": "ebit_to_interest",
            "CFOToOR":       "operating_cf_to_revenue",
            "CFOToNP":       "operating_cf_to_net_profit",
            "CFOToGr":       "operating_cf_to_growth",
            # 杜邦分析 (dupont)
            "dupontROE":         "dupont_roe",
            "dupontAssetStoEquity": "dupont_asset_to_equity",
            "dupontAssetTurn":   "dupont_asset_turnover",
            "dupontPnitoni":     "dupont_net_profit_margin",
            "dupontNitogr":      "dupont_revenue_turnover",
            "dupontTaxBurden":   "dupont_tax_burden",
            "dupontIntburden":   "dupont_interest_burden",
            "dupontEbittogr":    "dupont_ebit_to_revenue",
        }
        df = df.copy()
        rename = {k: v for k, v in mapping.items() if k in df.columns}
        df = df.rename(columns=rename)
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def get_financial_data(
        self,
        stock_code: str,
        start_year: str = "2020",
        end_year: Optional[str] = None,
    ) -> Optional[pd.DataFrame]:
        """
        获取季频财务数据（6 个维度，合并为一个 DataFrame）

        查询：盈利 / 营运 / 成长 / 偿债（balance）/ 现金流 / 杜邦
        返回：index=statDate (YYYY-QN)，列=标准字段名

        Args:
            stock_code: 股票代码，如 '300260'
            start_year: 起始年份，默认为 '2020'
            end_year: 结束年份，默认为当前年份

        Returns:
            合并后的财务 DataFrame，失败返回 None
        """
        if end_year is None:
            end_year = datetime.now().strftime("%Y")

        # 进程级缓存查询（24h TTL）
        cache_key = (stock_code, start_year)
        cached_df = _get_cached_financial_data(cache_key)
        if cached_df is not None:
            logger.debug("Baostock 财务数据缓存命中: %s", stock_code)
            return cached_df

        try:
            bs_code = self._convert_stock_code(stock_code)
        except DataFetchError:
            return None

        # 6 个接口：(方法名)
        # 注意：query_debtpaying_data 不存在，用 query_balance_data 代替
        apis = [
            "query_profit_data",
            "query_operation_data",
            "query_growth_data",
            "query_balance_data",
            "query_cash_flow_data",
            "query_dupont_data",
        ]

        def _fetch_quarter(
            bs: Any, api_name: str, code: str, year: str, quarter: str
        ) -> Optional[pd.DataFrame]:
            method = getattr(bs, api_name, None)
            if method is None:
                return None
            rs = method(code, year, quarter)
            if rs.error_code != "0" or not rs.fields:
                return None
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=rs.fields)
            if df.empty or (len(df) == 1 and df.iloc[0].isna().all()):
                return None
            return df

        merged: Optional[pd.DataFrame] = None

        try:
            with self._baostock_session() as bs:
                for year_int in range(int(end_year), int(start_year) - 1, -1):
                    for quarter in ("1", "2", "3", "4"):
                        for api_name in apis:
                            df = _fetch_quarter(bs, api_name, bs_code, str(year_int), quarter)
                            if df is None or df.empty:
                                continue
                            date_col = "statDate" if "statDate" in df.columns else "pubDate"
                            df = df.set_index(date_col)
                            df.index.name = "report_date"
                            if merged is None:
                                merged = df
                            else:
                                merged = merged.combine_first(df)

            if merged is None or merged.empty:
                logger.warning(f"Baostock 财务数据为空: {stock_code}")
                return None

            merged = self._map_financial_columns(merged)

            logger.info(
                f"Baostock 财务数据获取成功: {stock_code}, "
                f"期数={len(merged)}, 字段={list(merged.columns)}"
            )

            # 写入进程级缓存（24h TTL）
            _set_cached_financial_data(cache_key, merged)

            return merged

        except Exception as e:
            logger.warning(f"Baostock 财务数据获取失败 {stock_code}: {e}")
            return None


    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """
        获取股票列表

        使用 Baostock 的 query_stock_basic 接口获取全部股票列表

        Returns:
            包含 code, name 列的 DataFrame，失败返回 None
        """
        try:
            with self._baostock_session() as bs:
                rs = bs.query_stock_basic()

                if rs.error_code == "0":
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())

                    if data_list:
                        df = pd.DataFrame(data_list, columns=rs.fields)
                        df["code"] = df["code"].apply(
                            self._strip_code_suffix
                        )
                        df = df.rename(columns={"code_name": "name"})

                        if not hasattr(self, "_stock_name_cache"):
                            self._stock_name_cache = {}
                        for _, row in df.iterrows():
                            code_value = cast(str, row["code"])
                            name_value = cast(str, row["name"])
                            self._stock_name_cache[code_value] = name_value

                        logger.info(f"Baostock 获取股票列表成功: {len(df)} 条")
                        return df.loc[:, ["code", "name"]]

        except Exception as e:
            logger.warning(f"Baostock 获取股票列表失败: {e}")

        return None
