"""FRの過去の推移を集める。
- MEXC / Phoenix / Meridian / Arcus / GRVT: 取引所の履歴APIから取得(Phoenixは約7日分まで)
- RISEx / Variational / SoDEX: 履歴APIが見つからないため、FR差モニターが更新のたびに
  このフォルダの fr_history.db に記録した分を使う(モニターを動かしている間だけたまる)
FRはすべて「年率%」。時刻はUNIX秒。
"""
import itertools
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import dex_fr_diff as core

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fr_history.db")
API_VENUES = ("MEXC", "Phoenix", "Meridian", "Arcus", "GRVT")       # 履歴APIあり
LOCAL_VENUES = ("RISEx", "Variational", "SoDEX")   # 自前で記録
RECORD_EVERY = 10 * 60   # 記録の最短間隔(秒)。自動更新が短くてもDBが膨らみすぎないように
KEEP_DAYS = 14           # これより古い記録は消す


# ---------- 自前の記録(RISEx / Variational / SoDEX) ----------
def _db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS fr (ts INTEGER, venue TEXT, sym TEXT, fr REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS fr_sym ON fr (sym, venue, ts)")
    return con


def record(data):
    """fetch_all の結果を記録する。前回の記録から RECORD_EVERY 経っていなければ何もしない。"""
    now = int(time.time())
    con = _db()
    try:
        with con:
            for venue in LOCAL_VENUES:
                d = data.get(venue)
                if not d:
                    continue
                last = con.execute("SELECT MAX(ts) FROM fr WHERE venue=?", (venue,)).fetchone()[0]
                if last and now - last < RECORD_EVERY:
                    continue
                con.executemany("INSERT INTO fr VALUES (?,?,?,?)",
                                [(now, venue, sym, fr) for sym, (fr, _px) in d.items()])
            con.execute("DELETE FROM fr WHERE ts < ?", (now - KEEP_DAYS * 86400,))
    finally:
        con.close()


def _local(venue, sym, since):
    if not os.path.exists(DB_PATH):
        return []
    con = _db()
    try:
        return con.execute("SELECT ts, fr FROM fr WHERE sym=? AND venue=? AND ts>=? ORDER BY ts",
                           (sym, venue, since)).fetchall()
    finally:
        con.close()


# ---------- 履歴API ----------
def _mexc(sym, since):
    out = []
    for page in range(1, 20):
        d = core.get(f"{core.MEXC}/api/v1/contract/funding_rate/history",
                     symbol=f"{sym}_USDT", page_num=page, page_size=100)["data"]
        for x in d["resultList"]:
            t = x["settleTime"] // 1000
            if t >= since and x.get("collectCycle"):
                out.append((t, float(x["fundingRate"]) * 100 * core.HOURS_PER_YEAR / x["collectCycle"]))
        rows = d["resultList"]
        if not rows or rows[-1]["settleTime"] // 1000 < since or page >= d["totalPage"]:
            break
    return sorted(out)


_phoenix_names = {}


def _phoenix(sym, since):
    """fundingRatePercentage は1時間あたりの%。"""
    if not _phoenix_names:
        for m in core.get(f"{core.PHOENIX}/v1/view/exchange/markets"):
            _phoenix_names[m["symbol"].upper()] = m["symbol"]
    name = _phoenix_names.get(sym, sym)
    rates = core.get(f"{core.PHOENIX}/v1/funding/{name}/rates")["rates"]
    return [(r["timestamp"], float(r["fundingRatePercentage"]) * core.HOURS_PER_YEAR)
            for r in rates if r["timestamp"] >= since]


_meridian_ids = {}


def _meridian(sym, since):
    """fundingRate1h は1時間あたりの率(小数)。1回200件までなので続きをたどる。"""
    if not _meridian_ids:
        for p in core.get(f"{core.MERIDIAN}/v1/product", limit=100)["data"]:
            _meridian_ids[p["baseTokenName"].upper()] = p["id"]
    pid = _meridian_ids.get(sym)
    if not pid:
        return []
    out, cursor = [], None
    params = dict(productId=pid, limit=200, order="asc",
                  startTime=since * 1000, endTime=int(time.time() * 1000))
    for _ in range(20):
        if cursor:
            params["cursor"] = cursor
        d = core.get(f"{core.MERIDIAN}/v1/funding", **params)
        out += [(x["chargedAt"] // 1000, float(x["fundingRate1h"]) * 100 * core.HOURS_PER_YEAR)
                for x in d["data"]]
        cursor = d.get("nextCursor")
        if not d.get("hasNext") or not cursor:
            break
    return out


def _arcus(sym, since):
    """fundingRate は1時間あたりの率(小数)。時刻はマイクロ秒。"""
    d = core.get(f"{core.ARCUS}/v1/fundingRates", market=f"{sym}-USD", limit=1000,
                 **{"from": since * 10**6, "to": int(time.time()) * 10**6})
    return sorted((x["time"] // 10**6, float(x["fundingRate"]) * 100 * core.HOURS_PER_YEAR)
                  for x in d["fundingRates"])


def _grvt(name, since):
    """name は「BTC_USDT_Perp」の形。funding_rate は精算1回あたりの%。時刻はナノ秒。"""
    if "_" not in name:
        name = f"{name}_USDT_Perp"
    body = {"instrument": name, "limit": 1000,
            "start_time": str(since * 10**9), "end_time": str(int(time.time()) * 10**9)}
    out = []
    for _ in range(10):
        d = core.post(f"{core.GRVT}/full/v1/funding", body)
        out += [(int(x["funding_time"]) // 10**9,
                 float(x["funding_rate"]) * core.HOURS_PER_YEAR / x["funding_interval_hours"])
                for x in d["result"] if x.get("funding_interval_hours")]
        if not d.get("next") or not d["result"]:
            break
        body["cursor"] = d["next"]
    return sorted(set(out))


API_FUNCS = {"MEXC": _mexc, "Phoenix": _phoenix, "Meridian": _meridian, "Arcus": _arcus,
             "GRVT": _grvt}


def get_history(sym, venues, days=10, log=print):
    """戻り値: {取引所名: [(UNIX秒, FR年率%), ...]}  データがない取引所は入らない。"""
    since = int(time.time()) - days * 86400
    out = {}
    for venue in venues:
        try:
            if venue in API_FUNCS:  # 取引所での元の名前で問い合わせる(MEXCの AAPLSTOCK など)
                pts = API_FUNCS[venue](core.orig_name(venue, sym), since)
            else:
                pts = _local(venue, sym, since)
        except Exception as e:
            log(f"{venue}: 履歴の取得失敗 ({e})")
            continue
        if pts:
            out[venue] = pts
    return out


# ---------- 継続的にFR差が出ている銘柄を探す ----------
CACHE_TTL = 60 * 60          # 取得した履歴は1時間使い回す(何度も開いても速いように)
WORKERS = {"MEXC": 4, "Arcus": 4, "GRVT": 4, "Phoenix": 1, "Meridian": 1}  # 同時に問い合わせる数(Phoenixは制限が厳しい)
STALE = 12 * 3600            # これより古い値はその時刻のFRとみなさない(記録の抜けを埋めすぎない)
_cache, _cache_lock = {}, threading.Lock()


def _cached_history(venue, sym, days):
    key = (venue, sym)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL and hit[1] >= days:
        return hit[2]
    since = int(time.time()) - days * 86400
    if venue in API_FUNCS:
        pts = API_FUNCS[venue](core.orig_name(venue, sym), since)
    else:
        pts = _local(venue, sym, since)
    with _cache_lock:
        _cache[key] = (time.time(), days, pts)
    return pts


def _hourly(pts, hours):
    """1時間ごとの値にそろえる(その時刻の直前に決まったFRを使う)。戻り値: {時刻: FR}"""
    out, i, last = {}, 0, None
    for h in hours:
        while i < len(pts) and pts[i][0] <= h:
            last = pts[i]
            i += 1
        if last and h - last[0] <= STALE:
            out[h] = last[1]
    return out


def persistent_pairs(data, venues, days=3, log=print, stop=None, all_pairs=False):
    """銘柄ごとに、期間中ずっとFR差が出ていた取引所の組み合わせを探す。
    all_pairs=True なら、いちばん良い組み合わせだけでなく条件を満たす組み合わせをすべて返す(携帯版で使う)。
    戻り値: 行のリスト。各行は dict:
      sym, short, long(取引所名), avg(平均FR差 年率%), keep(ショート側が高かった時間の割合 %),
      low(FR差の下位25% 年率%), now(今のFR差), hours(比べられた時間数), cover(期間に対するデータの割合 %)"""
    groups = core.group_by_symbol(data, venues)
    jobs = {}
    for sym, items in groups.items():
        for _fr, ex, _px in items:
            jobs.setdefault(ex, []).append(sym)

    hist, done, total = {}, [0], sum(len(v) for v in jobs.values())
    lock = threading.Lock()

    def fetch(ex, sym):
        if stop and stop.is_set():
            return
        try:
            pts = _cached_history(ex, sym, days)
        except Exception:
            pts = []
        with lock:
            hist[(ex, sym)] = pts
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == total:
                log(f"履歴を取得中... {done[0]}/{total}")

    threads = []
    for ex, syms in jobs.items():  # 取引所ごとに並行して取得
        def run(ex=ex, syms=syms):
            with ThreadPoolExecutor(WORKERS.get(ex, 1)) as pool:
                list(pool.map(lambda s: fetch(ex, s), syms))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    now = int(time.time()) // 3600 * 3600
    hours = list(range(now - days * 86400 + 3600, now + 1, 3600))
    rows = []
    for sym, items in groups.items():
        series = {ex: _hourly(hist.get((ex, sym)) or [], hours) for _fr, ex, _px in items}
        current = {ex: fr for fr, ex, _px in items}
        best = None
        for a, b in itertools.combinations(series, 2):
            common = [h for h in hours if h in series[a] and h in series[b]]
            if len(common) < max(6, len(hours) * 0.6):  # 期間の6割以上そろっていないと判断しない
                continue
            diffs = [series[a][h] - series[b][h] for h in common]
            avg = sum(diffs) / len(diffs)
            if avg < 0:
                a, b, diffs, avg = b, a, [-d for d in diffs], -avg
            diffs.sort()
            row = dict(sym=sym, short=a, long=b, avg=avg,
                       keep=sum(d > 0 for d in diffs) / len(diffs) * 100,
                       low=diffs[len(diffs) // 4], now=current[a] - current[b],
                       hours=len(common), cover=len(common) / len(hours) * 100)
            if all_pairs:
                rows.append(row)
            elif best is None or row["avg"] > best["avg"]:
                best = row
        if best:
            rows.append(best)
    rows.sort(key=lambda r: r["avg"], reverse=True)
    return rows
