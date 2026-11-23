# -*- coding: utf-8 -*-
"""板块分析申万一级行业市场数据链——iFinD 标准 HTTP 取数与 wire 边界（issue #99）。

范围（本票，父规格 #98 决策 1/2/4）：申万 2021 一级目录（核定配置数据）、
主行业归属解析（正式归属按层级单独查询 + 目录唯一匹配）、31 个一级指数
实时快照 wire 解析与同日合格行筛选、行业/基准指数日线 wire 解析。
成分/同业（#100）与催化/综合（#101）不在本模块。

复用边界（父规格决策 1：仅行为等价复用）：
- 认证与传输按行为等价复用 ``data_provider.ifind_http_daily_adapter`` 的
  ``_resolve_access_token`` / ``_http_api_curl_post``（认证缓存键、刷新与
  锁语义、有界 curl 传输、凭据脱敏语义均不变）；
- 板块入口独立判断资格：只依赖 ``IFIND_HTTP_REFRESH_TOKEN`` 与
  ``IFIND_HTTP_TIMEOUT_SECONDS``（凭据 + 单次请求超时），不依赖个股日线
  开关（``ENABLE_IFIND_HTTP_API``）、数据源 profile 或深度投研配置；
- 板块业务不新增业务缓存（单次报告内取数，不跨报告复用）。

单次请求超时（决策 1）在共享传输的绝对截止上再取小：每次出站的
``request_deadline`` 取 ``min(调用方截止, now + IFIND_HTTP_TIMEOUT_SECONDS)``，
与既有传输的 ``--max-time`` 语义组合生效。
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime
from decimal import Decimal, DecimalException
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import pandas as pd
from icontract import ensure
from pydantic import ValidationError

from data_provider.ifind_http_daily_adapter import (
    _http_api_curl_post,
    _redact_token,
    _resolve_access_token,
)
from src.schemas.sector_vendor import SwIndexCloseTable

_HTTP_ROOT = "https://quantapi.51ifind.com/api/v1"
_BASIC_DATA_ENDPOINT = f"{_HTTP_ROOT}/basic_data_service"
_REALTIME_ENDPOINT = f"{_HTTP_ROOT}/real_time_quotation"
_HISTORY_ENDPOINT = f"{_HTTP_ROOT}/cmd_history_quotation"
_DATA_POOL_ENDPOINT = f"{_HTTP_ROOT}/data_pool"

# 涨跌幅与最新价/昨收交叉复核容差（与 2026-11-18 实测复核口径一致：
# abs((latest/preClose − 1)×100 − changeRatio) < 0.005 个百分点）。
# latest=100.5、preClose=100、changeRatio=0.495 的确切差值是 0.005pp，
# 落在容差边界，拒绝。
_CHANGE_RATIO_TOLERANCE_PP = Fraction("0.005")


def _parse_quote_number(text: str) -> Optional[Decimal]:
    """保留实时行情的十进制字面，超出 Decimal 表示范围时仅该字段缺失。"""
    try:
        return Decimal(text)
    except DecimalException:
        return None


def _change_ratio_within_tolerance(row: Dict[str, Any]) -> bool:
    """qualified 行出口契约谓词（容差判定唯一入口）：带涨跌幅且带复核基准
    （latest/preClose>0）的行必须落在既有容差内；无涨跌幅（成分金额口径行，
    ``require_change=False`` 路径，含交叉复核不合格被空白化的行）或无复核
    基准时不对缺失发明断言。资格循环与出口契约共用本谓词，不重复实现。
    先限制非零输入可由有限浮点值承载，再用精确比值比较容差。"""
    change = row.get("change_pct")
    if change is None:
        return True
    try:
        projected_change = float(change)
        if not math.isfinite(projected_change) or (change != 0 and projected_change == 0):
            return False
        latest = row.get("latest")
        pre_close = row.get("pre_close")
        if latest is None or pre_close is None or pre_close <= 0:
            return True
        projected_latest, projected_pre_close = float(latest), float(pre_close)
        if (not math.isfinite(projected_latest) or not math.isfinite(projected_pre_close)
                or (latest != 0 and projected_latest == 0) or projected_pre_close == 0):
            return False
        # 将 Decimal 字面转换为精确分数，避免上下文舍入改变阈值两侧的判断。
        exact_latest = Fraction(Decimal(str(latest)))
        exact_pre_close = Fraction(Decimal(str(pre_close)))
        implied = (exact_latest / exact_pre_close - 1) * 100
        diff = abs(implied - Fraction(Decimal(str(change))))
    except (DecimalException, OverflowError):
        return False
    return diff < _CHANGE_RATIO_TOLERANCE_PP


class SwSectorFetchError(RuntimeError):
    """iFinD 标准 HTTP 板块取数失败（认证/传输/HTTP 状态/errorcode/形状）。

    消息只携带供应商业务诊断（errorcode/errmsg 截断）与本地异常类型，
    不携带凭据或完整 URL（凭据脱敏由共享传输层保证）。
    """


# ---------------------------------------------------------------------------
# 申万 2021 一级目录（核定配置数据，父规格决策 2）
# ---------------------------------------------------------------------------

SW_L1_DIRECTORY_VERSION = "sw2021-l1-20261118"
"""核定目录版本：申万 2021 一级行业指数目录，2026-11-18 实测核定。"""

SW_L1_DIRECTORY_SOURCE = (
    "申万 2021 一级行业指数目录：iFinD basic_data_service ths_index_short_name_index "
    "2026-11-18 盘后实测核定（31/31 简称逐一核对），对照申万行业分类标准 2021 版"
    "（31 个一级、发布时口径）；供应商单次搜索结果不作为全集依据"
)

SW_L1_DIRECTORY: Tuple[Tuple[str, str], ...] = (
    ("801010.SL", "农林牧渔"),
    ("801030.SL", "基础化工"),
    ("801040.SL", "钢铁"),
    ("801050.SL", "有色金属"),
    ("801080.SL", "电子"),
    ("801110.SL", "家用电器"),
    ("801120.SL", "食品饮料"),
    ("801130.SL", "纺织服饰"),
    ("801140.SL", "轻工制造"),
    ("801150.SL", "医药生物"),
    ("801160.SL", "公用事业"),
    ("801170.SL", "交通运输"),
    ("801180.SL", "房地产"),
    ("801200.SL", "商贸零售"),
    ("801210.SL", "社会服务"),
    ("801230.SL", "综合"),
    ("801710.SL", "建筑材料"),
    ("801720.SL", "建筑装饰"),
    ("801730.SL", "电力设备"),
    ("801740.SL", "国防军工"),
    ("801750.SL", "计算机"),
    ("801760.SL", "传媒"),
    ("801770.SL", "通信"),
    ("801780.SL", "银行"),
    ("801790.SL", "非银金融"),
    ("801880.SL", "汽车"),
    ("801890.SL", "机械设备"),
    ("801950.SL", "煤炭"),
    ("801960.SL", "石油石化"),
    ("801970.SL", "环保"),
    ("801980.SL", "美容护理"),
)
"""申万 2021 一级行业指数目录（指数代码 → 一级行业简称，31 项）。

