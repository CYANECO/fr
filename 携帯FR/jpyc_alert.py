"""JPYC が割安(1JPYC が THRESHOLD 円以下)になったら、ntfy で携帯に知らせる。
GitHub Actions(fr-page.yml)で10分ごとに動かす。手元で試すとき:
  python 携帯FR/jpyc_alert.py          … 値段を見るだけ(NTFY_TOPIC がなければ送らない)
  python 携帯FR/jpyc_alert.py --test   … 値段に関係なくテスト通知を送る
合言葉(ntfy のチャンネル名)は環境変数 NTFY_TOPIC(GitHub の Secrets)に入れ、ファイルには書かない。
値段: DexScreener の JPYC(新しいほう)の USDC ペアのうち中身がいちばん多いもの(ドル建て)×ドル円(Yahoo、だめなら open.er-api)。
"""
import json
import os
import sys
import time

import requests

try:
    import truststore  # このPCはセキュリティソフトがSSLを中継するので必要(Actionsにはないので無くてもよい)
    truststore.inject_into_ssl()
except ImportError:
    pass

THRESHOLD = 0.995           # この値段(円)以下で知らせる
REPEAT_SEC = 2 * 3600       # 割安が続くときは2時間おきに知らせる(さらに下がったらすぐ)
DROP_MORE = 0.002           # 前回知らせた値段からこれだけ下がったら、2時間たっていなくても知らせる
JPYC = "0xe7c3d8c9a439fede00d2600032d5db0be71c3c29"  # 新しいJPYC(イーサリアム・Polygon・Avalanche 共通)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jpyc_state.json")
UA = {"User-Agent": "Mozilla/5.0"}


def jpyc_usd():
    """(ドル建ての値段, 取引所名, ページのURL)。中身(流動性)がいちばん多い USDC ペアを使う。"""
    pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{JPYC}", timeout=15).json()["pairs"]
    pairs = [p for p in pairs if p["baseToken"]["address"].lower() == JPYC
             and p["quoteToken"]["symbol"] == "USDC" and p.get("priceUsd")]
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))
    return float(best["priceUsd"]), f"{best['chainId']} {best['dexId']}", best["url"]


def usd_jpy():
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/JPY=X",
                         params={"interval": "1m", "range": "1d"}, headers=UA, timeout=15)
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]), "Yahoo"
    except Exception:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        return float(r.json()["rates"]["JPY"]), "open.er-api(1日1回更新)"


def send(topic, title, msg, url):
    requests.post("https://ntfy.sh/", timeout=15, json={
        "topic": topic, "title": title, "message": msg, "click": url, "priority": 4, "tags": ["yen"]})


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    usd, venue, url = jpyc_usd()
    fx, fx_src = usd_jpy()
    yen = usd * fx
    msg = (f"1 JPYC = {yen:.4f}円（{(yen - 1) * 100:+.2f}%）\n"
           f"{usd:.6f}ドル × ドル円 {fx:.2f}（{fx_src}）\n{venue}")
    print(msg)
    if "--test" in sys.argv:
        if topic:
            send(topic, "JPYC見張り：テスト通知", msg, url)
            print("テスト通知を送りました")
        return
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    now = time.time()
    if yen > THRESHOLD:
        st = {}  # 割安が終わった → 次に割安になったらすぐ知らせる
    elif (now - st.get("t", 0) >= REPEAT_SEC or yen <= st.get("yen", 9) - DROP_MORE):
        if topic:
            send(topic, f"JPYCが割安：{yen:.4f}円", msg, url)
            print("通知しました")
        st = {"t": now, "yen": yen}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


if __name__ == "__main__":
    main()
