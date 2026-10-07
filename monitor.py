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
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
AVAILABLE_STATES = {"available"}  # 一部空きは「時間帯全体が空き」の証拠にならない
TIME_SLOT_WORDS = ("午前", "午後", "夜間")
WEEKDAY_JA = "月火水木金土日"
JST = ZoneInfo("Asia/Tokyo")
STATE_VERSION = 2
CHIIKI_RULES_REVISION = "2026-10-07"


def now_jst():
    return datetime.now(JST)


def today_jst():
    return now_jst().date()


# ---------------------------------------------------------------- config

def load_config(path=None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "/chiiki/web/" in cfg.get("base_url", ""):
        if (cfg.get("facility_rules_revision") != CHIIKI_RULES_REVISION
                or not cfg.get("room_catalog")):
            raise ValueError(
                "地域センターの利用条件が旧版です。monitor.pyとconfig_chiiki.yamlを"
                "同じ修正版で上書きしてください。施設確認済みの除外条件を適用できないため通知を停止します。")
        eligible = [e for e in cfg["room_catalog"]
                    if e.get("monitor", True) and not e.get("excluded_reason")
                    and e.get("user_confirmed_use") not in {"not_allowed", "joint_only"}
                    and e.get("fee", float("inf")) <= cfg.get("max_total_fee", float("inf"))]
        print(f"[INFO] 地域センター利用条件版: {CHIIKI_RULES_REVISION} / 通知候補{len(eligible)}室 / 設定: {Path(path or CONFIG_PATH).name}")
    cfg.setdefault("required_slots", ["午後", "夜間"])
    cfg.setdefault("check_interval_min", 5)
    cfg.setdefault("active_hours", [7, 23])
    cfg.setdefault("category_value", CATEGORY_SHOGAI_GAKUSHUKAN)
    cfg.setdefault("horizon_months", 3)
    cfg.setdefault("notify_filled", False)
    cfg.setdefault("discord_mention", "")
    cfg.setdefault("closed_confirmations", 2)
    cfg.setdefault("facility_filter", [])
    # 環境変数はメモリ上だけで読み、追跡中の設定ファイルを書き換えない。
    if "DISCORD_WEBHOOK_URL" in os.environ:
        cfg["discord_webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"]
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
    intervals = {}
    for r in rows:
        d = r["date"]
        iso = datetime.strptime(d, "%Y%m%d").date().isoformat()
        room = r["room"]
        if not room:
            raise ValueError("日付順一覧の部屋名が不明です")
        if INCLUDE_MANSION_IN_ROOM and r.get("mansion"):
            room = f"{r['mansion']}・{room}"
        ranges = re.findall(
            r"(\d{1,2})[:時](\d{2})分?\s*[～〜~－–—-]\s*(\d{1,2})[:時](\d{2})",
            r.get("text", ""))
        if not ranges:
            raise ValueError("日付順一覧の終了時刻が不明です（推測で空きにしません）")
        for sh, sm, eh, em in ranges:
            start, end = int(sh) * 60 + int(sm), int(eh) * 60 + int(em)
            if not (0 <= int(sm) < 60 and 0 <= int(em) < 60 and 0 <= start < end <= 1440):
                raise ValueError("日付順一覧の時刻が不正です")
            intervals.setdefault((room, iso), []).append((start, end))
    slots = {}
    for (room, iso), spans in intervals.items():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for slot, (ws, we) in SLOT_WINDOWS.items():
            ws, we = ws // 100 * 60 + ws % 100, we // 100 * 60 + we % 100
            key = f"{room}|{slot}|{iso}"
            if any(start <= ws and end >= we for start, end in merged):
                slots[key] = "available"
            elif any(start < we and end > ws for start, end in merged):
                slots[key] = "partially"
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
    today = today_jst()
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
            locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width": 1400, "height": 1200},
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
                if rows >= DAILY_ROW_CAP or (rows > 0 and not slots):
                    # 1日に分けても上限に達する場合、未取得分を満室と断定しない。
                    all_ok = False
                    failed_ranges.append((start_iso, int(days_value)))
                    log_failure(f"{tag}: 一覧の全件取得・解析を確認できません")
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
        today = today_jst()
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


def normalize_room_name(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\(\d+\s*(?:名|人)?\)$", "", text.strip())
    return re.sub(r"\s+", "", text).replace("簞", "箪")


def room_catalog(cfg):
    if cfg.get("room_catalog"):
        return cfg["room_catalog"]
    return [dict(room, facility=target["kan"])
            for target in cfg.get("targets", []) for room in target["rooms"]]


def match_room(room_text, entries):
    """館と部屋を別々に完全一致。定員表記・全半角・明示した別名だけを吸収。"""
    facility, sep, room = room_text.partition("・")
    if not sep:
        return None
    for entry in entries:
        facilities = [entry["facility"], *entry.get("facility_aliases", [])]
        names = [entry["name"], *entry.get("aliases", [])]
        if (normalize_room_name(facility) in {normalize_room_name(x) for x in facilities}
                and normalize_room_name(room) in {normalize_room_name(x) for x in names}):
            return entry
    return None


def annotate_room(g, entry, cfg):
    for field in ("capacity", "fee", "fee_note", "usage_note", "blocked_reason",
                  "conditional_reason", "normal_fee", "source", "slot_times"):
        if field in entry:
            g[field] = entry[field]
    required = cfg["required_slots"]
    optional = [s for s in cfg.get("optional_slots", [])
                if g["slots"].get(s) in AVAILABLE_STATES]
    order = list(cfg.get("slot_windows", {})) or [*required, *optional]
    g["available_slots"] = [s for s in order if s in required or s in optional]
    g["optional_available"] = optional
    rates = entry.get("slot_fees", {})
    if rates:
        g["required_fee"] = sum(rates[s] for s in required)
        g["fee"] = sum(rates[s] for s in g["available_slots"])
        g["budget_fee"] = sum(rates[s] for s in cfg.get("budget_slots", required))
    else:
        g["required_fee"] = g.get("fee")
        g["budget_fee"] = g.get("fee")
    g["has_optional_slots"] = bool(cfg.get("optional_slots"))


def find_matched(groups: dict, cfg: dict) -> dict:
    required = cfg["required_slots"]
    flt = cfg.get("facility_filter") or []
    allowlist = cfg.get("room_allowlist") or []
    matched = {}
    catalog = room_catalog(cfg)
    unknown = set()
    today, end = search_bounds(cfg)
    for gkey, g in groups.items():
        if not g["is_target"] or not g["date"] or not today <= g["date"] <= end:
            continue
        if flt and not any(f in gkey for f in flt):
            continue
        if catalog:
            entry = match_room(g["room"], catalog)
            if entry is None:
                unknown.add(g["room"])
                continue
            if entry.get("monitor", True) is False:
                continue
            if entry.get("excluded_reason"):
                continue
            if entry.get("user_confirmed_use") in {"not_allowed", "joint_only"}:
                # 個別確認は一般的な公開案内より優先。一体利用の部屋を
                # 片側だけの料金・空きで通知することも認めない。
                continue
            annotate_room(g, entry, cfg)
            if cfg.get("max_total_fee") is not None:
                if g.get("budget_fee") is None or g["budget_fee"] > cfg["max_total_fee"]:
                    continue
        if allowlist and not catalog:
            entry = match_allowlist(g["room"], allowlist)
            if entry is None:
                continue
            g["fee"] = entry.get("fee")
            g["capacity"] = entry.get("capacity")
        if all(g["slots"].get(s) in AVAILABLE_STATES for s in required):
            matched[gkey] = g
    for room in sorted(unknown):
        print(f"[WARN] 施設台帳に未登録のため料金・定員を確認してください: {room}")
    return matched


# ---------------------------------------------------------------- state & discord

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("slots"), dict):
                raise ValueError("slots が辞書ではありません")
            for field in ("matched", "ever_matched"):
                if not isinstance(state.get(field), list) or not all(isinstance(k, str) for k in state[field]):
                    raise ValueError(f"{field} がキーのリストではありません")
            return state
        except (ValueError, OSError) as e:
            raise RuntimeError(f"{STATE_PATH.name} を読めません。復旧するか --reset-state で再登録してください") from e
    return {}


def save_state(raw_slots: dict, matched_keys: list, ever_matched: set, **metadata):
    data = json.dumps({"version": STATE_VERSION,
                    "updated": now_jst().isoformat(),
                    "slots": raw_slots,
                    "matched": sorted(matched_keys),
                    "ever_matched": sorted(ever_matched), **metadata},
                   ensure_ascii=False, indent=1) + "\n"
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE_PATH.parent,
                                         prefix=STATE_PATH.name + ".", suffix=".tmp", delete=False) as f:
            temp_path = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, STATE_PATH)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def format_group_line(gkey: str, g: dict, reopened: bool) -> str:
    if g["date"]:
        wd = WEEKDAY_JA[g["date"].weekday()]
        holiday = "・祝" if jpholiday.is_holiday(g["date"]) else ""
        day = f"{g['date'].month}/{g['date'].day}({wd}{holiday})"
    else:
        day = gkey.split("|")[-1]
    tag = " ♻️再度空き" if reopened else ""
    capacity = f"最大{g['capacity']}人" if g.get("capacity") is not None else "定員未確認"
    fee = f"{g['fee']:,}円" if g.get("fee") is not None else "料金未確認"
    slots = "＋".join(g.get("available_slots", [])) or NOTIFY_LABEL
    line = f"・**{day}** {g['room']}｜{capacity}{tag}\n　空き: {slots}｜合計 {fee}"
    if g.get("has_optional_slots"):
        extra = "・".join(g["optional_available"]) or "なし"
        line += f"（必須2枠 {g['required_fee']:,}円／追加の空き: {extra}）"
    if g.get("normal_fee") is not None:
        line += f"／通常料金 {g['normal_fee']:,}円"
    for field in ("blocked_reason", "conditional_reason", "fee_note", "usage_note"):
        if g.get(field):
            line += f"\n　{g[field]}"
    return line