依据见 ``SW_L1_DIRECTORY_SOURCE`` 与
``.claude/reviews/ifind-sector-api-live-validation-20261118.md``；
目录变更须重核 31 个代码及名称并更新版本号。
"""

_DIRECTORY_BY_NAME: Dict[str, str] = {name: code for code, name in SW_L1_DIRECTORY}


# ---------------------------------------------------------------------------
# 申万 2021 二级目录（核定配置数据，issue #103 工单 01）
# ---------------------------------------------------------------------------

SW_L2_OFFICIAL_DIRECTORY_VERSION = "sw2021-l2-official-20261120"
"""官方发布目录快照版本：申万研究所发布的申万 2021 二级行业指数目录，
2026-11-20 采集。"""

SW_L2_OFFICIAL_DIRECTORY_SOURCE = (
    "申万 2021 二级行业指数官方发布目录快照：申万研究所发布的二级行业指数"
    "全集（行业代码/行业名称/上级行业三列；2026-11-20 经项目既有 AkShare "
    "兼容接口 sw_index_second_info 采集，131 项），作为分类与指数发布目录的"
    "主证据；父行业均为核定的 31 个申万一级行业名称，二级名称无重复。"
    "采集可复现：同一接口在目录变更时会返回新版本，采集时点随版本号记录"
)

SW_L2_OFFICIAL_DIRECTORY: Tuple[Tuple[str, str, str], ...] = (
    ("801011", "林业Ⅱ", "农林牧渔"),
    ("801012", "农产品加工", "农林牧渔"),
    ("801014", "饲料", "农林牧渔"),
    ("801015", "渔业", "农林牧渔"),
    ("801016", "种植业", "农林牧渔"),
    ("801017", "养殖业", "农林牧渔"),
    ("801018", "动物保健Ⅱ", "农林牧渔"),
    ("801019", "农业综合Ⅱ", "农林牧渔"),
    ("801032", "化学纤维", "基础化工"),
    ("801033", "化学原料", "基础化工"),
    ("801034", "化学制品", "基础化工"),
    ("801036", "塑料", "基础化工"),
    ("801037", "橡胶", "基础化工"),
    ("801038", "农化制品", "基础化工"),
    ("801039", "非金属材料Ⅱ", "基础化工"),
    ("801043", "冶钢原料", "钢铁"),
    ("801044", "普钢", "钢铁"),
    ("801045", "特钢Ⅱ", "钢铁"),
    ("801051", "金属新材料", "有色金属"),
    ("801053", "贵金属", "有色金属"),
    ("801054", "小金属", "有色金属"),
    ("801055", "工业金属", "有色金属"),
    ("801056", "能源金属", "有色金属"),
    ("801072", "通用设备", "机械设备"),
    ("801074", "专用设备", "机械设备"),
    ("801076", "轨交设备Ⅱ", "机械设备"),
    ("801077", "工程机械", "机械设备"),
    ("801078", "自动化设备", "机械设备"),
    ("801081", "半导体", "电子"),
    ("801082", "其他电子Ⅱ", "电子"),
    ("801083", "元件", "电子"),
    ("801084", "光学光电子", "电子"),
    ("801085", "消费电子", "电子"),
    ("801086", "电子化学品Ⅱ", "电子"),
    ("801092", "汽车服务", "汽车"),
    ("801093", "汽车零部件", "汽车"),
    ("801095", "乘用车", "汽车"),
    ("801096", "商用车", "汽车"),
    ("801101", "计算机设备", "计算机"),
    ("801102", "通信设备", "通信"),
    ("801103", "IT服务Ⅱ", "计算机"),
    ("801104", "软件开发", "计算机"),
    ("801111", "白色家电", "家用电器"),
    ("801112", "黑色家电", "家用电器"),
    ("801113", "小家电", "家用电器"),
    ("801114", "厨卫电器", "家用电器"),
    ("801115", "照明设备Ⅱ", "家用电器"),
    ("801116", "家电零部件Ⅱ", "家用电器"),
    ("801117", "其他家电Ⅱ", "家用电器"),
    ("801124", "食品加工", "食品饮料"),
    ("801125", "白酒Ⅱ", "食品饮料"),
    ("801126", "非白酒", "食品饮料"),
    ("801127", "饮料乳品", "食品饮料"),
    ("801128", "休闲食品", "食品饮料"),
    ("801129", "调味发酵品Ⅱ", "食品饮料"),
    ("801131", "纺织制造", "纺织服饰"),
    ("801132", "服装家纺", "纺织服饰"),
    ("801133", "饰品", "纺织服饰"),
    ("801141", "包装印刷", "轻工制造"),
    ("801142", "家居用品", "轻工制造"),
    ("801143", "造纸", "轻工制造"),
    ("801145", "文娱用品", "轻工制造"),
    ("801151", "化学制药", "医药生物"),
    ("801152", "生物制品", "医药生物"),
    ("801153", "医疗器械", "医药生物"),
    ("801154", "医药商业", "医药生物"),
    ("801155", "中药Ⅱ", "医药生物"),
    ("801156", "医疗服务", "医药生物"),
    ("801161", "电力", "公用事业"),
    ("801163", "燃气Ⅱ", "公用事业"),
    ("801178", "物流", "交通运输"),
    ("801179", "铁路公路", "交通运输"),
    ("801181", "房地产开发", "房地产"),
    ("801183", "房地产服务", "房地产"),
    ("801191", "多元金融", "非银金融"),
    ("801193", "证券Ⅱ", "非银金融"),
    ("801194", "保险Ⅱ", "非银金融"),
    ("801202", "贸易Ⅱ", "商贸零售"),
    ("801203", "一般零售", "商贸零售"),
    ("801204", "专业连锁Ⅱ", "商贸零售"),
    ("801206", "互联网电商", "商贸零售"),
    ("801207", "旅游零售Ⅱ", "商贸零售"),
    ("801216", "体育Ⅱ", "社会服务"),
    ("801218", "专业服务", "社会服务"),
    ("801219", "酒店餐饮", "社会服务"),
    ("801223", "通信服务", "通信"),
    ("801231", "综合Ⅱ", "综合"),
    ("801711", "水泥", "建筑材料"),
    ("801712", "玻璃玻纤", "建筑材料"),
    ("801713", "装修建材", "建筑材料"),
    ("801721", "房屋建设Ⅱ", "建筑装饰"),
    ("801722", "装修装饰Ⅱ", "建筑装饰"),
    ("801723", "基础建设", "建筑装饰"),
    ("801724", "专业工程", "建筑装饰"),
    ("801726", "工程咨询服务Ⅱ", "建筑装饰"),
    ("801731", "电机Ⅱ", "电力设备"),
    ("801733", "其他电源设备Ⅱ", "电力设备"),
    ("801735", "光伏设备", "电力设备"),
    ("801736", "风电设备", "电力设备"),
    ("801737", "电池", "电力设备"),
    ("801738", "电网设备", "电力设备"),
    ("801741", "航天装备Ⅱ", "国防军工"),
    ("801742", "航空装备Ⅱ", "国防军工"),
    ("801743", "地面兵装Ⅱ", "国防军工"),
    ("801744", "航海装备Ⅱ", "国防军工"),
    ("801745", "军工电子Ⅱ", "国防军工"),
    ("801764", "游戏Ⅱ", "传媒"),
    ("801765", "广告营销", "传媒"),
    ("801766", "影视院线", "传媒"),
    ("801767", "数字媒体", "传媒"),
    ("801769", "出版", "传媒"),
    ("801782", "国有大型银行Ⅱ", "银行"),
    ("801783", "股份制银行Ⅱ", "银行"),
    ("801784", "城商行Ⅱ", "银行"),
    ("801785", "农商行Ⅱ", "银行"),
    ("801881", "摩托车及其他", "汽车"),
    ("801951", "煤炭开采", "煤炭"),
    ("801952", "焦炭Ⅱ", "煤炭"),
    ("801961", "油气开采Ⅱ", "石油石化"),
    ("801962", "油服工程", "石油石化"),
    ("801963", "炼化及贸易", "石油石化"),
    ("801971", "环境治理", "环保"),
    ("801972", "环保设备Ⅱ", "环保"),
    ("801981", "个护用品", "美容护理"),
    ("801982", "化妆品", "美容护理"),
    ("801983", "医疗美容", "美容护理"),
    ("801991", "航空机场", "交通运输"),
    ("801992", "航运港口", "交通运输"),
    ("801993", "旅游及景区", "社会服务"),
    ("801994", "教育", "社会服务"),
    ("801995", "电视广播Ⅱ", "传媒"),
)

# 对账事实（2026-11-20）：官方 131 项中，名称与父行业与下方 iFinD 核验子集
# 完全一致的 124 项；下列 7 项官方分类存在但 iFinD 侧未核验（iFinD 代码区间
# 扫描 + 成分归属反推均未确认，多为当前无成分/低覆盖分类）——不产生 iFinD
# 指数代码映射，目标股归属落在这些分类时按二级缺口如实披露。
SW_L2_UNVERIFIED_BY_IFIND: Tuple[Tuple[str, str, str], ...] = (
    ("801011", "林业Ⅱ", "农林牧渔"),
    ("801019", "农业综合Ⅱ", "农林牧渔"),
    ("801117", "其他家电Ⅱ", "家用电器"),
    ("801207", "旅游零售Ⅱ", "商贸零售"),
    ("801216", "体育Ⅱ", "社会服务"),
    ("801961", "油气开采Ⅱ", "石油石化"),
    ("801983", "医疗美容", "美容护理"),
)

SW_L2_DIRECTORY_VERSION = "sw2021-l2-20261120"
"""核定目录版本：申万 2021 二级行业指数目录（官方发布目录 + iFinD 身份
核验），2026-11-20 核定。"""

SW_L2_DIRECTORY_SOURCE = (
    "申万 2021 二级行业指数目录：以官方发布目录快照为分类主证据"
    f"（{SW_L2_OFFICIAL_DIRECTORY_VERSION}，131 项，"
    "见 SW_L2_OFFICIAL_DIRECTORY_SOURCE），逐一 join iFinD 侧核验——"
    "801001-801999 指数代码区间扫描（2026-11-19，basic_data_service "
    "ths_index_short_name_index 简称回显，31 个一级目录双重比对）+ 同日成分"
    "（p03473）成员 L1/L2 正式归属多数票反推（2026-11-19），124 项与官方"
    "目录的二级正式名称与父行业**逐项完全一致**（无名称/父行业矛盾），"
    "另 7 项官方分类存在但 iFinD 侧未核验（SW_L2_UNVERIFIED_BY_IFIND，"
    "不产生指数代码映射、如实缺口）。父行业与二级正式名称必须能在本目录"
    "唯一映射到已核验的 iFinD 指数代码才采纳，缺失或歧义按缺口处理"
    "（不按名称近似匹配、不用样例白名单替代目录）。验收样本："
    "中国平安→保险Ⅱ（801194.SL）、三一重工→工程机械（801077.SL，"
    "2026-11-19 实测归属；官方目录同项核对一致）"
)

SW_L2_DIRECTORY: Tuple[Tuple[str, str, str], ...] = (
    ("801178.SL", "物流", "交通运输"),
    ("801179.SL", "铁路公路", "交通运输"),
    ("801991.SL", "航空机场", "交通运输"),
    ("801992.SL", "航运港口", "交通运输"),
    ("801764.SL", "游戏Ⅱ", "传媒"),
    ("801765.SL", "广告营销", "传媒"),
    ("801766.SL", "影视院线", "传媒"),
    ("801767.SL", "数字媒体", "传媒"),
    ("801769.SL", "出版", "传媒"),
    ("801995.SL", "电视广播Ⅱ", "传媒"),
    ("801161.SL", "电力", "公用事业"),
    ("801163.SL", "燃气Ⅱ", "公用事业"),
    ("801012.SL", "农产品加工", "农林牧渔"),
    ("801014.SL", "饲料", "农林牧渔"),
    ("801015.SL", "渔业", "农林牧渔"),
    ("801016.SL", "种植业", "农林牧渔"),
    ("801017.SL", "养殖业", "农林牧渔"),
    ("801018.SL", "动物保健Ⅱ", "农林牧渔"),
    ("801151.SL", "化学制药", "医药生物"),
    ("801152.SL", "生物制品", "医药生物"),
    ("801153.SL", "医疗器械", "医药生物"),
    ("801154.SL", "医药商业", "医药生物"),
    ("801155.SL", "中药Ⅱ", "医药生物"),
    ("801156.SL", "医疗服务", "医药生物"),
    ("801202.SL", "贸易Ⅱ", "商贸零售"),
    ("801203.SL", "一般零售", "商贸零售"),
    ("801204.SL", "专业连锁Ⅱ", "商贸零售"),
    ("801206.SL", "互联网电商", "商贸零售"),
    ("801741.SL", "航天装备Ⅱ", "国防军工"),
    ("801742.SL", "航空装备Ⅱ", "国防军工"),
    ("801743.SL", "地面兵装Ⅱ", "国防军工"),
    ("801744.SL", "航海装备Ⅱ", "国防军工"),
    ("801745.SL", "军工电子Ⅱ", "国防军工"),
    ("801032.SL", "化学纤维", "基础化工"),
    ("801033.SL", "化学原料", "基础化工"),
    ("801034.SL", "化学制品", "基础化工"),
    ("801036.SL", "塑料", "基础化工"),
    ("801037.SL", "橡胶", "基础化工"),
    ("801038.SL", "农化制品", "基础化工"),
    ("801039.SL", "非金属材料Ⅱ", "基础化工"),
    ("801111.SL", "白色家电", "家用电器"),
    ("801112.SL", "黑色家电", "家用电器"),
    ("801113.SL", "小家电", "家用电器"),
    ("801114.SL", "厨卫电器", "家用电器"),
    ("801115.SL", "照明设备Ⅱ", "家用电器"),
    ("801116.SL", "家电零部件Ⅱ", "家用电器"),
    ("801711.SL", "水泥", "建筑材料"),
    ("801712.SL", "玻璃玻纤", "建筑材料"),
    ("801713.SL", "装修建材", "建筑材料"),
    ("801721.SL", "房屋建设Ⅱ", "建筑装饰"),
    ("801722.SL", "装修装饰Ⅱ", "建筑装饰"),
    ("801723.SL", "基础建设", "建筑装饰"),
    ("801724.SL", "专业工程", "建筑装饰"),
    ("801726.SL", "工程咨询服务Ⅱ", "建筑装饰"),
    ("801181.SL", "房地产开发", "房地产"),
    ("801183.SL", "房地产服务", "房地产"),
    ("801051.SL", "金属新材料", "有色金属"),
    ("801053.SL", "贵金属", "有色金属"),
    ("801054.SL", "小金属", "有色金属"),
    ("801055.SL", "工业金属", "有色金属"),
    ("801056.SL", "能源金属", "有色金属"),
    ("801072.SL", "通用设备", "机械设备"),
    ("801074.SL", "专用设备", "机械设备"),
    ("801076.SL", "轨交设备Ⅱ", "机械设备"),
    ("801077.SL", "工程机械", "机械设备"),
    ("801078.SL", "自动化设备", "机械设备"),
    ("801092.SL", "汽车服务", "汽车"),
    ("801093.SL", "汽车零部件", "汽车"),
    ("801095.SL", "乘用车", "汽车"),
    ("801096.SL", "商用车", "汽车"),
    ("801881.SL", "摩托车及其他", "汽车"),
    ("801951.SL", "煤炭开采", "煤炭"),
    ("801952.SL", "焦炭Ⅱ", "煤炭"),
    ("801971.SL", "环境治理", "环保"),
    ("801972.SL", "环保设备Ⅱ", "环保"),
    ("801731.SL", "电机Ⅱ", "电力设备"),
    ("801733.SL", "其他电源设备Ⅱ", "电力设备"),
    ("801735.SL", "光伏设备", "电力设备"),
    ("801736.SL", "风电设备", "电力设备"),
    ("801737.SL", "电池", "电力设备"),
    ("801738.SL", "电网设备", "电力设备"),
    ("801081.SL", "半导体", "电子"),
    ("801082.SL", "其他电子Ⅱ", "电子"),
    ("801083.SL", "元件", "电子"),
    ("801084.SL", "光学光电子", "电子"),
    ("801085.SL", "消费电子", "电子"),
    ("801086.SL", "电子化学品Ⅱ", "电子"),
    ("801962.SL", "油服工程", "石油石化"),
    ("801963.SL", "炼化及贸易", "石油石化"),
    ("801218.SL", "专业服务", "社会服务"),
    ("801219.SL", "酒店餐饮", "社会服务"),
    ("801993.SL", "旅游及景区", "社会服务"),
    ("801994.SL", "教育", "社会服务"),
    ("801131.SL", "纺织制造", "纺织服饰"),
    ("801132.SL", "服装家纺", "纺织服饰"),
    ("801133.SL", "饰品", "纺织服饰"),
    ("801231.SL", "综合Ⅱ", "综合"),
    ("801981.SL", "个护用品", "美容护理"),
    ("801982.SL", "化妆品", "美容护理"),
    ("801101.SL", "计算机设备", "计算机"),
    ("801103.SL", "IT服务Ⅱ", "计算机"),
    ("801104.SL", "软件开发", "计算机"),
    ("801141.SL", "包装印刷", "轻工制造"),
    ("801142.SL", "家居用品", "轻工制造"),
    ("801143.SL", "造纸", "轻工制造"),
    ("801145.SL", "文娱用品", "轻工制造"),
    ("801102.SL", "通信设备", "通信"),
    ("801223.SL", "通信服务", "通信"),
    ("801043.SL", "冶钢原料", "钢铁"),
    ("801044.SL", "普钢", "钢铁"),
    ("801045.SL", "特钢Ⅱ", "钢铁"),
    ("801782.SL", "国有大型银行Ⅱ", "银行"),
    ("801783.SL", "股份制银行Ⅱ", "银行"),
    ("801784.SL", "城商行Ⅱ", "银行"),
    ("801785.SL", "农商行Ⅱ", "银行"),
    ("801191.SL", "多元金融", "非银金融"),
    ("801193.SL", "证券Ⅱ", "非银金融"),
    ("801194.SL", "保险Ⅱ", "非银金融"),
    ("801124.SL", "食品加工", "食品饮料"),
    ("801125.SL", "白酒Ⅱ", "食品饮料"),
    ("801126.SL", "非白酒", "食品饮料"),
    ("801127.SL", "饮料乳品", "食品饮料"),
    ("801128.SL", "休闲食品", "食品饮料"),
    ("801129.SL", "调味发酵品Ⅱ", "食品饮料"),
)
"""申万 2021 二级行业指数目录（iFinD 指数代码 → (二级正式名称, 父一级名称)，
124 项——官方发布目录 131 项中已经 iFinD 侧核验的子集）。

