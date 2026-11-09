"""Market catalog for quote.eastmoney.com/center.

Every entry was verified against the live push2 endpoint: `fs` is the exact
filter expression the site itself uses, `total` records the row count observed
when the catalog was built (2026-11-05). Categories mirror the site's own
sidebar so the platform stays recognisable to anyone coming from EastMoney.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class Market:
    id: str
    group: str
    name: str
    fs: str
    kind: str
    interval: int = 120
    history: bool = False
    total: int = 0
    note: str = ""
    primary: bool = False  # shown on the dashboard heat grid
    session: str = "CN"    # trading calendar key (see sessions.py)


# --- global indices: the site's own regional baskets -------------------------
ASIA = (
    "i:1.000001,i:0.399001,i:0.399005,i:0.399006,i:1.000300,i:100.HSI,"
    "i:100.HSCEI,i:124.HSCCI,i:100.TWII,i:100.N225,i:100.KOSPI200,i:100.KS11,"
    "i:100.STI,i:100.SENSEX,i:100.KLSE,i:100.SET,i:100.PSI,i:100.KSE100,"
    "i:100.VNINDEX,i:100.JKSE,i:100.CSEALL"
)
EUROPE = (
    "i:100.SX5E,i:100.FTSE,i:100.MCX,i:100.AXX,i:100.FCHI,i:100.GDAXI,i:100.RTS,"
    "i:100.IBEX,i:100.PSI20,i:100.OMXC20,i:100.BFX,i:100.AEX,i:100.WIG,"
    "i:100.OMXSPI,i:100.SSMI,i:100.HEX,i:100.OSEBX,i:100.ATX,i:100.MIB,i:100.ASE,"
    "i:100.ICEXI,i:100.PX,i:100.ISEQ"
)
AMERICA = "i:100.DJIA,i:100.SPX,i:100.NDX,i:100.TSX,i:100.BVSP,i:100.MXX"
OCEANIA = "i:100.AS51,i:100.AORD,i:100.NZ50"
OTHER = "i:100.UDI,i:100.BDI,i:100.CRB"

GROUPS: list[tuple[str, str, str]] = [
    ("global", "全球市场", "指数与全球股指"),
    ("cn", "沪深京", "A股与沪深指数"),
    ("hk", "港股市场", "港交所挂牌品种"),
    ("us", "美股市场", "纽交所/纳斯达克/美交所"),
    ("fx", "外汇市场", "汇率与离岸人民币"),
    ("futures", "期货市场", "国内交易所与国际期货"),
    ("bonds", "债券市场", "现券、可转债与债券指数"),
    ("funds", "基金市场", "场内ETF/LOF/REITs"),
    ("sectors", "板块行情", "行业/概念/地域"),
    ("metals", "贵金属", "上海金与伦敦金银"),
]

MARKETS: tuple[Market, ...] = (
    # ---- 全球 ---------------------------------------------------------------
    Market("global.asia", "global", "亚洲股市", ASIA, "index", 45, True, 21),
    Market("global.europe", "global", "欧洲股市", EUROPE, "index", 45, True, 23),
    Market("global.america", "global", "美洲股市", AMERICA, "index", 45, True, 6),
    Market("global.oceania", "global", "澳洲股市", OCEANIA, "index", 60, True, 3),
    Market("global.other", "global", "其他指数", OTHER, "index", 60, True, 3),
    Market("global.all", "global", "全球指数", "m:100", "index", 90, True, 63, "", True),
    # ---- 沪深京 -------------------------------------------------------------
    Market("cn.a", "cn", "沪深京A股",
           "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048", "stock", 300, False, 5927),
    Market("cn.sh", "cn", "上证A股", "m:1+t:2,m:1+t:23", "stock", 300, False, 2468),
    Market("cn.sz", "cn", "深证A股", "m:0+t:6,m:0+t:80", "stock", 300, False, 3095),
    Market("cn.gem", "cn", "创业板", "m:0+t:80", "stock", 300, False, 1453),
    Market("cn.star", "cn", "科创板", "m:1+t:23", "stock", 300, False, 622),
    Market("cn.bj", "cn", "北证A股", "m:0+t:81+s:2048", "stock", 300, False, 364),
    Market("cn.index", "cn", "沪深指数", "m:1+t:1,m:0+t:5", "index", 90, False, 1225, "", True),
    # ---- 港股 ---------------------------------------------------------------
    Market("hk.all", "hk", "全部港股", "m:128+t:3,m:128+t:4", "stock", 300, False, 2932),
    Market("hk.main", "hk", "港股主板", "m:128+t:3", "stock", 300, False, 2618, "", True),
    Market("hk.gem", "hk", "港股创业板", "m:128+t:4", "stock", 300, False, 314),
    Market("hk.index", "hk", "香港指数", "m:305,m:124,m:125", "index", 90, False, 368, "", True),
    # ---- 美股 ---------------------------------------------------------------
    Market("us.all", "us", "全部美股", "m:105,m:106,m:107", "stock", 300, False, 13895, "", True),
    Market("us.nasdaq", "us", "纳斯达克", "m:105", "stock", 300, False, 6073),
    Market("us.nyse", "us", "纽交所", "m:106", "stock", 300, False, 2966),
    Market("us.amex", "us", "美交所", "m:107", "stock", 300, False, 4856),
    Market("us.index", "us", "美股指数",
           "i:100.NDX,i:100.DJIA,i:100.SPX", "index", 60, True, 3, "", True),
    # ---- 外汇 ---------------------------------------------------------------
    Market("fx.all", "fx", "全部汇率", "m:119,m:120,m:133", "forex", 60, True, 190, "", True),
    Market("fx.basic", "fx", "基本汇率", "m:119", "forex", 60, True, 145),
    Market("fx.cnyc", "fx", "人民币中间价", "m:120", "forex", 90, True, 25),
    Market("fx.cnh", "fx", "离岸人民币", "m:133", "forex", 90, True, 20),
    # ---- 期货 ---------------------------------------------------------------
    Market("fut.shfe", "futures", "上期所", "m:113", "future", 60, False, 278, "", True),
    Market("fut.ine", "futures", "上期能源", "m:142", "future", 60, False, 73),
    Market("fut.dce", "futures", "大商所", "m:114", "future", 60, False, 302),
    Market("fut.czce", "futures", "郑商所", "m:115", "future", 60, False, 295),
    Market("fut.gfex", "futures", "广期所", "m:225", "future", 60, False, 58),
    Market("fut.cffex", "futures", "中金所", "m:220", "future", 60, False, 72, "", True),
    Market("fut.hkex", "futures", "港交所期货", "m:130", "future", 90, False, 555),
    Market("fut.global", "futures", "国际期货",
           "m:101,m:102,m:103,m:104,m:108,m:109,m:110,m:111,m:112",
           "future", 90, False, 639, "", True),
    # ---- 债券 ---------------------------------------------------------------
    Market("bond.sh", "bonds", "沪市现券", "m:1+b:MK0351", "bond", 180, False, 234),
    Market("bond.sz", "bonds", "深市现券", "m:0+b:MK0351", "bond", 180, False, 234),
    Market("bond.cb", "bonds", "可转债", "b:MK0354", "bond", 120, False, 317, "", True),
    Market("bond.index", "bonds", "债券指数",
           "i:1.000012,i:1.000013,i:1.000022,i:1.000061,i:0.395021,i:0.395022,"
           "i:0.395031,i:0.395032,i:0.399481", "index", 180, True, 9, "", True),
    # ---- 基金 ---------------------------------------------------------------
    Market("fund.all", "funds", "全部场内基金",
           "b:MK0021,b:MK0022,b:MK0023,b:MK0827", "fund", 300, False, 1665, "", True),
    Market("fund.etf", "funds", "股票ETF", "b:MK0021", "fund", 300, False, 1376),
    Market("fund.money", "funds", "货币ETF", "b:MK0022", "fund", 300, False, 27),
    Market("fund.cross", "funds", "跨境ETF", "b:MK0023", "fund", 300, False, 245),
    Market("fund.gold", "funds", "商品ETF", "b:MK0827", "fund", 300, False, 17),
    # ---- 板块 ---------------------------------------------------------------
    Market("sector.industry", "sectors", "行业板块", "m:90+t:2", "sector", 90, False, 496, "", True),
    Market("sector.concept", "sectors", "概念板块", "m:90+t:3", "sector", 90, False, 531, "", True),
    Market("sector.region", "sectors", "地域板块", "m:90+t:1", "sector", 120, False, 31),
    # ---- 贵金属 -------------------------------------------------------------
    Market("metal.cn", "metals", "上海黄金", "m:118", "commodity", 60, True, 21, "", True),
    Market("metal.intl", "metals", "国际贵金属", "m:122,m:123", "commodity", 60, True, 18, "", True),
)

# Which trading calendar each market follows (see sessions.py).
_SESSIONS: dict[str, str] = {
    "global.asia": "ASIA",
    "global.europe": "EUROPE",
    "global.america": "AMERICA",
    "global.oceania": "OCEANIA",
    "global.other": "FX",
    "global.all": "WORLD",
    "cn.sh": "CN", "cn.sz": "CN", "cn.gem": "CN", "cn.star": "CN", "cn.bj": "CN",
    "cn.a": "CN", "cn.index": "CN",
    "hk.all": "HK", "hk.main": "HK", "hk.gem": "HK", "hk.index": "HK",
    "us.all": "US", "us.nasdaq": "US", "us.nyse": "US", "us.amex": "US", "us.index": "US",
    "fx.all": "FX", "fx.basic": "FX", "fx.cnyc": "FX", "fx.cnh": "FX",
    "fut.shfe": "CN_FUT", "fut.ine": "CN_FUT", "fut.dce": "CN_FUT",
    "fut.czce": "CN_FUT", "fut.gfex": "CN_FUT", "fut.cffex": "CN_FUT",
    "fut.hkex": "HK", "fut.global": "GLOB_FUT",
    "bond.sh": "CN", "bond.sz": "CN", "bond.cb": "CN", "bond.index": "CN",
    "fund.all": "CN", "fund.etf": "CN", "fund.money": "CN",
    "fund.cross": "CN", "fund.gold": "CN",
    "sector.industry": "CN", "sector.concept": "CN", "sector.region": "CN",
    "metal.cn": "CN", "metal.intl": "GLOB_FUT",
}

MARKETS = tuple(replace(m, session=_SESSIONS.get(m.id, "CN")) for m in MARKETS)

MARKET_MAP: dict[str, Market] = {m.id: m for m in MARKETS}
GROUP_MAP: dict[str, tuple[str, str]] = {g[0]: (g[1], g[2]) for g in GROUPS}

# Headline tape: the indices a trader expects at the top of a global monitor.
HEADLINES: tuple[tuple[str, str], ...] = (
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("100.HSI", "恒生指数"),
    ("100.N225", "日经225"),
    ("100.KS11", "韩国综合"),
    ("100.DJIA", "道琼斯"),
    ("100.SPX", "标普500"),
    ("100.NDX", "纳斯达克"),
    ("100.GDAXI", "德国DAX"),
    ("100.FTSE", "英国富时100"),
    ("100.FCHI", "法国CAC40"),
    ("100.SX5E", "欧洲斯托克50"),
    ("100.AS51", "澳洲200"),
    ("100.SENSEX", "印度SENSEX"),
    ("100.TSX", "加拿大TSX"),
    ("100.BVSP", "巴西IBOVESPA"),
)

# Instruments kept in the daily k-line cache (secid -> label).
KLINE_WATCH: tuple[tuple[str, str], ...] = (
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000300", "沪深300"),
    ("100.HSI", "恒生指数"),
    ("100.HSCEI", "国企指数"),
    ("100.N225", "日经225"),
    ("100.KS11", "韩国综合"),
    ("100.DJIA", "道琼斯"),
    ("100.SPX", "标普500"),
    ("100.NDX", "纳斯达克"),
    ("100.GDAXI", "德国DAX"),
    ("100.FTSE", "英国富时100"),
    ("100.FCHI", "法国CAC40"),
    ("100.SX5E", "欧洲斯托克50"),
    ("100.AS51", "澳洲200"),
    ("100.SENSEX", "印度SENSEX"),
    ("100.BVSP", "巴西IBOVESPA"),
    ("100.RTS", "俄罗斯RTS"),
    ("1.000012", "国债指数"),
)

# Quote fields requested from /api/qt/clist/get.
QUOTE_FIELDS = (
    "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f62,f115,f124,f152"
)


def market_ids() -> list[str]:
    return [m.id for m in MARKETS]
