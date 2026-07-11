#!/usr/bin/env python3
"""
新宿区 生涯学習館 空き状況モニター → Discord Webhook 通知ツール

条件: 土日祝で、同じ部屋の「午後」と「夜間」が両方空いているコマを検知して通知。
一度予約で埋まった後にキャンセル等で再度空いた場合も、条件が
「不成立 → 成立」に戻った時点で再通知します。

使い方:
    python monitor.py                # 1回チェック（差分があれば通知）
    python monitor.py --loop         # 常駐モード（config の check_interval_min 間隔）
    python monitor.py --debug        # スクリーンショット/HTML/XHRログを debug/ に保存
    python monitor.py --notify-all   # 現在条件を満たすコマを全部通知（動作確認用）
    python monitor.py --dry-run      # Discordに送信せずコンソール出力のみ
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import jpholiday
import requests
import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "state.json"
DEBUG_DIR = BASE_DIR / "debug"

BASE_URL = "https://www.shinjuku.eprs.jp/regasu/web/"

SYMBOL_MAP = {
    "○": "available", "◯": "available", "〇": "available",
    "△": "partially",
    "×": "full", "✕": "full", "✖": "full",
    "－": "closed", "-": "closed", "休": "closed", "保": "maintenance",
}
AVAILABLE_STATES = {"available", "partially"}
TIME_SLOT_WORDS = ("午前", "午後", "夜間")
WEEKDAY_JA = "月火水木金土日"


# ---------------------------------------------------------------- config

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("required_slots", ["午後", "夜間"])
    cfg.setdefault("target_days", "weekend_holiday")
    cfg.setdefault("check_interval_min", 5)
    cfg.setdefault("active_hours", [7, 23])
    if not cfg.get("discord_webhook_url"):
        print("[WARN] config.yaml の discord_webhook_url が未設定です。--dry-run 以外では通知できません。")
    return cfg


# ---------------------------------------------------------------- scraping

def dump_debug(page, tag: str, xhr_log: list):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_{tag}.png"), full_page=True)
    except Exception:
        pass
    try:
        (DEBUG_DIR / f"{ts}_{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    if xhr_log:
        (DEBUG_DIR / f"{ts}_{tag}_xhr.json").write_text(
            json.dumps(xhr_log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DEBUG] debug/ に {tag} のスクリーンショット・HTML・XHRログを保存しました")


def try_click_text(page, texts, timeout=4000) -> bool:
    for t in texts:
        for locator in (
            page.get_by_role("button", name=t),
            page.get_by_role("link", name=t),
            page.get_by_text(t, exact=True),
            page.get_by_text(t),
        ):
            try:
                locator.first.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


def extract_tables(page) -> list:
    """ページ内の全 <table> を汎用的に (ヘッダ, 行ラベル, セル) として抽出。"""
    return page.evaluate(
        """() => {
            const tables = [];
            document.querySelectorAll('table').forEach(tbl => {
                const headers = [];
                const headRow = tbl.querySelector('thead tr') || tbl.querySelector('tr');
                if (headRow) headRow.querySelectorAll('th,td').forEach(c => headers.push(c.innerText.trim()));
                const rows = [];
                const bodyRows = tbl.querySelectorAll('tbody tr');
                const targetRows = bodyRows.length ? bodyRows : tbl.querySelectorAll('tr');
                targetRows.forEach((tr, i) => {
                    if (!tbl.querySelector('thead') && i === 0) return;
                    const cells = [];
                    tr.querySelectorAll('th,td').forEach(c => cells.push(c.innerText.trim()));
                    if (cells.length > 1) rows.push({ label: cells[0], cells: cells.slice(1) });
                });
                if (rows.length) tables.push({ headers, rows });
            });
            return tables;
        }"""
    )


def parse_raw_slots(tables: list, facility: str) -> dict:
    """{ "施設|行ラベル|列ヘッダ": state } の生データを作る。"""
    slots = {}
    for tbl in tables:
        headers = tbl["headers"]
        for row in tbl["rows"]:
            label = re.sub(r"\s+", " ", row["label"])
            for i, cell in enumerate(row["cells"]):
                sym = cell.strip()[:1] if cell.strip() else ""
                if sym not in SYMBOL_MAP:
                    continue
                col = headers[i + 1] if i + 1 < len(headers) else f"col{i}"
                col = re.sub(r"\s+", " ", col)
                slots[f"{facility}|{label}|{col}"] = SYMBOL_MAP[sym]
    return slots


def fetch_availability(cfg: dict, debug: bool) -> dict:
    all_slots = {}
    xhr_log = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP", viewport={"width": 1400, "height": 1000},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        )
        page = context.new_page()

        def on_response(resp):
            try:
                if "json" in resp.headers.get("content-type", "") and len(xhr_log) < 50:
                    xhr_log.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass

        if debug:
            page.on("response", on_response)

        print(f"[INFO] {BASE_URL} を開いています…")
        page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        time.sleep(2)
        if debug:
            dump_debug(page, "home", xhr_log)

        for facility in cfg["facilities"]:
            print(f"[INFO] {facility} の空き状況を取得中…")
            try:
                page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
                time.sleep(1.5)
                try_click_text(page, [cfg.get("period", "1か月")])
                time.sleep(0.5)
                try_click_text(page, ["どこで", "施設", "施設で探す"])
                time.sleep(0.8)
                if not try_click_text(page, [facility]):
                    print(f"[WARN] 施設名「{facility}」が見つかりません。")
                    if debug:
                        dump_debug(page, f"notfound_{facility}", xhr_log)
                    continue
                time.sleep(0.5)
                try_click_text(page, ["検索", "空き状況を見る", "この条件で検索"])
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PWTimeout:
                    pass
                time.sleep(2)
                if debug:
                    dump_debug(page, f"result_{facility}", xhr_log)

                slots = parse_raw_slots(extract_tables(page), facility)
                if not slots:
                    print(f"[WARN] {facility}: テーブルを解析できませんでした。--debug のHTMLを確認してください。")
                else:
                    print(f"[INFO] {facility}: {len(slots)} コマ取得")
                all_slots.update(slots)
            except Exception as e:
                print(f"[ERROR] {facility} の取得中にエラー: {e}")
                if debug:
                    dump_debug(page, f"error_{facility}", xhr_log)
            time.sleep(cfg.get("per_facility_wait_sec", 3))
        browser.close()
    return all_slots


# ---------------------------------------------------------------- 条件判定

def parse_date_from_text(text: str):
    m = re.search(r"(20\d{2})[/年.\-](\d{1,2})[/月.\-](\d{1,2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})[/月](\d{1,2})", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        today = date.today()
        year = today.year if month >= today.month else today.year + 1
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def find_slot_word(*texts):
    for t in texts:
        for w in TIME_SLOT_WORDS:
            if w in t:
                return w
    return None


def is_target_day(d, day_text: str) -> bool:
    """土日祝かどうか。日付が取れた場合はカレンダー判定、無理なら表記の(土)(日)(祝)で判定。"""
    if d is not None:
        return d.weekday() >= 5 or jpholiday.is_holiday(d)
    return bool(re.search(r"[（(]\s*(土|日|祝)|土曜|日曜|祝日", day_text))


def build_groups(raw_slots: dict) -> dict:
    """
    生データを (施設, 部屋, 日付) ごとにまとめる。
    戻り値: { group_key: {"slots": {午後: state, ...}, "display": str, "is_target": bool} }
    """
    groups = {}
    for key, state in raw_slots.items():
        facility, label, col = (key.split("|") + ["", ""])[:3]
        slot = find_slot_word(col, label)
        if not slot:
            continue  # 時間帯が特定できないコマは対象外
        d = parse_date_from_text(col) or parse_date_from_text(label)
        day_text = f"{label} {col}"
        # 部屋名 = ラベルから時間帯・日付表記を除いたもの
        room = label
        for w in TIME_SLOT_WORDS:
            room = room.replace(w, "")
        room = re.sub(r"20\d{2}[/年.\-]\d{1,2}[/月.\-]\d{1,2}日?", "", room)
        room = re.sub(r"\d{1,2}[/月]\d{1,2}日?", "", room)
        room = re.sub(r"[（(]\s*[月火水木金土日祝]\s*[)）]", "", room).strip() or "(部屋不明)"

        date_key = d.isoformat() if d else re.sub(r"[^0-9/月日土日祝()（）]", "", col) or col
        gkey = f"{facility}|{room}|{date_key}"
        g = groups.setdefault(gkey, {"slots": {}, "date": d, "day_text": day_text,
                                     "facility": facility, "room": room})
        g["slots"][slot] = state
    for g in groups.values():
        g["is_target"] = is_target_day(g["date"], g["day_text"])
    return groups


def find_matched(groups: dict, cfg: dict) -> dict:
    """条件（土日祝 かつ required_slots が全て空き）を満たすグループを返す。"""
    required = cfg["required_slots"]
    matched = {}
    for gkey, g in groups.items():
        if not g["is_target"]:
            continue
        if all(g["slots"].get(s) in AVAILABLE_STATES for s in required):
            matched[gkey] = g
    return matched


# ---------------------------------------------------------------- state & discord

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(raw_slots: dict, matched_keys: list, ever_matched: set):
    STATE_PATH.write_text(
        json.dumps({"updated": datetime.now().isoformat(),
                    "slots": raw_slots,
                    "matched": sorted(matched_keys),
                    "ever_matched": sorted(ever_matched)},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")


def format_group_line(gkey: str, g: dict, reopened: bool) -> str:
    if g["date"]:
        wd = WEEKDAY_JA[g["date"].weekday()]
        holiday = "・祝" if jpholiday.is_holiday(g["date"]) else ""
        day = f"{g['date'].month}/{g['date'].day}({wd}{holiday})"
    else:
        day = gkey.split("|")[2]
    tag = " ♻️再度空き" if reopened else ""
    return f"・**{day}** {g['facility']} {g['room']}（午後＋夜間）{tag}"


def notify_discord(webhook_url: str, items: list, dry_run: bool):
    """items: [(gkey, group, reopened)]"""
    lines = [format_group_line(k, g, r) for k, g, r in items]
    chunks, buf = [], ""
    for line in lines:
        if len(buf) + len(line) > 1800:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf:
        chunks.append(buf)

    for i, chunk in enumerate(chunks):
        payload = {
            "content": "@here" if not dry_run else "",
            "embeds": [{
                "title": "🎉 土日祝 午後＋夜間の連続空きが出ました"
                         + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                "description": chunk + f"\n[予約システムを開く]({BASE_URL})",
                "color": 0x2ECC71,
                "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M")},
            }]
        }
        if dry_run:
            print("[DRY-RUN] Discordへ送信予定の内容:")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code >= 300:
                print(f"[ERROR] Discord通知失敗: {r.status_code} {r.text}")
            else:
                print(f"[INFO] Discordへ通知しました")
        time.sleep(1)


# ---------------------------------------------------------------- main

def run_once(cfg: dict, args) -> bool:
    raw = fetch_availability(cfg, debug=args.debug)
    if not raw:
        print("[ERROR] 空き状況を取得できませんでした。--debug で debug/ の内容を確認してください。")
        return False

    groups = build_groups(raw)
    matched = find_matched(groups, cfg)
    n_target = sum(1 for g in groups.values() if g["is_target"])
    print(f"[INFO] 土日祝のコマグループ {n_target} 件中、条件成立 {len(matched)} 件")

    state = load_state()
    prev_matched = set(state.get("matched", []))
    ever_matched = set(state.get("ever_matched", []))
    first_run = "slots" not in state

    if args.notify_all:
        targets = sorted(matched.keys())
    elif first_run:
        print("[INFO] 初回実行のため状態を保存しました。次回以降、条件成立の変化を通知します。")
        targets = []
    else:
        targets = sorted(set(matched.keys()) - prev_matched)

    if targets:
        items = [(k, matched[k], k in ever_matched) for k in targets]
        print(f"[INFO] 新たに条件成立 {len(targets)} 件を検出")
        notify_discord(cfg.get("discord_webhook_url", ""), items, dry_run=args.dry_run)
    else:
        print("[INFO] 新たな条件成立はありません")

    ever_matched |= set(matched.keys())
    save_state(raw, list(matched.keys()), ever_matched)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--notify-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="常駐モードで定期チェック")
    args = ap.parse_args()

    cfg = load_config()

    if not args.loop:
        ok = run_once(cfg, args)
        sys.exit(0 if ok else 1)

    interval = max(int(cfg["check_interval_min"]), 3) * 60
    start_h, end_h = cfg["active_hours"]
    print(f"[INFO] 常駐モード開始: {cfg['check_interval_min']}分間隔 / 稼働時間 {start_h}時〜{end_h}時")
    while True:
        now = datetime.now()
        if start_h <= now.hour < end_h:
            try:
                run_once(cfg, args)
            except Exception as e:
                print(f"[ERROR] チェック中に例外: {e}")
        else:
            print(f"[INFO] {now:%H:%M} は稼働時間外のためスキップ")
        time.sleep(interval)


if __name__ == "__main__":
    main()