依据见 ``SW_L2_DIRECTORY_SOURCE``（官方目录主证据 + iFinD 精确身份核验，
对账事实 2026-11-20）；目录变更须重核官方目录、iFinD 身份与对账结果并
更新版本号。
"""


def _build_l2_parent_name_index(
        entries: Tuple[Tuple[str, str, str], ...],
) -> Tuple[Dict[Tuple[str, str], str], Tuple[Tuple[str, str], ...]]:
    """(父一级, 二级名称) → iFinD 指数代码 + 歧义键集（构建期标记，issue #105 修复）。

    同一 (父行业, 名称) 键出现两个代码即目录数据错误：歧义按契约必须映射
    为 None（缺口），静默覆盖会让 resolve 返回其中之一冒充唯一命中。构建
    期把歧义键**标记为不可解析**（从唯一映射中移除、单独收集），不抛错、
    不阻断模块加载与其他键解析（issue #105 修复：受控歧义只降相应二级
    身份，一级报告与目录其余条目继续可用）；正式目录的发布前数据错误由
    目录唯一性/覆盖测试负责发现（当前核定目录无歧义键）。
    """
    index: Dict[Tuple[str, str], str] = {}
    ambiguous: set = set()
    for code, name, parent in entries:
        key = (parent, name)
        if key in index or key in ambiguous:
            ambiguous.add(key)
            index.pop(key, None)  # 歧义键不可解析（映射 None 语义）
            continue
        index[key] = code
    return index, tuple(sorted(ambiguous))


_L2_BY_PARENT_AND_NAME, _L2_AMBIGUOUS_KEYS = (
    _build_l2_parent_name_index(SW_L2_DIRECTORY))


def sw_l2_key_ambiguous(l1_name: str, l2_name: str) -> bool:
    """(父一级, 二级名称) 是否为核定目录构建期标记的歧义键（issue #105 修复）。

    歧义键在目录构建期已从唯一映射移除（:func:`_build_l2_parent_name_index`），
    :func:`resolve_sw_l2_index` 对其恒返回 None；本函数供调用方在二级身份
    缺口处区分「歧义不可解析」与「未核验/未知名称」，只降相应二级身份、
    不泛化目录系统。
    """
    if not l1_name or not l2_name:
        return False
    return (l1_name.strip(), l2_name.strip()) in set(_L2_AMBIGUOUS_KEYS)


def resolve_sw_l2_index(l1_name: str, l2_name: str) -> Optional[str]:
    """父一级 + 二级名称 → 已核验的 iFinD 二级指数代码（唯一映射；issue #103）。

    仅当 (父一级名称, 二级名称) 能在核定目录中**唯一**命中且该条目已有
    iFinD 侧核验身份时返回代码；父行业不符、名称未知、歧义（构建期标记
    为不可解析，见 ``sw_l2_key_ambiguous``）或仅官方分类存在但 iFinD 未
    核验（见 ``SW_L2_UNVERIFIED_BY_IFIND``）一律返回 None（调用方按二级
    缺口处理，不按名称近似匹配）。
    """
    if not l1_name or not l2_name:
        return None
    return _L2_BY_PARENT_AND_NAME.get((l1_name.strip(), l2_name.strip()))


# ---------------------------------------------------------------------------
# 配置门控（板块入口独立判断，不读日线开关/profile）
# ---------------------------------------------------------------------------


def sw_http_config() -> Optional[Tuple[str, float]]:
    """板块 iFinD 标准 HTTP 入口配置： ``(refresh_token, timeout_seconds)``。

    未配置凭据（缺失/空白）时返回 None——申万主行业按不可确认处理
    （父规格决策 1），不抛异常、不产出站。
    """
    from src.config import get_config

    config = get_config()
    token = (config.ifind_http_refresh_token or "").strip()
    if not token:
        return None
    timeout = float(getattr(config, "ifind_http_timeout_seconds", 30.0) or 0.0)
    if timeout <= 0:
        timeout = 30.0
    return token, timeout


# ---------------------------------------------------------------------------
# 共享传输出站（行为等价复用；单次超时取小见模块 docstring）
# ---------------------------------------------------------------------------


def _bounded_deadline(deadline: Optional[float], timeout_seconds: float) -> float:
    """本次出站的绝对截止：调用方截止与单次请求超时取小（monotonic）。"""
    single = time.monotonic() + max(timeout_seconds, 0.001)
    if deadline is None:
        return single
    return min(single, deadline)


def _sector_http_post(endpoint_url: str, body: Dict[str, Any], *, label: str,
                      refresh_token: str, timeout_seconds: float,
                      deadline: Optional[float],
                      parse_float: Optional[Callable[[str], Optional[Decimal]]] = None,
                      ) -> Dict[str, Any]:
    """板块共用出站：认证 → 有界 curl POST → envelope/errorcode/tables 边界。

    认证缓存沿用共享键/刷新/锁语义；本次出站截止为
    ``min(deadline, now + timeout_seconds)``。失败统一抛
    :class:`SwSectorFetchError`；全部消息**先凭据脱敏后截断**（refresh token
    与本次实际使用的 access token 一律不出现，跨截断边界的长回显同样被
    脱敏——复用既有适配器 ``_redact_token`` 安全语义）；errorcode 非标量时
    按类型分类不入消息内容（防恶意长回显）。
    ``parse_float``（可选）仅作用于**响应** JSON 的浮点字面解析（如实时行情
    传 ``Decimal`` 保留入站字面精度供金融交叉复核）；不传时响应解码与既有
    行为逐位一致（归属/成分/日线等其他端点不受影响）。
    """
    from src.patches.eastmoney_patch import request_deadline

    _MESSAGE_CAP = 160

    def _safe(message: str, access_used: Optional[str]) -> str:
        # 凭据脱敏下界：真实凭据均为长串，过短占位值（<8）不做全文替换，
        # 避免病态短值把供应商业务文本（如 errmsg）替换成掩码；
        # 先脱敏后截断——跨截断边界（80/120 等）的长回显先被整体脱敏
        secrets: Tuple[str, ...] = tuple(
            s for s in ((refresh_token or "").strip(), (access_used or "").strip())
            if len(s) >= 8)
        redacted = _redact_token(message, *secrets) if secrets else message
        return redacted[:_MESSAGE_CAP]

    def _errorcode_repr(errorcode: object) -> str:
        # 标量 code 保留有界字面；非标量（dict/list 等）只暴露类型名，
        # 不把潜在恶意/超长内容回显进缺口文本
        if isinstance(errorcode, bool):
            return f"<bool:{errorcode}>"
        if isinstance(errorcode, (int, float)):
            return repr(errorcode)
        if isinstance(errorcode, str):
            return errorcode[:20]
        return f"<{type(errorcode).__name__}>"

    def _post_once(access_token: str) -> Tuple[int, bytes]:
        return _http_api_curl_post(
            endpoint_url,
            {"Content-Type": "application/json", "access_token": access_token,
             "ifindlang": "cn"},
            json.dumps(body).encode(), label=label,
        )

    effective = _bounded_deadline(deadline, timeout_seconds)
    guard = request_deadline.set(effective)
    access_used: Optional[str] = None
    try:
        access_used = _resolve_access_token(refresh_token)
        status, raw = _post_once(access_used)
        if status in (401, 403):
            # 凭据被供应商拒绝：与共享凭据路径
            # （``_post_history_query_authorized``）一致——强制续用一次并重试
            # **同一请求一次**（不改变 refresh_token）；同一 guard 内完成，
            # 刷新与重试同受原每请求剩余截止钳制（有界，不另起预算）
            access_used = _resolve_access_token(refresh_token, force_refresh=True)
            status, raw = _post_once(access_used)
        if status in (401, 403):
            raise SwSectorFetchError(_safe(
                "ifind sector authentication failed after token refresh; "
                "请检查 IFIND_HTTP_REFRESH_TOKEN 有效性与账号权限", access_used))
        if status != 200:
            raise SwSectorFetchError(_safe(f"HTTP {status}", access_used))
        try:
            payload = (json.loads(raw, parse_float=parse_float)
                       if parse_float is not None else json.loads(raw))
        except (ValueError, UnicodeDecodeError):
            raise SwSectorFetchError(_safe("响应不是有效 JSON", access_used)) from None
        if not isinstance(payload, dict):
            raise SwSectorFetchError(_safe("响应顶层形状异常（非对象）", access_used))
        errorcode = payload.get("errorcode")
        if errorcode != 0:
            errmsg = payload.get("errmsg")
            errmsg_text = errmsg if isinstance(errmsg, str) else ""
            raise SwSectorFetchError(_safe(
                f"errorcode={_errorcode_repr(errorcode)} errmsg={errmsg_text}",
                access_used))
        if not isinstance(payload.get("tables"), list):
            raise SwSectorFetchError(_safe("响应缺 tables（形状异常）", access_used))
        return payload
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - 传输/认证边界统一降级
        raise SwSectorFetchError(_safe(
            f"{type(exc).__name__}: {str(exc)}", access_used)) from None
    finally:
        request_deadline.reset(guard)


# ---------------------------------------------------------------------------
# 主行业归属（basic_data_service，层级单独查询——同批重复指标会覆盖结果）
# ---------------------------------------------------------------------------

_SW_INDUSTRY_INDICATOR = "ths_the_sw_industry_stock"


def _fetch_sw_industry_name(secucode: str, level: int, observation_date: str, *,
                            refresh_token: str, timeout_seconds: float,
                            deadline: Optional[float]) -> str:
    """指定层级的正式申万行业归属名称（严格身份与类型边界）。

    - **精确身份**：只采用 ``thscode`` 与请求代码完全一致的响应行（错误
      代码的行不采用，不取首行）；
    - **类型边界**：值必须是**非空字符串且 ≤64 字符**（行业名边界随载荷
      schema）——非字符串/空白/超长值按源错误拒绝，不做 ``str()`` 合法化；
    - **同码冲突**：多行同码给出不同有效值 → 冲突，不取其一。
    无匹配行/值非法/冲突 → :class:`SwSectorFetchError`（调用方按层级降级：
    L1 主行业不可确认、L2/L3 可选参考缺口——永不产生 API 500）。
    """
    body = {
        "codes": secucode,
        "indipara": [{
            "indicator": _SW_INDUSTRY_INDICATOR,
            "indiparams": [str(level), observation_date.replace("-", "")],
        }],
    }
    payload = _sector_http_post(
        _BASIC_DATA_ENDPOINT, body, label="sector_sw_industry",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise SwSectorFetchError(
            f"申万归属响应形状异常（tables 非 list，L{level}）")
    valid_names: List[str] = []
    matched = False
    for table in tables:
        if not isinstance(table, dict):
            continue
        if str(table.get("thscode") or "").strip() != secucode:
            continue  # 非本请求代码的响应行：不采用（精确身份，不取首行）
        matched = True
        cells = table.get("table") if isinstance(table.get("table"), dict) else {}
        values = cells.get(_SW_INDUSTRY_INDICATOR)
        if not isinstance(values, list) or not values:
            continue
        # 同表多值同样歧义：逐值校验后取本表去重结果，多个不同行业名 → 冲突
        # （不取首个——同表重复指标覆盖/多值都是供应商形状异常）
        names: List[str] = []
        for raw in values:
            if not isinstance(raw, str):
                raise SwSectorFetchError(
                    f"申万归属响应值非法（L{level} {secucode}：非字符串值，"
                    "不 str() 合法化）")
            name = raw.strip()
            if not name or len(name) > 64:
                raise SwSectorFetchError(
                    f"申万归属响应值非法（L{level} {secucode}：空白或超 64 字符行业名）")
            if name not in names:
                names.append(name)
        if len(names) > 1:
            raise SwSectorFetchError(
                f"申万归属响应同表多值冲突（L{level} {secucode}："
                f"{len(names)} 个不同行业名），不取其一")
        if names and names[0] not in valid_names:
            valid_names.append(names[0])
    if not matched:
        raise SwSectorFetchError(
            f"申万归属响应无精确匹配行（L{level} {secucode}）")
    if len(valid_names) > 1:
        # 冲突消息只含计数，不含供应商返回值（防回显）
        raise SwSectorFetchError(
            f"申万归属响应同码多表冲突（L{level} {secucode}："
            f"{len(valid_names)} 个不同行业名），不取其一")
    if not valid_names:
        raise SwSectorFetchError(
            f"申万归属响应无有效值（L{level} {secucode}）")
    return valid_names[0]


def resolve_sw_main_industry(secucode: str, *, observation_date: str,
                             refresh_token: str, timeout_seconds: float,
                             deadline: Optional[float]) -> Dict[str, Any]:
    """个股申万主行业归属解析（决策 2：单一主行业口径；L1 核心单一出站）。

    一级归属（核心）：与核定目录唯一名称匹配（不近似择首、不拼同名东财
    板块）。**本函数不查询二/三级参考**——可选参考出站延迟到核心事实
    （快照/趋势）、口径选择与必需研究完成之后，由
    :func:`resolve_sw_reference_levels` 在真正剩余取数预算内有界执行
    （spec 复审：一级很快、二级等待耗尽截止时不得让快照/趋势/兜底失去
    机会；东财切换路径不发起参考出站）。``l2_name``/``l3_name`` 恒为
    None，由调用方在参考补齐后回填。
    供应商不回显归属日期，``vendor_as_of`` 为本次请求的归属观察日（查询
    口径如实标注）。失败/未命中 → ``status="unavailable"`` + 可诊断缺口
    （供兜底门槛判定）。
    """
    out: Dict[str, Any] = {
        "status": "unavailable",
        "classification_system": None,
        "classification_level": None,
        "classification_version": None,
        "index_code": None,
        "index_name": None,
        "vendor_as_of": None,
        "l2_name": None,
        "l3_name": None,
        "gaps": [],
    }
    gaps: List[str] = []
    # 核心：一级归属（决定主行业口径与兜底门槛；失败即返回，无参考出站）
    try:
        l1_name = _fetch_sw_industry_name(
            secucode, 1, observation_date,
            refresh_token=refresh_token, timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
    except SwSectorFetchError as exc:
        gaps.append(f"申万一级归属查询失败（{exc}），主行业不可确认")
        out["gaps"] = gaps
        return out
    if not l1_name:
        gaps.append("申万一级归属未取得（供应商无值），主行业不可确认")
        out["gaps"] = gaps
        return out
    index_code = _DIRECTORY_BY_NAME.get(l1_name)
    if index_code is None:
        gaps.append(
            f"申万一级归属「{l1_name}」未命中核定一级目录（不近似择首、"
            "不拼入同名东财板块），主行业不可确认")
        out["gaps"] = gaps
        return out
    out.update({
        "status": "confirmed",
        "classification_system": "申万 2021 一级行业",
        "classification_level": "L1",
        "classification_version": SW_L1_DIRECTORY_VERSION,
        "index_code": index_code,
        "index_name": l1_name,
        "vendor_as_of": observation_date,
        "l2_name": None,
        "l3_name": None,
        "gaps": gaps,
    })
    return out


def resolve_sw_reference_levels(secucode: str, *, observation_date: str,
                                refresh_token: str, timeout_seconds: float,
                                deadline: Optional[float]) -> Dict[str, Any]:
    """申万二/三级归属参考（可选链；L1 确认后的余量出站）。

    层级单独查询（同批重复指标会覆盖结果）与既有失败隔离语义不变：单级
    失败仅记该级可选缺口，不影响另一级与主行业状态。调用方负责预算门控
    （取数截止后不发起；东财切换路径不发起）与结果回填
    （``l2_name``/``l3_name`` 回填进主行业归属事实，缺口随身份缺口透出）。
    返回 ``{"l2_name", "l3_name", "gaps"}``；未取得的层级为 None。
    """
    gaps: List[str] = []
    l2_name: Optional[str] = None
    l3_name: Optional[str] = None
    for level in (2, 3):
        label = "二级" if level == 2 else "三级"
        try:
            name = _fetch_sw_industry_name(
                secucode, level, observation_date,
                refresh_token=refresh_token, timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        except SwSectorFetchError as exc:
            gaps.append(
                f"申万{label}归属参考查询失败（可选参考，不影响主行业）：{exc}")
            continue
        if level == 2:
            l2_name = name
        else:
            l3_name = name
    return {"l2_name": l2_name, "l3_name": l3_name, "gaps": gaps}


# ---------------------------------------------------------------------------
# 一级指数快照（real_time_quotation）
# ---------------------------------------------------------------------------


def fetch_index_quotes(codes: str, *, refresh_token: str, timeout_seconds: float,
                       deadline: Optional[float],
                       indicators: str = ("changeRatio,amount,tradeTime,"
                                          "latest,preClose,turnoverRatio,"
                                          "riseCount,fallCount"),
                       raw_amount: bool = False,
                       ) -> List[Dict[str, Any]]:
    """指数/证券实时快照 wire 解析：每行一个 thscode 的归一事实 dict。

    行字段：``thscode``/``trade_date``/``trade_time``/``stamp``（完整供应
    商时间戳 datetime，新鲜度资格依据）/``change_pct``/``latest``/
    ``pre_close``/``amount``/``turnover``/``up_count``/``down_count``。
    ``trade_time`` 自**已解析合格的完整供应商时间戳**归一（``HH:MM:SS``）：
    wire 的辅助 ``tradeTime`` 单元格是任意文本，不作为行事实或下游来源
    文字（畸形/超长辅助值既不进快照行，也不进 ``snapshot_source`` 等边界
    字段）。
    缺值（None）原样保留，不在 wire 层补造；跨行资格（同日、时间/新鲜度、
    交叉复核、同码重复冲突）由 :func:`qualify_quote_rows` 判定。
    畸形 wire 形状（非 dict 行/缺键/不可解析时间戳）逐行保守跳过，不让
    供应商形状异常逃出边界（整体无有效行按源错误抛 :class:`SwSectorFetchError`，
    由调用方降缺口，不产生未处理 500）。
    ``indicators`` 默认为 31 指数快照口径（issue #99）；成分成员行情
    （issue #100）传成员口径子集（与 2026-11-18 实测一致），其余行为不变。
    ``raw_amount=True``（成分成员行情路径）：``amount`` 原样保留首个有限
    整数或 Decimal（不经 float cast，供成交额排序保原始精度）；默认路径
    （31 指数快照与既有调用方）行为不变。

    金融精度（实时行情端点响应按 ``Decimal`` 解析浮点字面）：``change_pct``/
    ``latest``/``pre_close`` 在行内**原样保留精确数值**（int/float/Decimal，
    不经 float cast）贯穿 :func:`qualify_quote_rows` 交叉复核——字面
    ``0.495000000000000001`` 与 ``0.495`` 在资格判定下可区分（前者对
    100.5/100 推算 0.5pp 差 0.00499… 在容差内，后者恰 0.005pp 拒绝）；
    公开展示投影（float 字段）由消费方 ``_num``/``float`` 边界转换（JSON
    安全）。其余数值位（换手/涨跌家数与非 raw 金额）维持 float 口径。
    """
    body = {
        "codes": codes,
        "indicators": indicators,
    }
    payload = _sector_http_post(
        _REALTIME_ENDPOINT, body, label="sector_sw_quotes",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline, parse_float=_parse_quote_number,
    )
    try:
        rows: List[Dict[str, Any]] = []
        for table in payload.get("tables") or []:
            if not isinstance(table, dict):
                continue
            thscode = str(table.get("thscode") or "").strip()
            if not thscode:
                continue
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            raw_times = table.get("time")
            times = raw_times if isinstance(raw_times, list) else []
            trade_stamp = str(times[0]) if times else ""
            trade_date = trade_stamp.split(" ")[0] if trade_stamp else ""
            # trade_time 归一自已解析的完整供应商时间戳（不取辅助 tradeTime）
            stamp = _parse_vendor_stamp(trade_stamp)
            rows.append({
                "thscode": thscode,
                "trade_date": trade_date,
                "trade_time": stamp.strftime("%H:%M:%S") if stamp else "",
                "stamp": stamp,
                # 交叉复核输入保留精确字面（Decimal 语义见 docstring）
                "change_pct": _first_finite_raw(cells.get("changeRatio")),
                "latest": _first_finite_raw(cells.get("latest")),
                "pre_close": _first_finite_raw(cells.get("preClose")),
                "amount": (_first_finite_raw(cells.get("amount"))
                           if raw_amount
                           else _first_finite(cells.get("amount"))),
                "turnover": _first_finite(cells.get("turnoverRatio")),
                "up_count": _first_finite(cells.get("riseCount")),
                "down_count": _first_finite(cells.get("fallCount")),
            })
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"快照响应形状异常（{type(exc).__name__}）") from None
    if not rows:
        raise SwSectorFetchError("快照响应无有效行（tables 为空）")
    return rows


def _parse_vendor_stamp(raw: str) -> Optional[datetime]:
    """完整供应商时间戳（``YYYY-MM-DD HH:MM:SS``）→ naive datetime。

    不可解析/缺失返回 None（该行时间资格无法验证，按保守拒绝处理，
    不猜测时区或用请求时刻顶替）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _first_finite_raw(values: object) -> Optional[Union[int, float, Decimal]]:
    """wire 数值位**原样**首个有限数值（共享数值位解析唯一入口：不做
    cast，Decimal 字面/大整数保留原始精度供金融复核与排序）。输入如实
    类型化为 ``object``（wire 单元格是未知 JSON 值），输出收窄为
    ``int | float | Decimal | None``。畸形输入本地守卫，不抛出、不让单行
    字段异常拖垮同批健康行：
    - 非 list 容器（dict/str/标量等 malformed wire）→ 缺值（不取 ``[0]``
      顶替、不按容器真值猜测）；
    - 布尔永不作数值；非 int/float/Decimal 元素 → 缺值；
    - 浮点 NaN/±inf 与非有限 Decimal → 缺值；原始大整数/大 Decimal
      **保留**（float 口径由 :func:`_first_finite` 另行守卫溢出与有限性）。
    """
    if not isinstance(values, list) or not values:
        return None
    v = values[0]
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, Decimal) and not v.is_finite():
        return None
    return v