def post_discord(webhook_url, payload, dry_run):
    if dry_run:
        print("[DRY-RUN] Discordへ送信予定の内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not webhook_url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定です（状態は送信済みにしません）")
    try:
        response = requests.post(webhook_url, params={"wait": "true"}, json=payload, timeout=15)
    except requests.RequestException as e:
        # RequestException の本文にWebhookのトークンを含むURLが出ることがある。
        raise RuntimeError("Discord通知の通信に失敗しました") from None
    if response.status_code >= 300:
        raise RuntimeError(f"Discord通知失敗: HTTP {response.status_code}")
    print("[INFO] Discordへ通知しました")


def send_lines(webhook_url, rows, title, color, dry_run, mention="", on_sent=None):
    chunks, keys, lines, size = [], [], [], 0
    for key, line in rows:
        if lines and size + len(line) + 1 > 1800:
            chunks.append((keys, lines))
            keys, lines, size = [], [], 0
        keys.append(key)
        lines.append(line)
        size += len(line) + 1
    if lines:
        chunks.append((keys, lines))
    for i, (keys, lines) in enumerate(chunks):
        ping = mention if i == 0 and mention in ("@here", "@everyone") else ""
        payload = {
            "content": ping,
            "allowed_mentions": {"parse": ["everyone"] if ping else []},
            "embeds": [{
                "title": title + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                "description": "\n".join(lines) + f"\n\n[予約システムを開く]({BASE_URL})",
                "color": color,
                "footer": {"text": now_jst().strftime("%Y-%m-%d %H:%M JST")},
            }],
        }
        post_discord(webhook_url, payload, dry_run)
        if on_sent and not dry_run:
            on_sent(keys)
        if not dry_run and i + 1 < len(chunks):
            time.sleep(1)


