"""携帯用のFR差ページ(index.html)を作る。GitHub Actions で数分ごとに動かして公開する。
dex_fr_diff.py のデータ取得をそのまま使う。使い方: python 携帯FR/make_page.py [出力フォルダ]
取引所の絞り込みや並べ替えは、携帯の画面上(JavaScript)で行う。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "site")
sys.argv = sys.argv[:1]  # dex_fr_diff は引数を表示件数として読むので渡さない
import dex_fr_diff as core  # noqa: E402

JST = timezone(timedelta(hours=9))

TRADE_URLS = {  # FR差モニター.pyw と同じ
    "Phoenix": "https://www.phoenix.trade/{sym}",
    "RISEx": "https://www.rise.trade/trade/{sym}",
    "Meridian": "https://app.meridian.xyz/{sym}USD",
    "Variational": "https://omni.variational.io/perpetual/{sym}",
    "Arcus": "https://app.arcus.xyz/trade/perpetuals/{sym}-USD",
    "GRVT": "https://grvt.io/exchange/perpetual/{sym}-USDT",
    "MEXC": "https://www.mexc.com/futures/{sym}_USDT",
}


def main():
    logs = []
    data = core.fetch_all(lambda msg: (print(msg, flush=True), logs.append(msg)))
    try:
        ranks = core.market_cap_ranks()
    except Exception as e:  # CoinGeckoは制限にかかりやすい。順位なしでも表示はする
        print(f"時価総額順位: 取得失敗 ({e})")
        ranks = {}
    syms = {s for d in data.values() for s in d}
    payload = {
        "time": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "epoch": int(time.time()),
        "data": {ex: {s: [round(fr, 4), px] for s, (fr, px) in d.items()} for ex, d in data.items()},
        "failed": [m.split(":")[0] for m in logs if "取得失敗" in m],
        "nonCrypto": sorted(s for s in syms if not core.is_crypto(s)),
        "ranks": {s: r for s in syms if (r := core.rank_of(s, ranks))},
        "urls": {ex: {s: u.format(sym=core.orig_name(ex, s)) for s in data.get(ex, {})}
                 for ex, u in TRADE_URLS.items()},
    }
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        html = f.read().replace("/*DATA*/null", json.dumps(payload, ensure_ascii=False))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"作成: {os.path.join(OUT_DIR, 'index.html')}")


if __name__ == "__main__":
    main()