def _first_finite(values: object) -> Optional[float]:
    """wire 数值位 float 口径（公开展示投影/31 指数快照非精确字段）：委托
    共享解析器后转 float；原始值超出 float 可表示范围或转换结果非有限
    （NaN/±inf，含大 Decimal 转 inf）按缺值处理（异常本地消化，不逃出
    边界拖垮同批健康行）。"""
    v = _first_finite_raw(values)
    if v is None:
        return None
    try:
        finite = float(v)
    except (OverflowError, ValueError, TypeError):
        return None
    return finite if math.isfinite(finite) else None


@ensure(
    lambda result: all(_change_ratio_within_tolerance(row)
                       for row in result[0].values()),
    "qualified rows carrying change_pct must agree with the exact implied "
    "latest/preClose change within the 0.005pp tolerance (financial "
    "cross-field invariant at the quote qualification boundary)",
)
def qualify_quote_rows(rows: List[Dict[str, Any]], *, session_date: str,
                       received_at: Optional[datetime] = None,
                       session_open: Optional[datetime] = None,
                       session_close: Optional[datetime] = None,
                       require_change: bool = True,
                       ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """快照行资格筛选：身份/会话/时间戳纪律 + 字段资格 + 重复保守拒绝。

    资格规则（沿用既有报价采纳纪律，见板块文档「统一报价交易时点资格」）：
    - **同码重复保守拒绝**：同一 thscode 出现多行时，逐字段完全一致才保留
      一行；任何矛盾（不同日期/数值/时间）→ 该代码整体不采用（供应商对
      同一身份给出冲突事实，不可信），不取最后一行、不取首行。
    - ``trade_date == session_date``（其他日期行不进入该日截面）；
    - **时间/新鲜度**（``received_at`` 为该响应实际接收钟，市场本地 naive）：
      完整供应商时间戳必须可解析、**不晚于接收时刻**（未来行情拒绝）；
      该会话已完结（接收 ≥ ``session_close``）→ 时间戳须 ≥ 会话收盘
      （盘后推送如 22:00:46 合法；已完结会话的上午盘中旧快照拒绝）；
      盘中（接收 < ``session_close``）→ 时间戳须 ≥ 会话开盘。会话开收盘
      未提供时仅要求不晚于接收时刻（保守下界不省略上界验证）。
    - **字段资格**（身份/会话/时间戳之外，按口径拆分）：``change_pct``
      与 ``latest/preClose`` 的精确推算值差异须在容差内（缺
      latest/preClose 时不发明复核基准；Decimal 入站保留字面
      精度，容差边界反例 100.5/100 + 0.495 恰差 0.005pp 拒绝、
      0.495000000000000001 在容差内采纳）。``require_change=True``
      （31 指数快照等既有口径，默认）：涨跌幅缺失**或交叉复核不合格**
      的行整体不合格——目标日行情资格与 ``quote_target_ok`` 依赖有效
      涨跌幅。``require_change=False``（成分成员/目标独立行情口径）：
      涨跌幅缺失或复核不合格**不剔除整行**——身份/会话/时间戳合格的行
      保留有效金额用于选样（amount-only 成员仍按成交额参与前五）；
      复核不合格的涨跌幅在**副本上置 None**（不污染调用方原始行），涨
      跌幅名次由同业支柱对缺失同行 withhold 并给覆盖说明。
    - 返回 ``({thscode: 行}, 保守拒绝说明)``：不合格行剔除（调用方按全集
      对齐缺口，剔除事实不丢失）；说明仅记录重复冲突等需显式诊断的拒绝，
      调用方负责有界化。
    """
    # 同码重复保守拒绝：逐字段完全一致才去重，矛盾即整码不采用
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("thscode") or "").strip()
        if not code:
            continue
        by_code.setdefault(code, []).append(row)
    notes: List[str] = []
    deduped: List[Dict[str, Any]] = []
    for code, code_rows in by_code.items():
        if len(code_rows) == 1:
            deduped.append(code_rows[0])
            continue
        keys = ("trade_date", "trade_time", "stamp", "change_pct", "latest",
                "pre_close", "amount", "turnover", "up_count", "down_count")
        first = code_rows[0]
        if all(all(r.get(k) == first.get(k) for k in keys) for r in code_rows[1:]):
            deduped.append(first)
        else:
            notes.append(
                f"快照同码重复行冲突（{code}，{len(code_rows)} 行事实不一致），"
                "该代码行情保守拒绝（不取首行/末行）")
    qualified: Dict[str, Dict[str, Any]] = {}
    for row in deduped:
        if row.get("trade_date") != session_date:
            continue
        change = row.get("change_pct")
        if require_change and change is None:
            continue
        stamp = row.get("stamp")
        if received_at is not None:
            if stamp is None:
                continue  # 时间戳不可解析：新鲜度无法验证，保守拒绝
            if stamp > received_at:
                continue  # 未来行情
            if session_close is not None and received_at >= session_close:
                if session_close > stamp:
                    continue  # 已完结会话的盘前/盘中旧快照（未过收盘）
            elif session_open is not None and stamp < session_open:
                continue  # 盘中：当日开盘前的时间戳
        if change is not None and not _change_ratio_within_tolerance(row):
            # 涨跌幅与 latest/preClose 交叉复核不合格（唯一容差谓词）：
            # 严格口径（31 指数快照）整行拒绝；成分口径只作废涨跌幅本身
            # ——副本上置 None（不污染调用方复用的原始行，如参考日二筛），
            # 有效金额保留参与选样，涨跌幅名次由同业支柱 withhold
            if require_change:
                continue
            row = dict(row)
            row["change_pct"] = None
        qualified[str(row.get("thscode") or "")] = row
    return qualified, notes