def notify_discord(webhook_url: str, items: list, dry_run: bool, mention="", on_sent=None):
    categories = (
        ("available", f"🎉 土日祝 {NOTIFY_LABEL}の空きが出ました", 0x2ECC71),
        ("blocked", "🚫 空いてはいるが利用出来ない施設", 0xE67E22),
        ("conditional", "🔎 空いているが利用条件の確認が必要な施設", 0xF1C40F),
    )
    for category, title, color in categories:
        rows = [(k, format_group_line(k, g, r)) for k, g, r in items
                if notification_category(g) == category]
        if rows:
            send_lines(webhook_url, rows, title, color, dry_run, mention, on_sent)
            mention = ""


def notification_category(g):
    if g.get("blocked_reason"):
        return "blocked"
    return "conditional" if g.get("conditional_reason") else "available"


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
    # 詳細は通常通知と同じ分類・分割処理へ渡す。長い説明でもEmbed上限を超えない。
    status = "✅ 取得成功" if ok else "⚠️ 一部取得失敗（下記参照）"
    err_body = ""
    if errors:
        lines_e = "\n".join(f"・{str(e)[:150]}" for e in errors[:15])
        if len(errors) > 15:
            lines_e += f"\n…ほか {len(errors) - 15} 件"
        err_body = f"\n**⚠️ 取得できなかった対象: {len(errors)}件**\n{lines_e}\n"
    payload = {
        "embeds": [{
            "title": "🔔 テスト通知: 監視システムは動作しています",
            "description": (
                f"{status}\n{err_body}\n"
                f"**🎯 {NOTIFY_LABEL}の空き（通知対象）: {len(matched)}件**\n"
                "料金・定員・利用条件は、この後の分類別メッセージに表示します（各分類先頭5件）。\n\n"
                f"**取得した空きコマ（全体）: {len(avail)}件**\n\n"
                "※これはテスト通知です。実際の通知は上の🎯に新しい枠が"
                "現れた時にだけ届きます。\n"
                f"[予約システムを開く]({BASE_URL})"
            ),
            "color": 0x3498DB,
            "footer": {"text": now_jst().strftime("%Y-%m-%d %H:%M JST")},
        }]
    }
    payload["allowed_mentions"] = {"parse": []}
    post_discord(cfg.get("discord_webhook_url", ""), payload, dry_run)
    examples = []
    for category in ("available", "blocked", "conditional"):
        examples.extend([(k, g, False) for k, g in ordered
                         if notification_category(g) == category][:5])
    notify_discord(cfg.get("discord_webhook_url", ""), examples, dry_run)


