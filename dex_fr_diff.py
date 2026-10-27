"""DeFi系perp DEX(Phoenix / RISEx / Meridian / Variational / Arcus / GRVT / SoDEX)とMEXCのFR差と価格差を抽出する。
APIキー不要(公開API)。使い方: python dex_fr_diff.py [表示件数]
FRは「年率%」に揃えて比較する(精算間隔が取引所ごとに違うため)。
"""
import json
import os
import re
import sys
import time

import requests

try:  # Windowsの証明書ストアを使う(セキュリティソフト経由の通信対策)
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
HOURS_PER_YEAR = 24 * 365

PHOENIX = "https://perp-api.phoenix.trade"
RISEX = "https://api.rise.trade"
MERIDIAN = "https://api.meridian.xyz"
VARIATIONAL = "https://omni-client-api.prod.ap-northeast-1.variational.io"
MEXC = "https://contract.mexc.com"
ARCUS = "https://api.arcus.xyz"
GRVT = "https://market-data.grvt.io"
SODEX = "https://mainnet-gw.sodex.dev"
SODEX_BIZ = "https://alpha-biz.sodex.dev"

# 株・ETF・商品(金や原油など)の銘柄。取引所の情報から集める。時価総額順位を付けないために使う
NON_CRYPTO = set()
# 同じものを別の名前で扱う取引所があるので、名前をそろえる(左→右)
ALIASES = {"GOLD": "XAU", "SILVER": "XAG"}
# 名前をそろえたときの、取引所での元の名前 {取引所名: {そろえた名前: 元の名前}}(履歴の取得で使う)
ORIG_NAMES = {}


def orig_name(venue, sym):
    return ORIG_NAMES.get(venue, {}).get(sym, sym)


def get(url, **params):
    for wait in (1, 2, 4, 8, 16):  # 429(アクセス制限)のときは待って再試行
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 429:
            break
        time.sleep(wait)
    r.raise_for_status()
    return r.json()


def post(url, body):
    """POSTで問い合わせる取引所用(GRVT)。429のときは待って再試行。"""
    for wait in (1, 2, 4, 8, 16):
        r = requests.post(url, json=body, timeout=15)
        if r.status_code != 429:
            break
        time.sleep(wait)
    r.raise_for_status()
    return r.json()


def phoenix():
    """WebSocketの market チャンネルで全銘柄を一度に受け取る(RESTは銘柄ごと・アクセス制限ありで遅い)。
    FR: funding は1時間ごとの率(%)。価格: markPx。"""
    import ssl

    import websocket  # pip install websocket-client

    markets = [m for m in get(f"{PHOENIX}/v1/view/exchange/markets")
               if m.get("marketStatus") == "active"]
    symbols = [m["symbol"] for m in markets]
    NON_CRYPTO.update(m["symbol"].upper() for m in markets
                      if (m.get("commodityMetadata") or {}).get("isCommodity"))
    # kPEPE のように小文字が混ざる銘柄は、取引画面を開くときに元の名前が必要
    ORIG_NAMES.setdefault("Phoenix", {}).update(
        {s.upper(): s for s in symbols if s != s.upper()})
    sslopt = {"context": truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)} if "truststore" in globals() else {}
    ws = websocket.create_connection(PHOENIX.replace("https", "wss") + "/v1/ws",
                                     timeout=10, sslopt=sslopt)
    out = {}
    try:
        for sym in symbols:
            ws.send(json.dumps({"type": "subscribe",
                                "subscription": {"channel": "market", "symbol": sym}}))
        deadline = time.time() + 15
        while len(out) < len(symbols) and time.time() < deadline:
            m = json.loads(ws.recv())
            if m.get("channel") == "market" and m.get("markPx") is not None:
                out[m["symbol"].upper()] = (float(m["funding"]) * HOURS_PER_YEAR, float(m["markPx"]))
    finally:
        ws.close()
    return out


def risex():
    """FR: current_funding_rate は1時間ごとの率(小数)。"""
    out = {}
    for m in get(f"{RISEX}/v1/markets")["data"]["markets"]:
        if not m.get("active"):
            continue
        sym = m["display_name"].split("/")[0].upper()
        if m.get("category") and m["category"] != "crypto":
            NON_CRYPTO.add(sym)
        out[sym] = (float(m["current_funding_rate"]) * 100 * HOURS_PER_YEAR,
                    float(m["mark_price"]))
    return out


def meridian():
    """FR: fundingRate1h は1時間ごとの率(小数)。価格: market-price の oraclePrice。"""
    prods = [p for p in get(f"{MERIDIAN}/v1/product", limit=100)["data"]
             if p.get("status") == "ACTIVE"]
    ids = [p["id"] for p in prods][:50]
    prices = {x["productId"]: float(x["oraclePrice"])
              for x in get(f"{MERIDIAN}/v1/product/market-price", productIds=ids)["data"]}
    return {p["baseTokenName"].upper(): (float(p["fundingRate1h"]) * 100 * HOURS_PER_YEAR,
                                         prices[p["id"]])
            for p in prods if p["id"] in prices}