# ---------------------------------------------------------------------------
# 指数成分（data_pool 专题报表 p03473；issue #100，父规格 #98 决策 3）
# ---------------------------------------------------------------------------


_VALID_MEMBER_SECUCODE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
"""入站成分成员身份格式：6 位数字 + 沪/深/北交易所后缀（不猜交易所）。"""

_MAX_PUBLISHABLE_MEMBER_ROWS = 10000
"""成分成员集合的可发布行数上限（与载荷 schema 的 member_count/snapshot_rows
上界一致）：供应商响应超过该上限属异常形状，成员集合整体不发布（明确有界
源缺口，不发布全集覆盖、不产生载荷校验 500）。"""


def _is_valid_a_share_secucode(code_text: str) -> bool:
    """入站成员代码必须形如 ``NNNNNN.SH|.SZ|.BJ``（6 位数字，精确后缀）。"""
    return bool(_VALID_MEMBER_SECUCODE.fullmatch(code_text or ""))


def fetch_index_members(index_code: str, observation_date: str, *,
                        refresh_token: str, timeout_seconds: float,
                        deadline: Optional[float],
                        full_constituent_audit: bool = False,
                        ) -> Dict[str, Any]:
    """申万指数成分 wire 解析（专题报表 p03473，``iv_date`` + ``iv_zsdm``）。

    ``full_constituent_audit``（issue #105 修复）：**二级**全量资格链显式
    启用的发布前审计——表级回显身份绑定核验、同码重复名称矛盾置空、
    非对象表项形状审计计数。**一级**消费语义（默认 ``False``）与基线
    （issue #100 时期）行为等价：不核验表级回显（p03473 本无表级回显，
    出现异常回显表不改变一级既有消费）、同码重复行保留首个非空可信名
    （不置空、不审计拒绝）、非对象表项按既有语义跳过（不计形状缺口）。
    一级旧键契约（``member_codes``/``member_names``/``gaps`` 语义）不变；
    两级共用的既有防护不受开关影响：六位码跨交易所矛盾整组剔除、异日/
    空日期行剔除、身份格式不合格剔除、超可发布上限整体不发布（安全可
    发布金额与正额下溢防护在行情/支柱层，与本开关无关）。

    返回：``member_codes``（与观察日**同日**的成员代码，保留供应商带回的
    **交易所后缀**——带交易所证券身份匹配的依据）、``member_names``、
    ``dates_seen``（响应中出现的全部行日期）、``rows_total``（原始行数）、
    ``completeness_evidence``（p03473 响应无总数/分页元数据，供应商单次
    响应不能证明全集——恒 None，完整性判定交给 :func:`classify_membership`）、
    ``rejected_identity_rows``（身份格式不合格被剔除的行数：非 6 位数字、缺
    后缀或后缀非沪/深/北；结构化计数，完整性证明的分母依据）、
    ``conflicting_code_members``（剥后缀后同 6 位码跨交易所矛盾被整组剔除的
    成员数）、``blank_date_rows``/``off_date_rows``（空日期/异日行的结构化
    计数，issue #104——全量资格要求**原始行先校验后过滤**，过滤后数量吻合
    但存在脏行时调用方依计数拒绝）、``identity_echo_conflict``（仅审计模式
    核验：响应表回显的指数身份与请求绑定指数冲突时 True——p03473 当前无
    表级回显，出现且冲突即身份绑定不可信）、``shape_gap_count``（仅审计
    模式计非对象表项；缺列/不对齐形状缺口两级照记）、``gaps``。行级同日
    筛选：行日期 ≠ 观察日的行不进入成员集合（异日成分不与目标日截面混
    用）；全部异日 = 未取得该日成分（非空成分，如实缺口）。身份校验：
    入站成员代码必须是 ``6 位数字 + 有效交易所后缀``（.SH/.SZ/.BJ），不合格
    行不采用且缺口**只含计数、不回显原始畸形标识**；剥后缀后同 6 位码出现
    多个交易所后缀 = 身份矛盾，整组剔除（保守，不取其一）。失败抛
    :class:`SwSectorFetchError`，由调用方降缺口（成分查询失败 ≠ 非成员）。
    """
    body = {
        "reportname": "p03473",
        "functionpara": {
            "iv_date": observation_date.replace("-", ""),
            "iv_zsdm": index_code,
        },
        "outputpara": "p03473_f001,p03473_f002,p03473_f003",
    }
    payload = _sector_http_post(
        _DATA_POOL_ENDPOINT, body, label="sector_sw_members",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    try:
        raw_tables = payload.get("tables")
        tables = raw_tables if isinstance(raw_tables, list) else []
        codes: List[str] = []
        names: Dict[str, str] = {}
        dates_seen: List[str] = []
        off_dates: List[str] = []
        rejected_identity_rows = 0
        blank_date_rows = 0
        off_date_rows = 0
        stripped_seen: Dict[str, str] = {}
        conflicting_names: set = set()
        conflicting_name_rows = 0
        conflicting_stripped: set = set()
        conflicting_members = 0
        shape_gaps: List[str] = []
        rows_total = 0
        echo_conflict = False
        echoed_identities: List[str] = []
        # 表级审计先于行处理（两阶段）：审计模式（二级全量资格链）下形状
        # 异常表显式计数（不静默忽略），回显与请求绑定指数冲突的表**整表
        # 不采用**——其行既不进成员集合、也不能作为目标成员身份证据（原始
        # 行数保留在审计计数内）；一级消费模式（基线语义）非对象表项按既有
        # 语义跳过、不核验回显（p03473 本无表级回显，异常回显表不改变一级
        # 既有消费），缺列/不对齐形状缺口两级照记（基线即有）
        usable_tables: List[Dict[str, Any]] = []
        echo_conflicted_tables: List[Dict[str, Any]] = []
        for table in tables:
            if not isinstance(table, dict):
                if full_constituent_audit:
                    shape_gaps.append(
                        "成分响应含非对象表项（表形状异常，该表不采用）")
                continue
            if full_constituent_audit:
                echo = str(table.get("thscode") or "").strip()
                if echo and echo != index_code:
                    echo_conflict = True
                    echo_conflicted_tables.append(table)
                    continue
                if echo and echo not in echoed_identities:
                    echoed_identities.append(echo)
            usable_tables.append(table)
        # 冲突回显表：原始行数计入审计计数（分母如实），行不进入成员集合
        for table in echo_conflicted_tables:
            cells = (table.get("table")
                     if isinstance(table.get("table"), dict) else {})
            row_codes = cells.get("p03473_f002")
            if isinstance(row_codes, list):
                rows_total += len(row_codes)
        for table in usable_tables:
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            row_dates = cells.get("p03473_f001")
            row_codes = cells.get("p03473_f002")
            row_names = cells.get("p03473_f003")
            if not isinstance(row_codes, list):
                shape_gaps.append("成分响应缺成员代码列（p03473_f002），该表不采用")
                continue
            if not (isinstance(row_dates, list) and isinstance(row_names, list)
                    and len(row_dates) == len(row_codes)
                    and len(row_names) == len(row_codes)):
                shape_gaps.append(
                    "成分响应列形状异常（p03473 三列不对齐），该表不采用（不按位置猜值）")
                continue
            rows_total += len(row_codes)
            for raw_date, raw_code, raw_name in zip(row_dates, row_codes,
                                                    row_names):
                date_text = str(raw_date or "").strip()
                if date_text and date_text not in dates_seen:
                    dates_seen.append(date_text)
                if date_text != observation_date:
                    if date_text:
                        off_date_rows += 1
                        if date_text not in off_dates:
                            off_dates.append(date_text)
                    else:
                        blank_date_rows += 1
                    continue
                code_text = str(raw_code or "").strip()
                if not _is_valid_a_share_secucode(code_text):
                    rejected_identity_rows += 1
                    continue
                stripped = code_text.split(".", 1)[0]
                prior = stripped_seen.get(stripped)
                if prior is not None and prior != code_text:
                    # 同 6 位码跨交易所矛盾：整组剔除（保守，不取其一）
                    if stripped not in conflicting_stripped:
                        conflicting_stripped.add(stripped)
                        conflicting_members += 1  # 先到成员随后回滚
                    conflicting_members += 1
                    continue
                stripped_seen.setdefault(stripped, code_text)
                # 同码重复行名称（issue #105 修复二轮）：审计模式（二级）以
                # **首个非空可信名**为审计基线——与基线不同的非空行 → 矛盾
                # 计数（全量资格拒绝依据），且矛盾成员的名称置空——任意择一
                # 名都不诚实，不发布（按代码显示）；一致重复正常去重；「首
                # 空白 + 单一非空」照常采纳（blank→one name，不误报矛盾）。
                # 一级消费模式恢复基线语义：名称只在**首次出现**时采纳
                # （首行空白 → 该成员名称保持缺失，不回填后续行的名称；
                # 名称展示事实，非成员资格依据，矛盾不做审计）。
                name_text = (raw_name.strip()
                             if isinstance(raw_name, str) else "")
                if full_constituent_audit:
                    if name_text and code_text not in conflicting_names:
                        prior_name = names.get(code_text)
                        if prior_name and prior_name != name_text:
                            conflicting_names.add(code_text)
                            conflicting_name_rows += 1
                            names[code_text] = ""
                        elif not prior_name:
                            names[code_text] = name_text
                if code_text not in codes:
                    codes.append(code_text)
                    if name_text:
                        names[code_text] = name_text
                elif full_constituent_audit and name_text \
                        and not names.get(code_text) \
                        and code_text not in conflicting_names:
                    # 「首空白 + 单一非空」补名仅审计模式（blank→one name）；
                    # 一级保持基线：不补名（names 缺失 → 支柱按代码显示）
                    names[code_text] = name_text
        # 冲突组整组回滚（先到成员先回退，保证分母如实）
        if conflicting_stripped:
            codes = [c for c in codes
                     if c.split(".", 1)[0] not in conflicting_stripped]
            names = {c: n for c, n in names.items()
                     if c.split(".", 1)[0] not in conflicting_stripped}
        gaps: List[str] = list(shape_gaps)
        if rejected_identity_rows:
            gaps.append(
                f"成分响应 {rejected_identity_rows} 行成员身份格式不合格"
                "（非 6 位数字或交易所后缀无效，计数披露、不回显原始值），"
                "不采用")
        if conflicting_stripped:
            shown = "、".join(sorted(conflicting_stripped)[:3])
            more = (f" 等 {len(conflicting_stripped)} 个" if len(
                conflicting_stripped) > 3 else "")
            gaps.append(
                f"成分响应 {len(conflicting_stripped)} 组剥后缀后同码跨交易所"
                f"矛盾（{shown}{more}），身份矛盾整组剔除（保守，不取其一）")
        if blank_date_rows and full_constituent_audit:
            # 审计计数 gap 仅二级全量资格链发布（一级缺口文本与基线逐字一致）
            gaps.append(
                f"成分响应 {blank_date_rows} 行空日期（原始行审计计数，"
                "剔除不采用）")
        if conflicting_name_rows:
            shown_conflict = "、".join(
                c.split(".", 1)[0] for c in sorted(conflicting_names)[:5])
            gaps.append(
                f"成分响应 {conflicting_name_rows} 行同码重复名称矛盾"
                f"（{shown_conflict}，计数披露），矛盾事实不静默择名，"
                "不授予全量资格")
        if off_dates:
            shown_dates = "、".join(sorted(off_dates)[:3])
            more = f" 等 {len(off_dates)} 个日期" if len(off_dates) > 3 else ""
            gaps.append(
                f"成分响应存在非观察日 {observation_date} 的行"
                f"（{shown_dates}{more}，异日成分不与目标日截面混用），已剔除并记录")
        if echo_conflict:
            # 不回显原始值：冲突回显身份是供应商返回的意外字符串（可能畸形
            # 或超长），缺口只含数量与截断长度（每值 ≤16 字符）
            shown_echo = "、".join(v[:16] for v in echoed_identities[:3])
            more = (f" 等 {len(echoed_identities)} 个" if len(
                echoed_identities) > 3 else "")
            gaps.append(
                f"成分响应表回显身份（{shown_echo}{more}，已截断）与请求"
                f"绑定指数 {index_code} 冲突，冲突表整表不采用（行不进"
                "成员集合、目标成员身份不可采信）")
        if rows_total and not codes and not rejected_identity_rows \
                and not conflicting_stripped:
            gaps.append(
                f"成分响应共 {rows_total} 行但无 {observation_date} 同日合格行，"
                "未取得该日成分（非空成分，如实缺口）")
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"成分响应形状异常（{type(exc).__name__}）") from None
    # 可发布上限：供应商响应超出载荷 schema 成员上界属异常形状——成员集合
    # 整体不发布（明确有界源缺口；不发布全集覆盖、不产生载荷校验 500）
    over_publish_limit = rows_total > _MAX_PUBLISHABLE_MEMBER_ROWS
    if over_publish_limit:
        codes = []
        names = {}
        gaps.append(
            f"成分响应 {rows_total} 行超出可发布成员上限 "
            f"{_MAX_PUBLISHABLE_MEMBER_ROWS}，成员集合不发布（完整性不可证、"
            "不发布全集覆盖）")
    if rows_total == 0 and not shape_gaps:
        # 响应结构整体无成员表/无成员行：按源错误处理（与既有快照/日线 wire
        # 同语义），调用方降缺口（成分未取得 ≠ 非成员）；表级形状问题
        # （缺列/不对齐）已在 shape_gaps 如实记录，返回空成员由判定层保守未知
        raise SwSectorFetchError(
            "成分响应无成员行（" + ("、".join(gaps) if gaps else "tables 为空") + "）")
    return {
        "member_codes": codes,
        "member_names": names,
        "dates_seen": sorted(dates_seen),
        "rows_total": rows_total,
        "completeness_evidence": None,
        "rejected_identity_rows": rejected_identity_rows,
        "conflicting_code_members": conflicting_members,
        "blank_date_rows": blank_date_rows,
        "off_date_rows": off_date_rows,
        "conflicting_name_rows": conflicting_name_rows,
        "identity_echo_conflict": echo_conflict,
        "shape_gap_count": len(shape_gaps),
        "over_publish_limit": over_publish_limit,
        "gaps": gaps,
    }


