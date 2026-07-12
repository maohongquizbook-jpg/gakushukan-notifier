#!/usr/bin/env python3
"""
新宿区 生涯学習館 空き状況モニター → Discord Webhook 通知ツール（v2）

レガス新宿 施設予約システムの空き状況検索フォームを実際のDOM構造
（#thismonth, #saturday/#sunday/#holiday, #bname=1000_1650, #btn-go）に
合わせて操作し、土日祝の「午後＋夜間 連続空き」を検知して通知する。

使い方:
    python monitor.py                # 1回チェック（差分があれば通知）
    python monitor.py --loop         # 常駐モード
    python monitor.py --debug       # 画面キャプチャ等を debug/ に保存
    python monitor.py --notify-all  # 現在条件を満たすコマを全部通知
    python monitor.py --dry-run     # Discordに送信しない
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
CATEGORY_SHOGAI_GAKUSHUKAN = "1000_1650"  # #bname の「生涯学習館」

SYMBOL_MAP = {
    "○": "available", "◯": "available", "〇": "available", "◎": "available",
    "△": "partially",
    "×": "full", "✕": "full", "✖": "full",
    "取": "processing",   # 取消処理中（約30分後に予約可能になる）
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
    cfg.setdefault("check_interval_min", 5)
    cfg.setdefault("active_hours", [7, 23])
    cfg.setdefault("category_value", CATEGORY_SHOGAI_GAKUSHUKAN)
    cfg.setdefault("facility_filter", [])
    if not cfg.get("discord_webhook_url"):
        print("[WARN] config.yaml の discord_webhook_url が未設定です。--dry-run 以外では通知できません。")
    return cfg


# ---------------------------------------------------------------- scraping

def dump_debug(page, tag: str):
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
    print(f"[DEBUG] debug/ に {tag} を保存しました")


def extract_tables(page) -> list:
    """
    ページ内の全 <table> を、直前の見出し（施設名など）付きで抽出する。
    セルは rowspan/colspan を展開してグリッド化する。
    """
    return page.evaluate(
        """() => {
            function findTitle(tbl) {
                const cap = tbl.querySelector('caption');
                if (cap && cap.innerText.trim()) return cap.innerText.trim();
                // カード形式のヘッダ
                const card = tbl.closest('.card');
                if (card) {
                    const h = card.querySelector('.card-header, .card-title, h1,h2,h3,h4,h5');
                    if (h && h.innerText.trim()) return h.innerText.trim();
                }
                // 直前の兄弟要素をさかのぼって見出しらしきものを探す
                let el = tbl.previousElementSibling, hops = 0;
                while (el && hops < 6) {
                    const t = el.innerText ? el.innerText.trim() : '';
                    if (t && t.length < 80 &&
                        (el.matches('h1,h2,h3,h4,h5,h6,legend,strong,b,.title,.heading') || /館|センター|施設/.test(t))) {
                        return t.split('\\n')[0];
                    }
                    el = el.previousElementSibling; hops++;
                }
                // 親をひとつ上がって同様に探す
                const parent = tbl.parentElement;
                if (parent) {
                    let p = parent.previousElementSibling, ph = 0;
                    while (p && ph < 4) {
                        const t = p.innerText ? p.innerText.trim() : '';
                        if (t && t.length < 80 && /館|センター|施設|室/.test(t)) return t.split('\\n')[0];
                        p = p.previousElementSibling; ph++;
                    }
                }
                return '';
            }

            function gridify(tbl) {
                // rowspan/colspan を展開して 2次元配列にする
                const grid = [];
                const rows = tbl.querySelectorAll('tr');
                rows.forEach((tr, r) => {
                    grid[r] = grid[r] || [];
                    let c = 0;
                    tr.querySelectorAll('th,td').forEach(cell => {
                        while (grid[r][c] !== undefined) c++;
                        const text = cell.innerText.trim().replace(/\\s+/g, ' ');
                        const rs = parseInt(cell.getAttribute('rowspan') || '1');
                        const cs = parseInt(cell.getAttribute('colspan') || '1');
                        for (let i = 0; i < rs; i++) {
                            for (let j = 0; j < cs; j++) {
                                grid[r + i] = grid[r + i] || [];
                                grid[r + i][c + j] = text;
                            }
                        }
                        c += cs;
                    });
                });
                return grid;
            }

            const out = [];
            document.querySelectorAll('table').forEach(tbl => {
                const grid = gridify(tbl);
                if (grid.length >= 2) out.push({ title: findTitle(tbl), grid });
            });
            return out;
        }"""
    )


def parse_raw_slots(tables: list) -> dict:
    """
    グリッド化されたテーブル群から { "見出し|行ヘッダ群|列ヘッダ群": state } を作る。
    ヘッダ行数を自動判定: 記号セルが現れる最初の行より上を全部列ヘッダとして扱い、
    行側も記号セルより左を全部行ヘッダとして扱う（多段ヘッダ対応）。
    """
    slots = {}
    for tbl in tables:
        grid = tbl["grid"]
        title = re.sub(r"\s+", " ", tbl.get("title") or "").strip()

        # 記号セルの位置を調べる
        sym_cells = []
        for r, row in enumerate(grid):
            for c, cell in enumerate(row or []):
                if cell and cell.strip()[:1] in SYMBOL_MAP and len(cell.strip()) <= 3:
                    sym_cells.append((r, c))
        if not sym_cells:
            continue
        first_sym_row = min(r for r, _ in sym_cells)
        first_sym_col = min(c for _, c in sym_cells)

        for r, c in sym_cells:
            sym = grid[r][c].strip()[:1]
            # 列ヘッダ: 記号行より上の同じ列のテキストを連結
            col_parts = []
            for hr in range(first_sym_row):
                v = grid[hr][c] if c < len(grid[hr] or []) else ""
                if v and v not in col_parts:
                    col_parts.append(v)
            # 行ヘッダ: 記号列より左の同じ行のテキストを連結
            row_parts = []
            for hc in range(first_sym_col):
                v = grid[r][hc] if hc < len(grid[r] or []) else ""
                if v and v not in row_parts:
                    row_parts.append(v)
            col = " ".join(col_parts) or f"col{c}"
            label = " ".join(row_parts) or f"row{r}"
            slots[f"{title}|{label}|{col}"] = SYMBOL_MAP[sym]
    return slots


def get_room_vocab(page) -> list:
    """#iname の選択肢（部屋名一覧）を取得する。"""
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('#iname option'))
                    .map(o => o.textContent.trim())
                    .filter(t => t && t !== '指定なし')"""
        )
    except Exception:
        return []


def parse_week_info(page) -> dict:
    """「施設ごと」画面の週表示テーブル(#week-info)から td の id/class/alt を直接読む。"""
    cells = page.evaluate(
        r"""() => {
            const out = [];
            const tbl = document.getElementById('week-info');
            if (!tbl) return out;
            const cap = tbl.querySelector('caption');
            const room = cap ? cap.innerText.trim().replace(/\s+/g, ' ') : '';
            tbl.querySelectorAll('td[id]').forEach(td => {
                const m = td.id.match(/^(\d{8})_(\d+)$/);
                if (!m) return;
                const img = td.querySelector('img.calendar-status');
                out.push({ room, date: m[1], code: m[2],
                           cls: td.className || '', alt: img ? (img.alt || '') : '' });
            });
            return out;
        }"""
    )
    slots = {}
    for c in cells:
        code = c["code"]
        slot = {"1": "午前", "2": "午後", "3": "夜間"}.get(code[:1])
        if not slot:
            continue
        alt, cls = c["alt"], c["cls"]
        if "available" in cls or "空き" == alt or alt.startswith("空"):
            state = "partially" if "一部" in alt else "available"
        elif "一部" in alt:
            state = "partially"
        elif "取" in alt:
            state = "processing"
        elif "予約" in alt or "×" in alt:
            state = "full"
        else:
            state = "closed"
        d = c["date"]
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        room = c["room"] or "(部屋不明)"
        slots[f"{room}|{slot}|{iso}"] = state
    return slots


def parse_daily_list(page, room_vocab: list) -> dict:
    """「日付順」画面（空いている時間帯の一覧）を解析する。
    テーブルを汎用グリッド化し、日付・時間帯・部屋名が揃う行を空きコマとして拾う。"""
    slots = {}
    # td idパターンがあればそちらを優先（週表示と同形式の場合）
    id_cells = page.evaluate(
        r"""() => Array.from(document.querySelectorAll('td[id]'))
                .map(td => ({ id: td.id, cls: td.className || '',
                              text: (td.innerText || '').trim().replace(/\s+/g, ' ') }))
                .filter(c => /^\d{8}_\d+$/.test(c.id))"""
    )
    tables = extract_tables(page)
    for tbl in tables:
        for row in tbl["grid"][0:]:
            if not row:
                continue
            row_text = " ".join(x for x in row if x)
            d = parse_date_from_text(row_text)
            slot = find_slot_word(row_text)
            room = next((r for r in room_vocab if r and r in row_text), None)
            if room is None:
                m = re.search(r"\S*(?:赤城|戸山|北新宿|住吉町|西戸山)\S*", row_text)
                room = m.group(0) if m else None
            if d and slot and room:
                slots[f"{room}|{slot}|{d.isoformat()}"] = "available"
    return slots


def fetch_availability(cfg: dict, debug: bool) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP", viewport={"width": 1400, "height": 1200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        )
        page = context.new_page()
        print(f"[INFO] {BASE_URL} を開いています…")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#btn-go", timeout=30000)
        time.sleep(1)

        # 検索条件を設定（折りたたみ内の要素でも動くようJS直接実行）
        page.evaluate(
            """() => {
                const col = document.getElementById('collapse-when');
                if (col && col.getAttribute('aria-expanded') !== 'true') col.click();
                const month = document.getElementById('thismonth');
                if (month) month.click();
                ['saturday', 'sunday', 'holiday'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && !el.checked) el.click();
                });
            }"""
        )
        time.sleep(0.5)
        page.evaluate(
            """(val) => {
                const sel = document.getElementById('bname');
                sel.value = val;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                if (typeof filterInst === 'function') { try { filterInst(); } catch (e) {} }
            }""",
            cfg["category_value"],
        )
        time.sleep(1)

        room_vocab = get_room_vocab(page)
        print(f"[INFO] 対象部屋数: {len(room_vocab)}")

        # 検索実行 → 「施設ごと」結果画面へ
        page.evaluate("() => document.getElementById('btn-go').click()")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        time.sleep(2)
        if not room_vocab:
            room_vocab = get_room_vocab(page)

        # ---- 戦略1: 「日付順」タブ（全部屋の空きを一覧で取得） ----
        slots = {}
        try:
            page.evaluate("() => doAction(document.form1, gRsvWOpeUnreservedDailyAction)")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            time.sleep(2)
            if debug:
                dump_debug(page, "daily_list")
            slots = parse_daily_list(page, room_vocab)
            if slots:
                print(f"[INFO] 日付順一覧から {len(slots)} コマの空きを取得")
        except Exception as e:
            print(f"[WARN] 日付順タブの処理でエラー: {e}")

        # ---- 戦略2: フォールバック（施設ごとを部屋単位で巡回） ----
        if not slots:
            print("[INFO] 日付順の解析に失敗。施設ごと画面を部屋単位で巡回します…")
            dump_debug(page, "daily_parsefail")
            # 施設ごと画面に戻る
            page.evaluate("() => doAction(document.form1, gRsvWOpeInstSrchVacantAction)")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            time.sleep(2)
            room_values = page.evaluate(
                """() => Array.from(document.querySelectorAll('#iname option'))
                        .filter(o => o.value && o.value !== '0')
                        .map(o => o.value)"""
            )
            print(f"[INFO] 巡回対象: {len(room_values)} 部屋")
            for i, val in enumerate(room_values):
                try:
                    page.evaluate(
                        """(v) => {
                            const sel = document.getElementById('iname');
                            sel.value = v;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            doSearch(document.form1, gRsvWOpeInstSrchVacantAction);
                        }""",
                        val,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except PWTimeout:
                        pass
                    time.sleep(1.2)
                    part = parse_week_info(page)
                    # 「翌月」がある場合はもう1ページ分読む（検索期間が月をまたぐ場合）
                    clicked = page.evaluate(
                        """() => {
                            const els = Array.from(document.querySelectorAll('button, a'));
                            const nx = els.find(e => (e.innerText || '').trim() === '翌月' && !e.disabled);
                            if (nx) { nx.click(); return true; }
                            return false;
                        }"""
                    )
                    if clicked:
                        time.sleep(1.5)
                        part.update(parse_week_info(page))
                    slots.update(part)
                except Exception as e:
                    print(f"[WARN] 部屋 {val} の取得でエラー: {e}")
            if debug and slots:
                dump_debug(page, "fallback_last_room")

        if not slots:
            print("[WARN] どちらの方式でも解析できませんでした。画面を debug/ に保存します。")
            dump_debug(page, "result_parsefail")
        else:
            n_avail = sum(1 for v in slots.values() if v in AVAILABLE_STATES)
            print(f"[INFO] 合計 {len(slots)} コマ取得（うち空き {n_avail}）")

        browser.close()
        return slots


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
    if d is not None:
        return d.weekday() >= 5 or jpholiday.is_holiday(d)
    return bool(re.search(r"[（(]\s*(土|日|祝)|土曜|日曜|祝日", day_text))


def build_groups(raw_slots: dict) -> dict:
    groups = {}
    for key, state in raw_slots.items():
        title, label, col = (key.split("|") + ["", ""])[:3]
        slot = find_slot_word(col, label)
        if not slot:
            continue
        d = parse_date_from_text(col) or parse_date_from_text(label)
        day_text = f"{label} {col}"

        def clean(text: str) -> str:
            """日付・時間帯・曜日表記・一般的なヘッダ語を取り除き、部屋名成分だけ残す。"""
            for w in TIME_SLOT_WORDS:
                text = text.replace(w, "")
            text = re.sub(r"20\d{2}[/年.\-]\d{1,2}[/月.\-]\d{1,2}日?", "", text)
            text = re.sub(r"\d{1,2}[/月]\d{1,2}日?", "", text)
            text = re.sub(r"[（(][月火水木金土日祝・\s]{1,6}[)）]", "", text)
            text = re.sub(r"^(日付|時間帯|部屋|施設|室場名?)$", "", text.strip())
            return text.strip()

        room = re.sub(r"\s+", " ", f"{title} {clean(label)} {clean(col)}").strip() or "(部屋不明)"
        date_key = d.isoformat() if d else re.sub(r"[^0-9/月日土日祝()（）]", "", col) or col
        gkey = f"{room}|{date_key}"
        g = groups.setdefault(gkey, {"slots": {}, "date": d, "day_text": day_text, "room": room})
        g["slots"][slot] = state
    for g in groups.values():
        g["is_target"] = is_target_day(g["date"], g["day_text"])
    return groups


def find_matched(groups: dict, cfg: dict) -> dict:
    required = cfg["required_slots"]
    flt = cfg.get("facility_filter") or []
    matched = {}
    for gkey, g in groups.items():
        if not g["is_target"]:
            continue
        if flt and not any(f in gkey for f in flt):
            continue
        if all(g["slots"].get(s) in AVAILABLE_STATES for s in required):
            matched[gkey] = g
    return matched


# ---------------------------------------------------------------- state & discord

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
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
        day = gkey.split("|")[-1]
    tag = " ♻️再度空き" if reopened else ""
    return f"・**{day}** {g['room']}（午後＋夜間）{tag}"


def notify_discord(webhook_url: str, items: list, dry_run: bool):
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
                print("[INFO] Discordへ通知しました")
        time.sleep(1)


# ---------------------------------------------------------------- main

def run_once(cfg: dict, args) -> bool:
    raw = fetch_availability(cfg, debug=args.debug)
    if not raw:
        print("[ERROR] 空き状況を取得できませんでした。debug/ の result_parsefail を確認してください。")
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
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if not args.loop:
        ok = run_once(cfg, args)
        sys.exit(0 if ok else 1)

    interval = max(int(cfg["check_interval_min"]), 3) * 60
    start_h, end_h = cfg["active_hours"]
    print(f"[INFO] 常駐モード開始: {cfg['check_interval_min']}分間隔 / 稼働 {start_h}時〜{end_h}時")
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