def variational():
    """FR: funding_rate は年率(小数)。"""
    out = {}
    for d in get(f"{VARIATIONAL}/metadata/stats")["listings"]:
        out[d["ticker"].upper()] = (float(d["funding_rate"]) * 100, float(d["mark_price"]))
    return out


def arcus():
    """FR: fundingRate は1時間ごとの率(小数)。価格: markPrice。"""
    out = {}
    for m in get(f"{ARCUS}/v1/markets")["markets"]:
        if m.get("status") != "ONLINE" or m.get("type") != "PERPETUAL":
            continue
        sym = m["baseAsset"].upper()
        if m.get("category") != "CRYPTO":
            NON_CRYPTO.add(sym)
        out[sym] = (float(m["fundingRate"]) * 100 * HOURS_PER_YEAR, float(m["markPrice"]))
    return out


def grvt():
    """FR: funding_rate は精算1回あたりの率(%)、funding_interval_hours が精算間隔(時間)。
    価格: mark_price。価格とFRは銘柄ごとに問い合わせる必要があるので、並行して取る。"""
    from concurrent.futures import ThreadPoolExecutor

    insts = [i for i in post(f"{GRVT}/full/v1/all_instruments",
                             {"kind": ["PERPETUAL"], "is_active": True})["result"]
             if i.get("status") == "ACTIVE" and i.get("funding_interval_hours")]

    def one(inst):
        try:
            return inst, post(f"{GRVT}/full/v1/ticker", {"instrument": inst["instrument"]})["result"]
        except Exception:
            return inst, None

    out = {}
    with ThreadPoolExecutor(8) as pool:
        for inst, t in pool.map(one, insts):
            if not t or not t.get("mark_price"):
                continue
            sym = inst["base"].upper()
            if inst.get("asset_class") != "CRYPTO":
                NON_CRYPTO.add(sym)
            out[sym] = (float(t["funding_rate"]) * HOURS_PER_YEAR / inst["funding_interval_hours"],
                        float(t["mark_price"]))
    return out


def sodex():
    """FR: fundingRate は精算1回あたりの率(小数)、fundingInterval が精算間隔(秒。今は全銘柄3600)。
    価格: markPrice。株・指数・商品は biz/futures/symbols の tags で見分ける。"""
    syms = {m["name"]: m for m in get(f"{SODEX}/api/v1/perps/markets/symbols")["data"]
            if m.get("status") == "TRADING"}
    try:
        for m in get(f"{SODEX_BIZ}/biz/futures/symbols")["data"]:
            if set(m.get("tags") or []) & {"stocks", "indexes", "commodities"}:
                sym = m["baseCoin"].upper()
                NON_CRYPTO.add(ALIASES.get(sym, sym))  # SILVER は XAG にそろえられるので
    except Exception:  # 種類が取れなくてもFRは表示する
        pass
    out = {}
    for d in get(f"{SODEX}/api/v1/perps/markets/mark-prices")["data"]:
        m = syms.get(d["symbol"])
        if not m or not d.get("markPrice"):
            continue
        out[m["baseCoin"].upper()] = (float(d["fundingRate"]) * 100 * HOURS_PER_YEAR * 3600
                                      / (m.get("fundingInterval") or 3600),
                                      float(d["markPrice"]))
    return out


def mexc():
    """CEX。FR: fundingRate は精算1回あたりの率(小数)、collectCycle が精算間隔(時間)。
    価格: fairPrice(MEXCのマーク価格)。"""
    out = {}
    for d in get(f"{MEXC}/api/v1/contract/funding_rate")["data"]:
        base, _, quote = d["symbol"].partition("_")
        if quote != "USDT" or not d.get("collectCycle"):
            continue
        out[base.upper()] = (float(d["fundingRate"]) * 100 * HOURS_PER_YEAR / d["collectCycle"],
                             float(d["fairPrice"]))
    # MEXCの株は「AAPLSTOCK」のような名前なので、ほかの取引所と同じ「AAPL」にそろえる。
    # ただし同じ名前の仮想通貨がMEXCにある場合(DASH など)は、取り違えないようそのままにする
    names = ORIG_NAMES.setdefault("MEXC", {})
    for sym in [s for s in out if s.endswith("STOCK") and len(s) > 5]:
        base = sym[:-5]
        if base not in out:
            out[base] = out.pop(sym)
            names[base] = sym
            NON_CRYPTO.add(base)
    return out


VENUES = {"Phoenix": phoenix, "RISEx": risex, "Meridian": meridian, "Arcus": arcus, "GRVT": grvt,
          "Variational": variational, "SoDEX": sodex, "MEXC": mexc}


