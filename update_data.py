# -*- coding: utf-8 -*-
"""A股AI主题波段监控 —— 数据抓取 + 指标计算 + 导出网页数据。

数据源: 中证指数官网 (csindex.com.cn), 免费无鉴权, 含OHLC/成交额/PE。
        东财接口在本机被拦截, 故不使用akshare。

指标体系(波段维度, 5-20日窗口, 目标捕捉10个交易日左右的波段):

  方向分 SwingScore (0-100) —— 波段方向与强度
    trend    40%: 收盘对10日线乖离(±6%映射) + MA5>MA10 + MA10的5日斜率
    momentum 35%: 相对沪深300的10日超额收益(±8%映射) + RS线是否在其10日均线上方
    breadth  25%: 7个AI产业链指数中收于10日均线上方的比例

  温度分 Temp (0-100) —— 波段过热/冰点
    拥挤 60%: AI主题成交额占中证全指比例(5日平滑)的滚动1年百分位
    涨速 40%: 指数10日涨幅的滚动1年百分位

  状态机(方向 x 温度):
    up_healthy 方向>=60 温度<80   上行波段·健康
    up_hot     方向>=60 温度>=80  上行波段·过热(移动止盈区)
    neutral    40<=方向<60        转折/震荡
    down       方向<40 温度>30    下行波段
    down_cold  方向<40 温度<=30   冰点·波段酝酿

  波段信号(在图上标记, 并回测验证; 规则经多变体对比选定, 对T+1成交稳健):
    入场 = 方向分上穿60 且 温度<70 (新波段启动且不追过热)
    离场 = 方向分跌破40 (波段破位) 或 温度>=85且收盘跌破10日线 (过热破线)

用法:
    python update_data.py             # 拉数据 + 计算 + 写 data.js
    python update_data.py --no-fetch  # 用 data/ 缓存重算(调试用)
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8))  # 云端runner是UTC, 统一用北京时间


def now_bj() -> datetime:
    return datetime.now(BJT)

import numpy as np
import pandas as pd
import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

START = "20150101"
PCT_WINDOW = 250        # 温度分位的滚动窗口(交易日, 约1年)
PCT_MIN_PERIODS = 120
CHART_DAYS = 500        # 网页图表保留最近约2年

# 信号阈值
ENTRY_SCORE = 60        # 方向分上穿此值 => 入场
ENTRY_TEMP_MAX = 70     # 入场时温度须低于此值(不追过热启动)
EXIT_SCORE = 40         # 方向分跌破此值 => 波段破位离场
HOT_TEMP = 80           # 温度过热线(状态机)
EXIT_TEMP = 85          # 过热离场的温度条件
COLD_TEMP = 30          # 冰点线

MAIN = "930713"         # 中证人工智能主题 (CS人工智)
BENCH = "000300"        # 沪深300
MARKET = "000985"       # 中证全指(全市场成交额分母)

# AI产业链指数(广度篮子 = 主指数 + 以下6个)
CHAIN = [
    ("931071", "人工智能产业"),
    ("H30184", "半导体"),
    ("931160", "通信设备"),
    ("930651", "计算机"),
    ("930851", "云计算大数据"),
    ("H30590", "机器人"),
]

ALL_CODES = [(MAIN, "CS人工智(主题)"), (BENCH, "沪深300"), (MARKET, "中证全指")] + CHAIN
NAME = dict(ALL_CODES)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
CSINDEX_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"

STATES = {
    "up_healthy": {"label": "上行波段 · 健康",
                   "advice": "波段持有区: 方向明确且未过热。跟踪10日线持有, "
                             "等温度>=80转入过热区再启动移动止盈。"},
    "up_hot":     {"label": "上行波段 · 过热",
                   "advice": "移动止盈区: 波段仍在但温度已高, 不再加仓。收盘跌破10日线即离场, "
                             "不等方向分走坏; 惯性冲刺段吃到多少算多少。"},
    "neutral":    {"label": "转折 / 震荡",
                   "advice": "方向不明的换挡区, 波段思维下观望为主。等方向分重新站上60(新波段启动)"
                             "或跌破40(下行确认)再动作。"},
    "down":       {"label": "下行波段",
                   "advice": "回避区: 方向已坏且温度未冷却, 反抽10日线是减仓位不是买点。"
                             "等温度降到30以下进入酝酿区。"},
    "down_cold":  {"label": "冰点 · 波段酝酿",
                   "advice": "左侧观察区: 交易冷清+跌速放缓, 新波段常在此酝酿。"
                             "等方向分站回60的右侧信号入场, 不提前抄底。"},
}


# ---------------- 抓取 ----------------

def fetch_index(code: str, tries: int = 4) -> pd.DataFrame:
    """从中证官网取指数全历史日线。失败则回退到本地缓存。"""
    cache = os.path.join(DATA_DIR, f"{code}.csv")
    last_err = None
    for i in range(tries):
        try:
            r = requests.get(CSINDEX_URL, params={
                "indexCode": code,
                "startDate": START,
                "endDate": f"{now_bj():%Y%m%d}",
            }, headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
                timeout=30)
            r.raise_for_status()
            data = r.json().get("data") or []
            if not data:
                raise RuntimeError("empty payload")
            df = pd.DataFrame(data)
            df = df.rename(columns={
                "tradeDate": "date", "tradingValue": "amount_yi", "peg": "pe",
            })[["date", "open", "high", "low", "close", "amount_yi", "pe"]]
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            df.to_csv(cache, index=False)
            return df
        except Exception as e:
            last_err = e
            time.sleep(1.2 * (i + 1))
    if os.path.exists(cache):
        print(f"  [警告] {code} 拉取失败({last_err}), 使用本地缓存")
        return pd.read_csv(cache, parse_dates=["date"])
    raise RuntimeError(f"{code} 拉取失败且无缓存: {last_err}")


def load_all(no_fetch: bool) -> dict:
    out = {}
    for code, name in ALL_CODES:
        cache = os.path.join(DATA_DIR, f"{code}.csv")
        if no_fetch and os.path.exists(cache):
            df = pd.read_csv(cache, parse_dates=["date"])
        else:
            df = fetch_index(code)
            time.sleep(0.5)
        out[code] = df.set_index("date")
        print(f"  {code} {name}: {len(df)}行, 最新 {df['date'].iloc[-1]:%Y-%m-%d}")
    return out


# ---------------- 指标 ----------------

def rolling_pctile(s: pd.Series, window: int = PCT_WINDOW,
                   min_periods: int = PCT_MIN_PERIODS) -> pd.Series:
    def pct(x):
        x = x[~np.isnan(x)]
        if len(x) < 60:
            return np.nan
        return (x[:-1] <= x[-1]).mean() * 100

    return s.rolling(window, min_periods=min_periods).apply(pct, raw=True)


def clip_map(x, half_range):
    """把 ±half_range 的数值线性映射到 0-100, 中值50。"""
    return np.clip(50 + x / half_range * 50, 0, 100)


def compute(dfs: dict) -> pd.DataFrame:
    main = dfs[MAIN]
    bench = dfs[BENCH].reindex(main.index).ffill(limit=5)
    market = dfs[MARKET].reindex(main.index).ffill(limit=5)

    c = main["close"]
    ma5, ma10, ma20 = c.rolling(5).mean(), c.rolling(10).mean(), c.rolling(20).mean()

    # --- 趋势(波段方向) ---
    bias10 = c / ma10 - 1
    t1 = clip_map(bias10, 0.06)
    t2 = (ma5 > ma10).astype(float) * 100
    t3 = (ma10 > ma10.shift(5)).astype(float) * 100
    trend = 0.5 * t1 + 0.25 * t2 + 0.25 * t3

    # --- 相对动量 (vs 沪深300, 10日) ---
    excess10 = c.pct_change(10) - bench["close"].pct_change(10)
    m1 = clip_map(excess10, 0.08)
    rs = c / bench["close"]
    rs_ma10 = rs.rolling(10).mean()
    m2 = (rs > rs_ma10).astype(float) * 100
    momentum = 0.7 * m1 + 0.3 * m2

    # --- 广度: 主指数+6链条指数中收于MA10上方的比例(缺数据不计入分母) ---
    above = pd.DataFrame(index=main.index)
    for code, _ in [(MAIN, "")] + CHAIN:
        cc = dfs[code]["close"].reindex(main.index).ffill(limit=5)
        ma = cc.rolling(10).mean()
        above[code] = np.where(cc.notna() & ma.notna(), (cc > ma).astype(float), np.nan)
    breadth = above.mean(axis=1) * 100

    score = 0.40 * trend + 0.35 * momentum + 0.25 * breadth

    # --- 温度: 拥挤(成交额占比5日平滑分位) 60% + 涨速(10日涨幅分位) 40% ---
    share = main["amount_yi"] / market["amount_yi"]
    share_ma5 = share.rolling(5).mean()
    crowd_pct = rolling_pctile(share_ma5)
    speed_pct = rolling_pctile(c.pct_change(10))
    temp = 0.6 * crowd_pct + 0.4 * speed_pct

    # --- 估值参考: PE滚动1年分位 ---
    pe_pct = rolling_pctile(main["pe"])

    # --- 状态机 ---
    def state_of(sc, tp):
        if np.isnan(sc):
            return None
        if sc >= ENTRY_SCORE:
            return "up_hot" if (not np.isnan(tp) and tp >= HOT_TEMP) else "up_healthy"
        if sc >= 40:
            return "neutral"
        return "down_cold" if (not np.isnan(tp) and tp <= COLD_TEMP) else "down"

    state = pd.Series([state_of(s_, t_) for s_, t_ in zip(score, temp)], index=main.index)

    ind = pd.DataFrame({
        "close": c, "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "trend": trend, "momentum": momentum, "breadth": breadth, "score": score,
        "bias10": bias10, "excess10": excess10,
        "rs": rs, "rs_ma10": rs_ma10,
        "share": share, "share_ma5": share_ma5,
        "crowd_pct": crowd_pct, "speed_pct": speed_pct, "temp": temp,
        "pe": main["pe"], "pe_pct": pe_pct,
        "bench_close": bench["close"],
    })
    ind["state"] = state
    return ind


# ---------------- 波段信号与回测 ----------------

def swing_signals(ind: pd.DataFrame) -> list:
    """波段交易信号: 入场=方向分上穿60且温度<70, 离场=方向分<40 或 温度>=85且破10日线。

    以信号日收盘价成交(监控用途, 不计滑点; T+1收盘成交的回测结果同量级)。
    返回trades列表, 最后一笔可能未平仓。
    """
    trades = []
    pos = None
    sc, tp = ind["score"].values, ind["temp"].values
    cl, m10 = ind["close"].values, ind["ma10"].values
    for i in range(1, len(ind)):
        d = ind.index[i]
        if pos is None:
            if (sc[i - 1] < ENTRY_SCORE <= sc[i]
                    and not np.isnan(tp[i]) and tp[i] < ENTRY_TEMP_MAX):
                pos = {"entry_date": d, "entry_px": cl[i], "entry_i": i}
        else:
            exit_break = sc[i] < EXIT_SCORE
            exit_hot = (not np.isnan(tp[i])) and tp[i] >= EXIT_TEMP and cl[i] < m10[i]
            if exit_break or exit_hot:
                trades.append({
                    "entry_date": pos["entry_date"], "entry_px": pos["entry_px"],
                    "exit_date": d, "exit_px": cl[i],
                    "hold_days": i - pos["entry_i"],
                    "ret": cl[i] / pos["entry_px"] - 1,
                    "reason": "过热破线" if exit_hot and not exit_break else "方向破位",
                    "open": False,
                })
                pos = None
    if pos is not None:
        i = len(ind) - 1
        trades.append({
            "entry_date": pos["entry_date"], "entry_px": pos["entry_px"],
            "exit_date": None, "exit_px": ind["close"].iloc[-1],
            "hold_days": i - pos["entry_i"],
            "ret": ind["close"].iloc[-1] / pos["entry_px"] - 1,
            "reason": "持仓中", "open": True,
        })
    return trades


def swing_stats(trades: list, since: str) -> dict:
    closed = [t for t in trades if not t["open"] and t["entry_date"] >= pd.Timestamp(since)]
    if not closed:
        return {}
    rets = np.array([t["ret"] for t in closed])
    holds = np.array([t["hold_days"] for t in closed])
    return {
        "since": since[:4],
        "n": int(len(closed)),
        "win_rate": round(float((rets > 0).mean()) * 100, 0),
        "ret_med": round(float(np.median(rets)) * 100, 1),
        "ret_avg": round(float(rets.mean()) * 100, 1),
        "ret_sum": round(float(np.log1p(rets).sum()) * 100, 0),   # 累计对数收益近似
        "hold_med": int(np.median(holds)),
        "worst": round(float(rets.min()) * 100, 1),
        "best": round(float(rets.max()) * 100, 1),
    }


def validate(ind: pd.DataFrame) -> dict:
    """按状态分档统计930713未来10个交易日表现(波段视角的区分度检验)。"""
    c = ind["close"]
    n = len(c)
    v = c.values
    fwd_ret, fwd_dd = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n - 10):
        seg = v[i:i + 11]
        fwd_ret[i] = seg[-1] / seg[0] - 1
        fwd_dd[i] = seg.min() / seg[0] - 1

    full = pd.DataFrame({"state": ind["state"], "ret": fwd_ret, "dd": fwd_dd},
                        index=ind.index).dropna()

    def per_state(df):
        rows = []
        for key in STATES:
            sub = df[df["state"] == key]
            if len(sub) < 10:
                continue
            rows.append({
                "state": key, "label": STATES[key]["label"], "days": int(len(sub)),
                "fwd10_ret_med": round(float(sub["ret"].median()) * 100, 1),
                "fwd10_dd_med": round(float(sub["dd"].median()) * 100, 1),
                "win_rate": round(float((sub["ret"] > 0).mean()) * 100, 0),
            })
        return rows

    d19 = full[full.index >= "2019-01-01"]
    d22 = full[full.index >= "2022-01-01"]
    return {
        "per_state": per_state(d19),
        "per_state_recent": per_state(d22),
        "sample_range": f"{d19.index[0]:%Y-%m} ~ {d19.index[-1]:%Y-%m}",
        "sample_range_recent": f"{d22.index[0]:%Y-%m} ~ {d22.index[-1]:%Y-%m}",
    }


def chain_detail(dfs: dict, market: pd.DataFrame) -> list:
    """产业链各指数明细(波段口径): 定位主题内部哪个环节最强/最热。"""
    rows = []
    bench_c = dfs[BENCH]["close"]
    for code, name in [(MAIN, NAME[MAIN])] + CHAIN:
        df = dfs[code]
        c = df["close"]
        ma10 = c.rolling(10).mean()
        share_ma5 = (df["amount_yi"] / market["amount_yi"].reindex(df.index).ffill(limit=5)
                     ).rolling(5).mean()
        crowd = rolling_pctile(share_ma5)
        speed = rolling_pctile(c.pct_change(10))
        excess10 = c.pct_change(10) - bench_c.reindex(df.index).ffill(limit=5).pct_change(10)
        last, prev = df.iloc[-1], df.iloc[-2]
        rows.append({
            "code": code, "name": name,
            "date": df.index[-1].strftime("%m-%d"),
            "close": round(float(last["close"]), 2),
            "chg_pct": round(float(last["close"] / prev["close"] - 1) * 100, 2),
            "bias10": round(float(c.iloc[-1] / ma10.iloc[-1] - 1) * 100, 1),
            "excess10": round(float(excess10.iloc[-1]) * 100, 1),
            "above_ma10": bool(c.iloc[-1] > ma10.iloc[-1]),
            "share_pct_of_mkt": round(float(share_ma5.iloc[-1]) * 100, 2),
            "crowd_pct": round(float(crowd.iloc[-1]), 0) if pd.notna(crowd.iloc[-1]) else None,
            "speed_pct": round(float(speed.iloc[-1]), 0) if pd.notna(speed.iloc[-1]) else None,
        })
    return rows


# ---------------- 导出 ----------------

def rnd(x, n=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n)


def series_out(s: pd.Series, n=2):
    return [rnd(v, n) for v in s]


def export(ind: pd.DataFrame, chain_rows: list, val: dict, trades: list):
    last = ind.iloc[-1]
    prev = ind.iloc[-2]
    score5ago = ind["score"].iloc[-6] if len(ind) > 6 else np.nan
    temp5ago = ind["temp"].iloc[-6] if len(ind) > 6 else np.nan
    state_key = last["state"] or "neutral"

    run = 0
    for sk in reversed(ind["state"].tolist()):
        if sk == state_key:
            run += 1
        else:
            break

    open_trade = trades[-1] if trades and trades[-1]["open"] else None

    snapshot = {
        "date": ind.index[-1].strftime("%Y-%m-%d"),
        "close": rnd(last["close"]),
        "chg_pct": rnd((last["close"] / prev["close"] - 1) * 100),
        "score": rnd(last["score"], 1),
        "score_chg5": rnd(last["score"] - score5ago, 1),
        "trend": rnd(last["trend"], 0),
        "momentum": rnd(last["momentum"], 0),
        "breadth": rnd(last["breadth"], 0),
        "temp": rnd(last["temp"], 0),
        "temp_chg5": rnd(last["temp"] - temp5ago, 0),
        "crowd_pct": rnd(last["crowd_pct"], 0),
        "speed_pct": rnd(last["speed_pct"], 0),
        "share_ma5": rnd(last["share_ma5"] * 100, 2),
        "bias10": rnd(last["bias10"] * 100, 1),
        "excess10": rnd(last["excess10"] * 100, 1),
        "above_ma10": bool(last["close"] > last["ma10"]),
        "pe": rnd(last["pe"], 1),
        "pe_pct": rnd(last["pe_pct"], 0),
        "state": state_key,
        "state_label": STATES[state_key]["label"],
        "state_advice": STATES[state_key]["advice"],
        "state_run_days": run,
        "position": None if open_trade is None else {
            "entry_date": open_trade["entry_date"].strftime("%Y-%m-%d"),
            "entry_px": rnd(open_trade["entry_px"]),
            "hold_days": open_trade["hold_days"],
            "ret": rnd(open_trade["ret"] * 100, 1),
        },
    }

    view = ind.tail(CHART_DAYS)
    series = {
        "dates": [d.strftime("%Y-%m-%d") for d in view.index],
        "close": series_out(view["close"]),
        "ma10": series_out(view["ma10"]),
        "ma20": series_out(view["ma20"]),
        "score": series_out(view["score"], 1),
        "trend": series_out(view["trend"], 1),
        "momentum": series_out(view["momentum"], 1),
        "breadth": series_out(view["breadth"], 1),
        "temp": series_out(view["temp"], 1),
        "crowd_pct": series_out(view["crowd_pct"], 1),
        "speed_pct": series_out(view["speed_pct"], 1),
        "share_ma5": series_out(view["share_ma5"] * 100, 3),
        "rs": series_out(view["rs"] * 1000, 2),
        "rs_ma10": series_out(view["rs_ma10"] * 1000, 2),
        "state": [s if isinstance(s, str) else None for s in view["state"]],
    }

    first_view = view.index[0]
    markers = []
    for t in trades:
        if t["entry_date"] >= first_view:
            markers.append({"type": "entry", "date": t["entry_date"].strftime("%Y-%m-%d"),
                            "px": rnd(t["entry_px"])})
        if not t["open"] and t["exit_date"] >= first_view:
            markers.append({"type": "exit", "date": t["exit_date"].strftime("%Y-%m-%d"),
                            "px": rnd(t["exit_px"]), "reason": t["reason"]})

    recent_trades = []
    for t in trades[-12:]:
        recent_trades.append({
            "entry_date": t["entry_date"].strftime("%Y-%m-%d"),
            "exit_date": t["exit_date"].strftime("%Y-%m-%d") if t["exit_date"] is not None else None,
            "hold_days": t["hold_days"],
            "ret": rnd(t["ret"] * 100, 1),
            "reason": t["reason"],
            "open": t["open"],
        })

    payload = {
        "updated_at": now_bj().strftime("%Y-%m-%d %H:%M:%S") + " (北京时间)",
        "snapshot": snapshot,
        "series": series,
        "markers": markers,
        "trades": recent_trades,
        "swing_stats": [swing_stats(trades, "2019-01-01"), swing_stats(trades, "2022-01-01")],
        "chain": chain_rows,
        "validation": val,
        "states_def": {k: v for k, v in STATES.items()},
        "meta": {
            "main": f"{MAIN} {NAME[MAIN]}", "bench": f"{BENCH} 沪深300",
            "market": f"{MARKET} 中证全指",
            "pct_window": PCT_WINDOW, "chart_days": CHART_DAYS,
            "entry_score": ENTRY_SCORE, "entry_temp_max": ENTRY_TEMP_MAX,
            "exit_score": EXIT_SCORE,
            "hot_temp": HOT_TEMP, "exit_temp": EXIT_TEMP, "cold_temp": COLD_TEMP,
        },
    }
    js = "window.AI_MONITOR_DATA = " + json.dumps(payload, ensure_ascii=False) + ";"
    with open(os.path.join(HERE, "data.js"), "w", encoding="utf-8") as f:
        f.write(js)
    return snapshot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    print(">>> 拉取指数数据 (中证指数官网)")
    dfs = load_all(args.no_fetch)

    print(">>> 计算指标")
    ind = compute(dfs)
    chain_rows = chain_detail(dfs, dfs[MARKET])
    val = validate(ind)
    trades = swing_signals(ind)

    snap = export(ind, chain_rows, val, trades)

    print(">>> 完成. 快照:")
    print(f"  数据截至 {snap['date']}  收盘 {snap['close']} ({snap['chg_pct']:+.2f}%)")
    print(f"  方向分 {snap['score']} (趋势{snap['trend']}/动量{snap['momentum']}/广度{snap['breadth']})"
          f"  5日变化 {snap['score_chg5']:+.1f}")
    print(f"  温度 {snap['temp']} (拥挤{snap['crowd_pct']}/涨速{snap['speed_pct']})"
          f"  5日变化 {snap['temp_chg5']:+.0f}")
    print(f"  状态: {snap['state_label']} (已持续{snap['state_run_days']}个交易日)")
    if snap["position"]:
        p = snap["position"]
        print(f"  当前波段持仓: {p['entry_date']}入场({p['entry_px']}), "
              f"已持有{p['hold_days']}日, 浮动{p['ret']:+.1f}%")
    else:
        print("  当前无波段持仓信号")

    print("\n>>> 波段信号回测 (信号日收盘成交):")
    for since in ("2019-01-01", "2022-01-01"):
        st = swing_stats(trades, since)
        if st:
            print(f"  {st['since']}年来: {st['n']}笔, 胜率{st['win_rate']}%, "
                  f"单笔中位{st['ret_med']}%/均值{st['ret_avg']}%, "
                  f"持有中位{st['hold_med']}日, 最差{st['worst']}%, 最好{st['best']}%, "
                  f"累计(对数){st['ret_sum']}%")

    print("\n>>> 状态分档 (2019年来, 未来10日):")
    for row in val["per_state"]:
        print(f"  {row['label']:<12s} 样本{row['days']:>5}天  收益中位 {row['fwd10_ret_med']:>5}%"
              f"  回撤中位 {row['fwd10_dd_med']:>5}%  胜率 {row['win_rate']:>4}%")


if __name__ == "__main__":
    main()
