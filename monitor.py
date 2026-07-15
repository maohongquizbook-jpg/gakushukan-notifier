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
import calendar
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
CONFIG_PATH = BASE_DIR / "config.yaml"   # --config で上書き可
STATE_PATH = BASE_DIR / "state.json"     # --state で上書き可
DEBUG_DIR = BASE_DIR / "debug"

BASE_URL = "https://www.shinjuku.eprs.jp/regasu/web/"  # configのbase_urlで上書き可
NOTIFY_LABEL = "午後＋夜間"  # configのnotify_labelで上書き可
INCLUDE_MANSION_IN_ROOM = False  # 日付順の「館」列を部屋名に含める（地域センター用）
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

def load_config(path=None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("required_slots", ["午後", "夜間"])
    cfg.setdefault("check_interval_min", 5)
    cfg.setdefault("active_hours", [7, 23])
    cfg.setdefault("category_value", CATEGORY_SHOGAI_GAKUSHUKAN)
    cfg.setdefault("horizon_months", 3)
    cfg.setdefault("notify_filled", True)
    cfg.setdefault("facility_filter", [])
    if not cfg.get("discord_webhook_url"):
        print("[WARN] config.yaml の discord_webhook_url が未設定です。--dry-run 以外では通知できません。")
    return cfg


# ---------------------------------------------------------------- scraping

def dump_debug(page, tag: str, screenshot: bool = True):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if screenshot:
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


SLOT_WINDOWS = {"午前": (900, 1200), "午後": (1300, 1700), "夜間": (1800, 2200)}


def parse_daily_list(page) -> dict:
    """「日付順」画面を解析する。
    空きコマは <tr id="YYYYMMDD_館CD_部屋CD_開始時刻_連番"> で列挙されるが、
    連続した時間帯が空いている場合は「13時00分～22時00分」のように
    1行にまとめて表示されるため、行内の時刻範囲を読み取り、
    その範囲がカバーする時間帯（午前/午後/夜間）すべてに展開する。"""
    rows = page.evaluate(
        r"""() => {
            const out = [];
            document.querySelectorAll('table[id^="dt_free-info"] tr[id]').forEach(tr => {
                const m = tr.id.match(/^(\d{8})_(\d+)_(\d+)_(\d{3,4})_\d+$/);
                if (!m) return;
                const fac = tr.querySelector('td.facility a, td.facility');
                const man = tr.querySelector('td.mansion a, td.mansion');
                out.push({ date: m[1], start: m[4],
                           room: fac ? fac.innerText.trim().replace(/\s+/g, ' ') : '',
                           mansion: man ? man.innerText.trim().replace(/\s+/g, ' ') : '',
                           text: (tr.innerText || '').replace(/\s+/g, ' ') });
            });
            return out;
        }"""
    )
    slots = {}
    for r in rows:
        d = r["date"]
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        room = r["room"] or "(部屋不明)"
        if INCLUDE_MANSION_IN_ROOM and r.get("mansion"):
            room = f"{r['mansion']}・{room}"
        times = re.findall(r"(\d{1,2})[:時](\d{2})", r.get("text", ""))
        nums = [int(h) * 100 + int(m) for h, m in times if int(h) <= 24]
        if nums:
            start, end = min(nums), max(nums)
        else:
            try:
                start = int(r["start"])
            except ValueError:
                continue
            end = start + 400
        for slot, (ws, we) in SLOT_WINDOWS.items():
            key = f"{room}|{slot}|{iso}"
            if start <= ws and end >= we:
                slots[key] = "available"
            elif start < we and end > ws:
                slots.setdefault(key, "partially")
    return slots


DAILY_ROW_CAP = 100  # 日付順一覧はおよそ100行で打ち切られる（サイト仕様）

COUNT_ROWS_JS = ("() => document.querySelectorAll("
                 "'table[id^=\"dt_free-info\"] tr[id]').length")

CLICK_MORE_JS = """() => {
    const cands = Array.from(document.querySelectorAll(
            'button, a, input[type=button], input[type=submit]'))
        .filter(e => (e.innerText || e.value || '').includes('さらに表示')
            && e.offsetParent !== null && !e.disabled);
    if (!cands.length) return false;
    cands.sort((a, b) =>
        (a.innerText || a.value || '').length -
        (b.innerText || b.value || '').length);
    cands[0].scrollIntoView({ block: 'center' });
    cands[0].click();
    return true;
}"""


def log_failure(text: str):
    """失敗内容をテキストで debug/ に必ず残す（Artifactsで回収可能にする）"""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        with open(DEBUG_DIR / "failure_log.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {text}\n")
    except Exception:
        pass


def open_home(page, debug: bool = False, tag: str = "home") -> bool:
    """トップページを開いて検索フォームが現れるまで待つ。失敗時は3回までリトライ。"""
    last_err = None
    for attempt in range(3):
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#btn-go", state="attached", timeout=25000)
            return True
        except Exception as e:
            last_err = e
            print(f"[WARN] トップページ読み込み失敗 ({attempt + 1}/3): {type(e).__name__}")
            time.sleep(15 * (attempt + 1))
    print(f"[ERROR] トップページに到達できません: {last_err}")
    log_failure(f"open_home失敗 tag={tag} err={type(last_err).__name__}")
    try:
        dump_debug(page, f"homefail_{tag}")
    except Exception:
        pass
    return False


def expand_daily(page) -> int:
    """「さらに表示」をなくなるまで展開し、読み込めた行数を返す。"""
    clicks, misses = 0, 0
    prev_rows = page.evaluate(COUNT_ROWS_JS)
    for _ in range(150):
        if page.evaluate(CLICK_MORE_JS):
            clicks += 1
            misses = 0
            for _ in range(12):  # 行数が増えるまで最大6秒待つ
                time.sleep(0.5)
                cur = page.evaluate(COUNT_ROWS_JS)
                if cur > prev_rows:
                    prev_rows = cur
                    break
        else:
            misses += 1
            if misses >= 6:
                break
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.0)
    page.evaluate(
        """() => {
            const els = Array.from(document.querySelectorAll('button, a'));
            const btn = els.find(e => (e.innerText || '').trim() === 'すべて開く');
            if (btn) btn.click();
        }"""
    )
    time.sleep(0.5)
    return page.evaluate(COUNT_ROWS_JS)