def fetch_all(log=print):
    """全取引所から取得する。戻り値: {取引所名: {銘柄: (FR年率%, 価格)}}"""
    data = {}
    for name, fn in VENUES.items():
        log(f"{name}: 取得中...")
        try:
            d = fn()
            for old, new in ALIASES.items():
                if old in d and new not in d:
                    d[new] = d.pop(old)
                    ORIG_NAMES.setdefault(name, {})[new] = ORIG_NAMES.get(name, {}).get(old, old)
            data[name] = d
            log(f"{name}: {len(data[name])}銘柄 取得")
        except Exception as e:
            log(f"{name}: 取得失敗 ({e})")
    return data


RANK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcap_rank.json")
RANK_MAX_AGE = 6 * 3600  # 時価総額順位は6時間に1回だけ取り直す(CoinGeckoの無料枠は回数制限があるため)


def market_cap_ranks():
    """CoinGeckoの時価総額順位(上位1000)。戻り値: {銘柄(大文字): 順位}"""
    try:
        if time.time() - os.path.getmtime(RANK_CACHE) < RANK_MAX_AGE:
            with open(RANK_CACHE, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    ranks = {}
    for page in (1, 2, 3, 4):
        if page > 1:
            time.sleep(3)  # 連続で取ると制限にかかりやすい
        try:
            coins = get("https://api.coingecko.com/api/v3/coins/markets", vs_currency="usd",
                        order="market_cap_desc", per_page=250, page=page)
        except Exception:
            if not ranks:
                raise
            return ranks  # 途中まで取れた分を使う(保存はせず、次回また取り直す)
        for c in coins:
            sym, rank = c["symbol"].upper(), c.get("market_cap_rank")
            if rank and rank < ranks.get(sym, 10**9):  # 同じ記号が複数あれば上位の方
                ranks[sym] = rank
    with open(RANK_CACHE, "w", encoding="utf-8") as f:
        json.dump(ranks, f)
    return ranks


def rank_of(sym, ranks):
    """取引所の銘柄名から順位を探す。1000PEPE・KPEPE(1000倍単位)なども元の銘柄で探す。"""
    if sym in NON_CRYPTO:
        return None
    if sym in ranks:
        return ranks[sym]
    m = re.match(r"(?:1000000|10000|1000|1M|K)(.+)", sym)
    return ranks.get(m.group(1)) if m else None


def is_crypto(sym):
    return sym not in NON_CRYPTO


def group_by_symbol(data, venues=None):
    """銘柄ごとに、比べられる取引所の値をまとめる。戻り値: {銘柄: [(FR年率%, 取引所名, 価格), ...]}
    2つ以上の取引所にある銘柄だけ。venues で使う取引所を絞れる。"""
    by_symbol = {}
    for ex, d in data.items():
        if venues is not None and ex not in venues:
            continue
        for sym, (fr, px) in d.items():
            by_symbol.setdefault(sym, []).append((fr, ex, px))

    out = {}
    for sym, items in by_symbol.items():
        if len(items) < 2:
            continue
        # 同じ名前でも中身が違う銘柄(株と仮想通貨など)を比べないよう、
        # 価格が真ん中の値から±20%以上ずれている取引所は外す
        mid = sorted(i[2] for i in items)[len(items) // 2]
        items = [i for i in items if mid and abs(i[2] / mid - 1) <= 0.2]
        if len(items) >= 2:
            out[sym] = sorted(items)
    return out


def build_rows(data, venues=None):
    """FR差の大きい順に並べる。各行: (FR差, 銘柄, ショート側, ロング側, 価格差%)
    ショート側/ロング側は (FR年率%, 取引所名, 価格)。venues で使う取引所を絞れる。"""
    rows = []
    for sym, items in group_by_symbol(data, venues).items():
        lo, hi = items[0], items[-1]
        px_diff = (hi[2] - lo[2]) / lo[2] * 100 if lo[2] else 0
        rows.append((hi[0] - lo[0], sym, hi, lo, px_diff))
    rows.sort(reverse=True)
    return rows


def main():
    rows = build_rows(fetch_all(lambda msg: print(msg, flush=True)))

    print(f"\nFR差(年率) 上位{TOP_N}件  ショート=FR高い側 / ロング=FR低い側")
    print("価格差 = ショート側の価格がロング側より何%高いか(プラスなら価格面も有利)")
    print(f"{'銘柄':<12}{'FR差(年率)':>11}  {'ショート側':<34}{'ロング側':<34}{'価格差':>7}")
    for diff, sym, hi, lo, pd_ in rows[:TOP_N]:
        print(f"{sym:<12}{diff:>10.1f}%  "
              f"{hi[1]:<11}{hi[0]:>8.1f}% @{hi[2]:<11.6g}"
              f"{lo[1]:<11}{lo[0]:>8.1f}% @{lo[2]:<11.6g}"
              f"{pd_:>6.2f}%")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"エラーが発生しました: {e}")
    try:  # ダブルクリック起動でも画面がすぐ閉じないようにする
        input("\nEnterキーで閉じます...")
    except EOFError:
        pass