def fetch_index_member_count(index_code: str, observation_date: str, *,
                             refresh_token: str, timeout_seconds: float,
                             deadline: Optional[float]) -> Dict[str, Any]:
    """供应商同日成分计数 wire（``basic_data_service`` +
    ``ths_constituent_num_index``，issue #104；父规格 #98 决策 4）。

    与 p03473 成分名单做双接口交叉校验的计数侧证据。**单指数绑定**：
    ``codes`` 只携带本请求指数（调用方为每次全量资格判定单独发起，请求
    即身份绑定）；``indiparams`` **显式**传同一交易日 ``YYYYMMDD``（未传
    日期时供应商回全 null 计数——2026-11-20 实测）。

    校验（任一不成立抛 :class:`SwSectorFetchError`，由调用方降全量缺口）：
    - 结果表 ``thscode`` 必须与请求指数**精确一致且恰好一个表**；响应出现
      其他指数行（串批/越界）或同码多表 → 唯一性不可证，拒绝（不取其一）；
    - 指标值列表必须恰好一个值，且为**唯一无冲突的正整数**（bool 永不作
      数值；DT_INTEGER 语义下整值浮点按数值等价接受）。响应以
      ``parse_float=Decimal`` **精确解码**（issue #104 复审）：JSON 字面
      ``5.0000000000000001`` 在二进制浮点解码会损失为 ``5.0`` 并绕过整数
      gate——精确值含小数即拒绝，解码后不可补救；超出可发布成员上界
      （与成分 wire 同一 ``_MAX_PUBLISHABLE_MEMBER_ROWS``）拒绝；
    - 供应商**输入参数回显**（``inputParams`` 中该指标的参数组）必须携带
      ``FDATE`` 且值与请求交易日一致——日期不在结果表内，回显是「计数
      属于该日」的唯一供应商证据，缺失或不符即拒绝。
    返回 ``{"index_code", "observation_date", "count", "fdate_echo", "gaps"}``。
    """
    ymd = observation_date.replace("-", "")
    body = {
        "codes": index_code,
        "indipara": [{
            "indicator": "ths_constituent_num_index",
            "indiparams": [ymd],
        }],
    }
    payload = _sector_http_post(
        _BASIC_DATA_ENDPOINT, body, label="sector_sw_member_count",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline, parse_float=Decimal,
    )

    def _fail(reason: str) -> SwSectorFetchError:
        return SwSectorFetchError(
            f"成分计数核验失败（{index_code} {observation_date}）：{reason}")

    try:
        raw_tables = payload.get("tables")
        tables = raw_tables if isinstance(raw_tables, list) else []
        # 非对象表项不静默过滤——先拒绝再判定唯一表（malformed 即拒绝）
        if any(not isinstance(t, dict) for t in tables):
            raise _fail("结果表含非对象表项（形状异常，不过滤后判定）")
        matched = [t for t in tables
                   if str(t.get("thscode") or "").strip() == index_code]
        if not matched:
            raise _fail("结果表无请求指数行（计数缺失）")
        if len(matched) > 1:
            raise _fail(
                f"结果表同码出现 {len(matched)} 个表（唯一性不可证，冲突拒绝）")
        if len(matched) != len(tables):
            raise _fail(
                "单指数绑定请求的响应含非请求指数行（串批/越界形状异常）")
        values = matched[0].get("table") if isinstance(
            matched[0].get("table"), dict) else {}
        raw_values = values.get("ths_constituent_num_index")
        if not isinstance(raw_values, list) or len(raw_values) != 1:
            raise _fail("计数列不是恰好一个值（形状异常）")
        raw_value = raw_values[0]
        # 唯一无冲突的正整数：bool 永不作数值；DT_INTEGER 语义下整值浮点
        # 按数值等价接受（``parse_float=Decimal`` 精确解码后按 Decimal
        # 整值判定——含小数的精确值如 5.0000000000000001 拒绝，不被二进制
        # 浮点解码掩盖）；其他类型（字符串等）不 str() 合法化
        if isinstance(raw_value, bool) or not isinstance(
                raw_value, (int, float, Decimal)):
            raise _fail("计数值不是整数（类型非法）")
        # 有限、整值与区间都在**原值**上校验后才做 int()（issue #104 三轮）：
        # 巨数指数字面（如 1e5000）的 int() 展开可能超预算/触发整数位数上限
        # ValueError；拒绝消息只描述要求，不展开巨数、不回显供应商原始值
        if isinstance(raw_value, Decimal):
            if not raw_value.is_finite():
                raise _fail("计数值不是整数（非有限）")
            if raw_value != raw_value.to_integral_value():
                raise _fail("计数值不是整数（精确值含小数）")
            if raw_value <= 0:
                raise _fail("计数值不是正整数")
            if raw_value > _MAX_PUBLISHABLE_MEMBER_ROWS:
                raise _fail(
                    f"计数超出可发布成员上限 {_MAX_PUBLISHABLE_MEMBER_ROWS}")
        elif isinstance(raw_value, float) and (
                raw_value != int(raw_value) or raw_value != raw_value):
            raise _fail("计数值不是整数（浮点含小数或非有限）")
        count = int(raw_value)
        if count <= 0:
            raise _fail("计数值不是正整数")
        if count > _MAX_PUBLISHABLE_MEMBER_ROWS:
            raise _fail(
                f"计数超出可发布成员上限 {_MAX_PUBLISHABLE_MEMBER_ROWS}")
        # 供应商输入参数回显：日期不在结果表内，FDATE 回显是「计数属于
        # 该日」的唯一供应商证据。匹配指标组内**每一个出现的 FDATE** 都
        # 必须是非空有效值且全部一致：相同重复可去重；显式空白/null 是
        # 无效源结果（与有效回显并存即拒绝，不能跳过空白后接受剩余值；
        # 仅空白/null = 缺失）；矛盾回显（如 [错误, 正确] 的 last-wins
        # 陷阱）与非字符串值（不 str() 合法化）一律拒绝。gap 文本不回显
        # 供应商返回的任意字符串（只披露数量与事实）。
        input_params = payload.get("inputParams")
        echo_values: List[str] = []
        blank_entries = 0
        invalid_echo = False
        if isinstance(input_params, dict):
            param_groups = input_params.get("params")
            groups = (param_groups if isinstance(param_groups, list)
                      else [])
            for group in groups:
                if not isinstance(group, dict):
                    continue
                if str(group.get("function") or "") != \
                        "ths_constituent_num_index":
                    continue
                group_params = group.get("params")
                if not isinstance(group_params, list):
                    continue
                for p in group_params:
                    if not isinstance(p, dict):
                        continue
                    if str(p.get("name") or "") == "FDATE":
                        value = p.get("value")
                        if value is None or (isinstance(value, str)
                                             and not value.strip()):
                            blank_entries += 1  # 显式空白 = 无效源结果
                            continue
                        if not isinstance(value, str):
                            invalid_echo = True
                            continue
                        echo_values.append(value.strip())
        if invalid_echo:
            raise _fail("供应商输入参数回显含非字符串 FDATE 值（无效回显）")
        unique_echoes = list(dict.fromkeys(echo_values))
        if unique_echoes and blank_entries:
            raise _fail(
                f"供应商输入参数回显含 {blank_entries} 个空白/null FDATE "
                "条目与有效值并存（无效源结果，不采纳）")
        if not unique_echoes:
            raise _fail("供应商输入参数回显缺 FDATE（日期证据缺失）")
        if len(unique_echoes) > 1:
            raise _fail(
                f"供应商输入参数回显出现 {len(unique_echoes)} 个不同 FDATE "
                "值（矛盾回显，不取其一，值不回显）")
        fdate_echo = unique_echoes[0]
        if fdate_echo != ymd:
            raise _fail(
                "供应商输入参数回显 FDATE 与请求交易日 "
                f"{ymd} 不符（回显值不回显）")
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级
        raise _fail(f"响应形状异常（{type(exc).__name__}）") from None
    return {
        "index_code": index_code,
        "observation_date": observation_date,
        "count": count,
        "fdate_echo": fdate_echo,
        "gaps": [],
    }