def format_lost_key(gkey: str) -> str:
    parts = gkey.split("|")
    room, iso = parts[0], parts[-1]
    d = parse_date_from_text(iso)
    if d:
        wd = WEEKDAY_JA[d.weekday()]
        hol = "・祝" if jpholiday.is_holiday(d) else ""
        return f"・**{d.month}/{d.day}({wd}{hol})** {room}（{NOTIFY_LABEL}）"
    return f"・{gkey}"


def notify_lost(webhook_url: str, keys: list, dry_run: bool, on_sent=None):
    send_lines(webhook_url, [(k, format_lost_key(k)) for k in keys],
               f"📕 {NOTIFY_LABEL}の連続空きが埋まりました", 0xE74C3C,
               dry_run, on_sent=on_sent)


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


def search_bounds(cfg):
    today = today_jst()
    if cfg.get("horizon_days"):
        end = today + timedelta(days=int(cfg["horizon_days"]) - 1)
    else:
        end = horizon_end_date(today, int(cfg.get("horizon_months", 3)))
    return today, end


def criteria_hash(cfg):
    fields = ("base_url", "category_value", "bname_values", "include_mansion_in_room",
              "required_slots", "slot_windows", "facility_filter", "room_allowlist",
              "targets", "horizon_days", "horizon_months", "room_catalog", "optional_slots",
              "max_total_fee", "budget_slots")
    criteria = {k: cfg.get(k) for k in fields}
    return hashlib.sha256(json.dumps(criteria, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def process_snapshot(cfg, args, raw, *, failed_ranges=(), failed_rooms=(),
                     explicit_status=False, annotate=None):
    """3種類の監視に共通の差分処理。失敗箇所は未知として前回判定を保つ。"""
    reset = getattr(args, "reset_state", False)
    state = {} if reset else load_state()
    today, end = search_bounds(cfg)

    def in_scope(key):
        d = parse_date_from_text(key.split("|")[-1])
        return d is not None and today <= d <= end

    def failed(key):
        return (key.split("|")[0] in failed_rooms
                or in_failed_range(key.split("|")[-1], failed_ranges))

    # 失敗箇所の新旧スロットを混ぜて、実在しない連続空きを組み立てない。
    fresh = {k: v for k, v in raw.items() if in_scope(k) and not failed(k)}
    if not fresh and (failed_ranges or failed_rooms):
        print("[ERROR] 比較できる取得結果がありません。状態を更新しません")
        return False
    groups = build_groups(fresh)
    matched = find_matched(groups, cfg)
    if annotate:
        annotate(matched, cfg)
    current = set(matched)
    fingerprint = criteria_hash(cfg)
    baseline = (reset or state.get("version") != STATE_VERSION
                or state.get("criteria_hash") != fingerprint)
    max_age = float(cfg.get("baseline_after_hours", 72))
    if state.get("updated") and max_age > 0:
        updated = datetime.fromisoformat(state["updated"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=JST)
        baseline |= now_jst() - updated > timedelta(hours=max_age)
    previous = {k for k in state.get("matched", []) if in_scope(k)}
    ever = {k for k in state.get("ever_matched", []) if in_scope(k)}
    raw_saved = dict(fresh)
    raw_saved.update({k: v for k, v in state.get("slots", {}).items() if in_scope(k) and failed(k)})

    failed_dates = { (date.fromisoformat(start) + timedelta(days=i)).isoformat()
                     for start, days in failed_ranges for i in range(days)
                     if today <= date.fromisoformat(start) + timedelta(days=i) <= end }
    pending = state.get("baseline_pending", {})
    pending_dates = set(pending.get("dates", []))
    pending_rooms = set(pending.get("rooms", []))

    def newly_baselined(key):
        return key.split("|")[-1] in pending_dates or key.split("|")[0] in pending_rooms

    counts = {}
    if baseline:
        accepted = set(current)
        pending_dates, pending_rooms = failed_dates, set(failed_rooms)
        print(f"[INFO] 初回・旧形式・条件変更・長期停止後のため、{len(current)}件を通知せず基準登録します")
    else:
        accepted = set(previous)
        # 初回に取得できなかった範囲は、最初に取得できた回を基準にする。
        accepted.update(k for k in current if newly_baselined(k))
        pending_dates &= failed_dates
        pending_rooms &= set(failed_rooms)

    def checkpoint():
        if not args.dry_run:
            save_state(raw_saved, list(accepted), ever, criteria_hash=fingerprint,
                       missing_counts=counts,
                       baseline_pending={"dates": sorted(pending_dates), "rooms": sorted(pending_rooms)})

    forced = args.notify_all
    if baseline and forced:
        accepted = set()
    targets = current if forced else (set() if baseline else current - accepted)
    lost = []
    if not baseline:
        confirmations = max(1, int(cfg.get("closed_confirmations", 2)))
        for key in previous - current:
            if failed(key):
                continue
            if explicit_status:
                # 川崎は空き以外も取得する。欠落だけを満室と断定しない。
                slots = groups.get(key, {}).get("slots", {})
                if not any(slots.get(slot) in {"full", "closed", "partially", "maintenance", "processing"}
                           for slot in cfg["required_slots"]):
                    continue
            count = int(state.get("missing_counts", {}).get(key, 0)) + 1
            counts[key] = count
            if count >= confirmations:
                lost.append(key)
        if not cfg.get("notify_filled", False) or forced:
            accepted.difference_update(lost)
            for k in lost:
                counts.pop(k, None)
            lost = []
    ever.update(accepted)
    # 送信前の保存には未送信の新規キーを入れない。
    checkpoint()

    def opened_sent(keys):
        accepted.update(keys)
        ever.update(keys)
        checkpoint()

    def closed_sent(keys):
        accepted.difference_update(keys)
        for k in keys:
            counts.pop(k, None)
        checkpoint()

    ordered = sorted(targets, key=lambda k: (matched[k]["date"], k))
    print(f"[INFO] 現在の連続空き {len(current)}件 / 新規通知 {len(ordered)}件 / 満室通知 {len(lost)}件")
    try:
        if ordered:
            notify_discord(cfg.get("discord_webhook_url", ""),
                           [(k, matched[k], k in ever) for k in ordered], args.dry_run,
                           mention=cfg.get("discord_mention", ""), on_sent=opened_sent)
        if lost:
            notify_lost(cfg.get("discord_webhook_url", ""), sorted(lost, key=lambda k: (k.split("|")[-1], k)),
                        args.dry_run, on_sent=closed_sent)
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return False
    if args.dry_run:
        print("[DRY-RUN] 状態ファイルは変更していません")
    return True


def run_once(cfg: dict, args) -> bool:
    raw, ok, failed_ranges = fetch_availability(cfg, debug=args.debug)
    if not ok and not failed_ranges:
        print("[ERROR] 取得の完了を確認できません。状態を更新しません")
        return False
    return process_snapshot(cfg, args, raw, failed_ranges=failed_ranges)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--notify-all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-state", action="store_true", help="通知せず現在の状態を基準として再登録")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="取得結果の要約をテスト通知としてDiscordへ必ず送る")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="設定ファイルのパス")
    ap.add_argument("--state", default=None, help="状態ファイルのパス")
    args = ap.parse_args()
    if args.reset_state and (args.notify_all or args.test):
        ap.error("--reset-state は --notify-all / --test と併用できません")

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
        now = now_jst()
        if start_h <= now.hour < end_h:
            try:
                if run_once(cfg, args):
                    args.reset_state = False
            except Exception as e:
                print(f"[ERROR] チェック中に例外: {e}")
        else:
            print(f"[INFO] {now:%H:%M} は稼働時間外のためスキップ")
        time.sleep(interval)


if __name__ == "__main__":
    main()