def search_window(page, cfg: dict, start_iso: str, days_value: str,
                  debug: bool, tag: str, bname_value=None):
    """開始日と期間を指定して検索し、日付順一覧を解析する。
    戻り値: (slots, ok, 行数)"""
    if not open_home(page, debug, tag):
        return {}, False, 0
    time.sleep(0.8)

    # 折りたたみを開き、曜日（土日祝）を設定
    page.evaluate(
        """() => {
            const col = document.getElementById('collapse-when');
            if (col && col.getAttribute('aria-expanded') !== 'true') col.click();
            ['saturday', 'sunday', 'holiday'].forEach(id => {
                const el = document.getElementById(id);
                if (el && !el.checked) el.click();
            });
        }"""
    )
    time.sleep(0.3)
    # 開始日・期間を直接指定（1か月ラジオは使わない）
    page.evaluate(
        """(p) => {
            const ds = document.getElementById('daystart');
            ds.value = p.start;
            ds.dispatchEvent(new Event('input', { bubbles: true }));
            ds.dispatchEvent(new Event('change', { bubbles: true }));
            const dy = document.getElementById('days');
            dy.value = p.days;
            dy.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        {"start": start_iso, "days": days_value},
    )
    # どこで（bname）を指定（Noneなら選択しない）
    if bname_value:
        page.evaluate(
            """(val) => {
                const sel = document.getElementById('bname');
                sel.value = val;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                if (typeof filterInst === 'function') { try { filterInst(); } catch (e) {} }
            }""",
            bname_value,
        )
    time.sleep(0.8)

    page.evaluate("() => document.getElementById('btn-go').click()")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    time.sleep(1.5)

    # 日付順タブへ
    try:
        page.evaluate("() => doAction(document.form1, gRsvWOpeUnreservedDailyAction)")
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        time.sleep(1.5)
    except Exception as e:
        print(f"[WARN] {tag}: 日付順タブでエラー: {e}")
        dump_debug(page, f"dailyfail_{tag}")
        return {}, False, 0

    if "日付順" not in (page.title() or ""):
        print(f"[WARN] {tag}: 日付順画面に到達できませんでした")
        dump_debug(page, f"dailyfail_{tag}")
        return {}, False, 0

    rows = expand_daily(page)
    if debug:
        dump_debug(page, f"daily_{tag}", screenshot=False)
    slots = parse_daily_list(page)
    print(f"[INFO] {tag} ({start_iso}〜{days_value}日間): {rows}行 / {len(slots)}コマ")
    return slots, True, rows


# 期間の細分化: 100行の上限に達した場合の分割パターン（daysの選択肢は 1/2/3/7/31 のみ）
SPLIT_MAP = {"31": [(0, "7"), (7, "7"), (14, "7"), (21, "7"), (28, "3")],
             "7": [(0, "3"), (3, "3"), (6, "1")],
             "3": [(0, "1"), (1, "1"), (2, "1")],
             "2": [(0, "1"), (1, "1")]}


def horizon_end_date(today: date, months: int = 3) -> date:
    """Nか月先の月末日を返す（例: 7/12, months=3 → 10/31）。"""
    m = today.month + months
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, calendar.monthrange(y, m)[1])


def fetch_availability(cfg: dict, debug: bool):
    """1週間ごとに分割して検索し、公開範囲（3か月先の月末）までの空きコマを取得する。
    100行の上限に達した週は自動的に短い期間へ分割して再検索する。"""
    from datetime import timedelta
    today = date.today()
    if cfg.get("horizon_days"):  # 日数での明示指定があれば優先
        end = today + timedelta(days=int(cfg["horizon_days"]) - 1)
    else:
        end = horizon_end_date(today, int(cfg.get("horizon_months", 3)))
    total_days = (end - today).days + 1
    print(f"[INFO] 検索範囲: {today.isoformat()} 〜 {end.isoformat()} ({total_days}日間)")
    # 月単位で検索し、100行上限に達した月だけ週単位に自動分割する
    base_queue = [((today + timedelta(days=off)).isoformat(), "31")
                  for off in range(0, total_days, 31)]

    all_slots = {}
    all_ok = True
    failed_ranges = []  # 取得に失敗した (開始日ISO, 日数) のリスト
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP", viewport={"width": 1400, "height": 1200},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        )
        page = context.new_page()

        # 「どこで」(bname) の値リストを決定。
        #  - category_value 指定あり → その1つだけ
        #  - bname_values: auto → ページから選択肢を自動取得（複数施設を順に検索）
        if cfg.get("category_value"):
            bnames = [cfg["category_value"]]
        elif cfg.get("bname_values") == "auto" or cfg.get("bname_values"):
            if not open_home(page, debug, "discovery"):
                browser.close()
                return {}, False, [(today.isoformat(), total_days)]
            opts = page.evaluate(
                """() => Array.from(document.querySelectorAll('#bname option'))
                        .filter(o => o.value && o.value !== '0')
                        .map(o => ({v: o.value, t: o.textContent.trim()}))"""
            )
            print(f"[INFO] どこで(bname)の選択肢: {opts}")
            if cfg.get("bname_values") == "auto":
                bnames = [o["v"] for o in opts]
            else:
                bnames = cfg["bname_values"]
            bname_names = {o["v"]: o["t"] for o in opts}
        else:
            bnames = [None]
        if "bname_names" not in dir():
            bname_names = {}

        i = 0
        for bname in bnames:
            queue = list(base_queue)
            while queue:
                start_iso, days_value = queue.pop(0)
                i += 1
                tag = f"w{i}"
                slots, ok, rows = {}, False, 0
                for attempt in range(2):  # 失敗時は1回だけ即時リトライ
                    try:
                        slots, ok, rows = search_window(page, cfg, start_iso, days_value,
                                                        debug, tag, bname_value=bname)
                    except Exception as e:
                        print(f"[ERROR] {tag} の取得中に例外: {type(e).__name__}")
                        ok = False
                    if ok:
                        break
                    time.sleep(5)
                if not ok:
                    all_ok = False
                    failed_ranges.append((start_iso, int(days_value)))
                    label = bname_names.get(bname, bname or "")
                    log_failure(f"{tag} 失敗: {label} {start_iso}〜{days_value}日間")
                    print(f"[WARN] {tag} 失敗: {label} {start_iso}〜{days_value}日間")
                    continue
                if rows >= DAILY_ROW_CAP and days_value in SPLIT_MAP:
                    print(f"[INFO] {tag}: 行数が上限に達したため期間を分割して再取得します")
                    base = date.fromisoformat(start_iso)
                    for off, dv in SPLIT_MAP[days_value]:
                        queue.insert(0, ((base + timedelta(days=off)).isoformat(), dv))
                    continue
                all_slots.update(slots)
                time.sleep(1)  # サーバー負荷への配慮

        browser.close()

    n_avail = sum(1 for v in all_slots.values() if v in AVAILABLE_STATES)
    print(f"[INFO] 合計 {len(all_slots)} コマ取得（うち空き {n_avail}）")
    if failed_ranges:
        print(f"[WARN] {len(failed_ranges)} 期間の取得に失敗。該当期間は前回の状態を引き継ぎます: {failed_ranges}")
    return all_slots, all_ok, failed_ranges


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


import unicodedata


def match_allowlist(room_text: str, allowlist: list):
    """部屋名が許可リストに一致するか。一致したエントリ（fee/capacity付き）を返す。"""
    norm = unicodedata.normalize("NFKC", room_text)
    for entry in allowlist:
        if all(unicodedata.normalize("NFKC", kw) in norm for kw in entry["keywords"]):
            return entry
    return None


def find_matched(groups: dict, cfg: dict) -> dict:
    required = cfg["required_slots"]
    flt = cfg.get("facility_filter") or []
    allowlist = cfg.get("room_allowlist") or []
    matched = {}
    for gkey, g in groups.items():
        if not g["is_target"]:
            continue
        if flt and not any(f in gkey for f in flt):
            continue
        if allowlist:
            entry = match_allowlist(g["room"], allowlist)
            if entry is None:
                continue
            g["fee"] = entry.get("fee")
            g["capacity"] = entry.get("capacity")
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
    extra = ""
    if g.get("capacity") or g.get("fee"):
        parts = []
        if g.get("capacity"):
            parts.append(f"定員{g['capacity']}名")
        if g.get("fee"):
            parts.append(f"計{g['fee']:,}円")
        extra = f" [{'・'.join(parts)}]"
    return f"・**{day}** {g['room']}（{NOTIFY_LABEL}）{extra}{tag}"


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
                "title": f"🎉 土日祝 {NOTIFY_LABEL}の連続空きが出ました"
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


def format_slot_key(k: str) -> str:
    room, slot, iso = (k.split("|") + ["", ""])[:3]
    d = parse_date_from_text(iso)
    if d:
        wd = WEEKDAY_JA[d.weekday()]
        hol = "・祝" if jpholiday.is_holiday(d) else ""
        return f"{d.month}/{d.day}({wd}{hol}) {room} {slot}"
    return k


def send_test_notification(cfg: dict, raw_slots: dict, ok: bool, dry_run: bool,
                           matched: dict = None, errors: list = None):
    """--test 用: 取得結果の要約をDiscordへ送る（条件成立の有無に関係なく必ず送信）。"""
    avail = sorted(k for k, v in raw_slots.items() if v in AVAILABLE_STATES)
    if matched is None:
        matched = find_matched(build_groups(raw_slots), cfg)
    ordered = sorted(matched.items(), key=lambda kv: (kv[1]["date"] or date.max, kv[0]))
    pair_lines = [f"・{format_group_line(k, g, False).lstrip('・')}"
                  for k, g in ordered]
    pair_body = "\n".join(pair_lines[:25]) if pair_lines else "（現在、条件成立の空きはありません）"
    if len(pair_lines) > 25:
        pair_body += f"\n…ほか {len(pair_lines) - 25} 件"
    avail = sorted(avail, key=lambda k: (k.split("|")[-1], k))
    lines = [f"・{format_slot_key(k)}" for k in avail[:25]]
    if len(avail) > 25:
        lines.append(f"…ほか {len(avail) - 25} 件")
    body = "\n".join(lines) if lines else "（現在、土日祝の空きコマはありません）"
    status = "✅ 取得成功" if ok else "⚠️ 一部取得失敗（下記参照）"
    err_body = ""
    if errors:
        lines_e = "\n".join(f"・{e}" for e in errors[:15])
        if len(errors) > 15:
            lines_e += f"\n…ほか {len(errors) - 15} 件"
        err_body = f"\n**⚠️ 取得できなかった対象: {len(errors)}件**\n{lines_e}\n"
    payload = {
        "embeds": [{
            "title": "🔔 テスト通知: 監視システムは動作しています",
            "description": (
                f"{status}\n{err_body}\n"
                f"**🎯 {NOTIFY_LABEL}の連続空き（通知対象）: {len(matched)}件**\n{pair_body}\n\n"
                f"**現在の土日祝の空きコマ（全体）: {len(avail)}件**\n{body}\n\n"
                "※これはテスト通知です。実際の通知は上の🎯に新しい枠が"
                "現れた時にだけ届きます。\n"
                f"[予約システムを開く]({BASE_URL})"
            ),
            "color": 0x3498DB,
            "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M")},
        }]
    }
    if dry_run:
        print("[DRY-RUN] テスト通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    url = cfg.get("discord_webhook_url", "")
    if not url:
        print("[ERROR] discord_webhook_url が未設定です。GitHub Actionsの場合はSecretsの"
              " DISCORD_WEBHOOK_URL を確認してください。")
        sys.exit(1)
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code >= 300:
        print(f"[ERROR] Discord通知失敗: {r.status_code} {r.text}")
        sys.exit(1)
    print("[INFO] テスト通知をDiscordへ送信しました")


def format_lost_key(gkey: str) -> str:
    parts = gkey.split("|")
    room, iso = parts[0], parts[-1]
    d = parse_date_from_text(iso)
    if d:
        wd = WEEKDAY_JA[d.weekday()]
        hol = "・祝" if jpholiday.is_holiday(d) else ""
        return f"・**{d.month}/{d.day}({wd}{hol})** {room}（{NOTIFY_LABEL}）"
    return f"・{gkey}"


def notify_lost(webhook_url: str, keys: list, dry_run: bool):
    lines = [format_lost_key(k) for k in keys]
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
            "embeds": [{
                "title": f"📕 {NOTIFY_LABEL}の連続空きが埋まりました"
                         + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                "description": chunk + f"\n[予約システムを開く]({BASE_URL})",
                "color": 0xE74C3C,
                "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M")},
            }]
        }
        if dry_run:
            print("[DRY-RUN] 埋まり通知内容:")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            r = requests.post(webhook_url, json=payload, timeout=15)
            if r.status_code >= 300:
                print(f"[ERROR] Discord通知失敗: {r.status_code} {r.text}")
            else:
                print("[INFO] 埋まり通知をDiscordへ送信しました")
        time.sleep(1)


# ---------------------------------------------------------------- main

def in_failed_range(iso: str, failed_ranges: list) -> bool:
    from datetime import timedelta
    d = parse_date_from_text(iso)
    if not d:
        return False
    for start_iso, days in failed_ranges:
        s = date.fromisoformat(start_iso)
        if s <= d < s + timedelta(days=days):
            return True
    return False


def run_once(cfg: dict, args) -> bool:
    raw, ok, failed_ranges = fetch_availability(cfg, debug=args.debug)
    if not raw and failed_ranges:
        print("[ERROR] 空き状況を1件も取得できませんでした。次回に再試行します。")
        return False

    state0 = load_state()
    if failed_ranges and state0.get("slots"):
        carried = 0
        for k, st in state0["slots"].items():
            if in_failed_range(k.split("|")[-1], failed_ranges):
                if k not in raw:
                    raw[k] = st
                    carried += 1
        print(f"[INFO] 取得失敗期間のコマ {carried} 件を前回状態から引き継ぎました")

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
        targets = sorted(targets, key=lambda k: (matched[k]["date"] or date.max, k))
        items = [(k, matched[k], k in ever_matched) for k in targets]
        print(f"[INFO] 新たに条件成立 {len(targets)} 件を検出")
        notify_discord(cfg.get("discord_webhook_url", ""), items, dry_run=args.dry_run)
    else:
        print("[INFO] 新たな条件成立はありません")

    # 前回成立していたのに今回消えた＝予約が入って埋まった（未来の日付のみ対象）
    if not first_run and not args.notify_all and cfg.get("notify_filled", True):
        lost = []
        for k in sorted(prev_matched - set(matched.keys())):
            d = parse_date_from_text(k.split("|")[-1])
            if d and d >= date.today():
                lost.append(k)
        if lost:
            lost = sorted(lost, key=lambda k: k.split("|")[-1])
            print(f"[INFO] 埋まった連続空き {len(lost)} 件を検出")
            notify_lost(cfg.get("discord_webhook_url", ""), lost, dry_run=args.dry_run)

    ever_matched |= set(matched.keys())
    save_state(raw, list(matched.keys()), ever_matched)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--notify-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="取得結果の要約をテスト通知としてDiscordへ必ず送る")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="設定ファイルのパス")
    ap.add_argument("--state", default=None, help="状態ファイルのパス")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # サイトごとの上書き
    global BASE_URL, SLOT_WINDOWS, NOTIFY_LABEL, STATE_PATH
    BASE_URL = cfg.get("base_url", BASE_URL)
    if cfg.get("slot_windows"):
        SLOT_WINDOWS = {k: tuple(v) for k, v in cfg["slot_windows"].items()}
        globals()["SLOT_WINDOWS"] = SLOT_WINDOWS
    # 時間帯の語リストは常にSLOT_WINDOWSから導出（長い名前を先に照合: 午後1 が 午後 より優先）
    globals()["TIME_SLOT_WORDS"] = tuple(
        sorted(SLOT_WINDOWS.keys(), key=len, reverse=True))
    NOTIFY_LABEL = cfg.get("notify_label", NOTIFY_LABEL)
    globals()["INCLUDE_MANSION_IN_ROOM"] = bool(cfg.get("include_mansion_in_room"))
    if args.state:
        STATE_PATH = Path(args.state)
    elif cfg.get("state_file"):
        STATE_PATH = BASE_DIR / cfg["state_file"]
    if args.test:
        raw, ok, failed_ranges = fetch_availability(cfg, debug=args.debug)
        errors = [f"{s}から{d}日間の取得に失敗" for s, d in failed_ranges] or None
        send_test_notification(cfg, raw, ok, dry_run=args.dry_run, errors=errors)
        sys.exit(0)
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