def classify_membership(target_secucode: str, members: Dict[str, Any], *,
                        observation_date: str) -> Dict[str, Any]:
    """指数成分边界三态判定（#98 决策 3：已纳入 / 明确未纳入 / 未知）。

    - ``included``：目标带交易所身份在同日成分集合中（positive evidence，
      无需完整性证明——找到即找到）；
    - ``excluded``：目标未命中且完整性证据可证（完整集合或明确独立证据；
      现场 p03473 无完整性元数据，此态仅受控/证据升级路径可达）；
    - ``unknown``：目标未命中且完整性不可证（现场常态），或成分未取得
      （查询失败/未取得 ≠ 非成员，不冒充排除）。

    返回 ``{status, target_secucode, observation_date, member_count,
    completeness_evidence, gaps}``；``member_count`` 是观测行数（非全集计数）。
    """
    member_codes = [str(c) for c in (members.get("member_codes") or [])]
    evidence = members.get("completeness_evidence")
    gaps = [str(g) for g in (members.get("gaps") or [])]
    member_count = len(member_codes)
    obtained = bool(member_codes)
    if target_secucode in member_codes:
        status = "included"
    elif obtained and evidence:
        # 排除需要真实成员集 + 完整性证据；空成员集与「完整集合」证据矛盾
        # （申万一级指数均有成分），保守未知
        status = "excluded"
    else:
        status = "unknown"
        if not obtained:
            gaps = gaps + [
                f"目标股 {target_secucode} 未命中且成分集合不可用/完整性不可证，"
                "成分状态未知（查询失败或无完整性证据 ≠ 非成员）"]
    return {
        "status": status,
        "target_secucode": target_secucode,
        "observation_date": observation_date,
        "member_count": member_count if obtained else None,
        "completeness_evidence": evidence if status != "unknown" else None,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# 指数日线（cmd_history_quotation）
# ---------------------------------------------------------------------------


def fetch_sw_l1_close_quotes(session_date: str, *, refresh_token: str,
                             timeout_seconds: float, deadline: Optional[float],
                             ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """查已收盘交易日的申万一级截面，按日线日期资格采纳，不生成报价时刻。"""
    codes = [code for code, _ in SW_L1_DIRECTORY]
    directory = set(codes)
    body = {
        "codes": ",".join(codes),
        "indicators": "close,preClose,changeRatio,amount,turnoverRatio",
        "startdate": session_date,
        "enddate": session_date,
        "functionpara": {"Interval": "D", "CPS": "1", "Fill": "Omit"},
    }
    payload = _sector_http_post(
        _HISTORY_ENDPOINT, body, label="sector_sw_close_quotes",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline, parse_float=_parse_quote_number,
    )
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise SwSectorFetchError("收盘行情响应形状异常（tables 非列表）")

    rows: List[Dict[str, Any]] = []
    for raw_table in tables:
        try:
            table = SwIndexCloseTable.model_validate(raw_table)
        except ValidationError:
            continue
        if table.thscode not in directory:
            continue
        columns = table.table if isinstance(table.table, dict) else {}
        for index, day in enumerate(table.time):
            if day not in (session_date, f"{session_date} 00:00:00"):
                continue
            # 错位列按缺值处理。保留同日无效行参与同码冲突裁决。
            cells = {name: column[index:index + 1] for name, column in columns.items()
                     if isinstance(column, list) and len(column) == len(table.time)}
            close = _first_finite_raw(cells.get("close"))
            pre_close = _first_finite_raw(cells.get("preClose"))
            prices_qualified = (close is not None and close > 0
                                and pre_close is not None and pre_close > 0)
            rows.append({
                "thscode": table.thscode, "trade_date": session_date,
                "change_pct": (_first_finite_raw(cells.get("changeRatio"))
                               if prices_qualified else None),
                "latest": close, "pre_close": pre_close,
                "amount": _first_finite(cells.get("amount")),
                "turnover": _first_finite(cells.get("turnoverRatio")),
            })
    return qualify_quote_rows(rows, session_date=session_date)


def _history_close_key(value: Any) -> Tuple[str, Any]:
    """收盘等价键（与既有资格层收盘语义一致）：可解析为有限正 Decimal →
    ``("valid", Decimal)``——数值等价（wire ``10`` 与 ``"10.00"`` 同键）；
    否则 ``("invalid", None)``——None/NaN/布尔/非数值/非正一律不作数值
    比较（布尔永不作数值，缺值语义同类）。"""
    if value is None or (isinstance(value, float)
                         and not math.isfinite(value)):
        return ("invalid", None)
    try:
        d = Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return ("invalid", None)
    if not d.is_finite() or d <= 0:
        return ("invalid", None)
    return ("valid", d)


def fetch_index_history(codes: str, start_date: str, end_date: str, *,
                        refresh_token: str, timeout_seconds: float,
                        deadline: Optional[float],
                        isolate_conflicting_dates: bool = False
                        ) -> Dict[str, pd.DataFrame]:
    """指数日线收盘 wire 解析：``{thscode: DataFrame(date, close)}``。

    请求固定 ``Interval=D / CPS=1（不复权，指数口径）/ Fill=Omit（无交易
    日期剔除——严格交易日历对齐由调用方完成）``。

    同一 thscode 可能拆多表返回：同码观测**逐行合并**（缺失段互补，不取
    末表覆盖前表）。合并前先做行级归一（与既有严格窗口资格语义一致）：
    - **日期归一**：``pd.to_datetime``（mixed + coerce）解析后按自然日
      ``date()`` 作键——同一交易日的不同 wire 形式（``"2026-11-03"`` 与
      ``"2026-11-03 00:00:00"``）同键，不产生伪重复；
    - **请求窗口过滤**：归一日期在 ``[start_date, end_date]`` 之外的行为
      窗口外行，与资格层语义一致忽略——窗口外矛盾不淘汰窗口内有效行；
    - **收盘等价**：按有限正 Decimal 数值比较（``10`` ≡ ``"10.00"``；
      布尔/非数/非正同类缺值）。同日等值去重保留一行，同日不等价按
      ``isolate_conflicting_dates`` 分级：
      - ``False``（默认，旧入口 20 点趋势等）：该代码整体保守拒绝（不取
        其一；不影响其他代码的有效行——调用方按缺行降缺口）；
      - ``True``（多窗口路径，issue #103 复审）：只隔离该无效日期——该日
        从该代码序列剔除（两行一并放弃，不取其一），其余日期照常保留。
        单次 61 点查询承载 5/20/60 三个窗口，某日冲突不应放大为整条序列
        全窗口丢失；冲突日缺失由调用方资格层按缺合格收盘点降缺口（仅影响
        含该日的窗口）。
    不可解析日期行原样透传（资格层忽略并计数，行级诊断不丢）。"""
    body = {
        "codes": codes,
        "indicators": "close",
        "startdate": start_date,
        "enddate": end_date,
        "functionpara": {"Interval": "D", "CPS": "1", "Fill": "Omit"},
    }
    payload = _sector_http_post(
        _HISTORY_ENDPOINT, body, label="sector_sw_history",
        refresh_token=refresh_token, timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    try:
        raw_tables = payload.get("tables")
        tables = raw_tables if isinstance(raw_tables, list) else []
        # 请求窗口成对表示（两端同取同弃的显式不变式）：解析失败整体为
        # None——退化为不按窗口过滤（资格层仍按期望交易日集对齐，不放大
        # 采用面），使用点无需再对两端分别收窄
        window: Optional[Tuple[date, date]] = None
        try:
            window = (date.fromisoformat(start_date),
                      date.fromisoformat(end_date))
        except ValueError:
            window = None
        rows_by_code: Dict[str, Dict[date, Tuple[Tuple[str, Any], Any]]] = {}
        passthrough_by_code: Dict[str, List[Tuple[str, Any]]] = {}
        conflicting: Set[str] = set()
        # isolate 模式下的按码冲突日期集：该日两行一并放弃（不取其一），
        # 后续重复行同样忽略；其余日期照常合并
        isolated_dates: Dict[str, Set[date]] = {}
        for table in tables:
            if not isinstance(table, dict):
                continue
            thscode = str(table.get("thscode") or "").strip()
            raw_times = table.get("time")
            time_list = raw_times if isinstance(raw_times, list) else []
            dates = [str(d) for d in time_list]
            raw_cells = table.get("table")
            cells = raw_cells if isinstance(raw_cells, dict) else {}
            raw_closes = cells.get("close")
            closes = raw_closes if isinstance(raw_closes, list) else []
            if not thscode or not dates or not closes:
                continue
            if thscode in conflicting:
                continue
            parsed = pd.to_datetime(dates, errors="coerce", format="mixed")
            merged = rows_by_code.setdefault(thscode, {})
            passthrough = passthrough_by_code.setdefault(thscode, [])
            isolated = isolated_dates.setdefault(thscode, set())
            for d_ts, raw_date, c_raw in zip(parsed, dates, closes):
                if pd.isna(d_ts):
                    # 不可解析日期行：原样透传（资格层忽略并注明，诊断不丢）
                    passthrough.append((raw_date, c_raw))
                    continue
                d = d_ts.date()
                if window is not None and not (window[0] <= d <= window[1]):
                    continue  # 窗口外行：资格层忽略，不参与冲突判定
                if d in isolated:
                    continue  # 已隔离日期：后续重复行一并忽略（不取其一）
                key = _history_close_key(c_raw)
                prior = merged.get(d)
                if prior is not None and prior[0] != key:
                    # 同码同日收盘矛盾（数值不等或有效性不同类）：供应商对
                    # 同一身份给出冲突事实。isolate 模式只隔离该无效日期
                    #（两行一并放弃，不取其一；已合并观测一并放弃，该日
                    # 由资格层降缺口）；默认模式整码保守拒绝。
                    if isolate_conflicting_dates:
                        isolated.add(d)
                        merged.pop(d, None)
                        continue
                    conflicting.add(thscode)
                    rows_by_code.pop(thscode, None)
                    passthrough_by_code.pop(thscode, None)
                    break
                if prior is None:
                    merged[d] = (key, c_raw)
        frames: Dict[str, pd.DataFrame] = {}
        for thscode, merged in rows_by_code.items():
            rows = [(d.isoformat(), merged[d][1]) for d in sorted(merged)]
            rows.extend(passthrough_by_code.get(thscode) or [])
            frames[thscode] = pd.DataFrame(
                {"date": [r[0] for r in rows],
                 "close": [r[1] for r in rows]},
            )
    except SwSectorFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 - wire 形状异常按源错误降级，不逃出边界
        raise SwSectorFetchError(f"日线响应形状异常（{type(exc).__name__}）") from None
    if not frames:
        raise SwSectorFetchError(
            "日线响应无有效行（tables 为空或同码矛盾整码拒绝）")
    return frames
