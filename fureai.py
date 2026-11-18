#!/usr/bin/env python3
"""
川崎市 ふれあいネット（SP画面）会議室 空き監視 → Discord通知

経路: /sp/ → 施設空き状況 → 地域から → 区 → 館 → 部屋 → 期間設定(土日祝チェック) →
      検索開始 → 時間帯別空き状況を解析。
判定・差分・通知は monitor.py の共通エンジンを再利用する。

使い方:
    python fureai.py               # 1回チェック（差分があれば通知）
    python fureai.py --test        # テスト通知（現在の成立一覧を必ず送る）
    python fureai.py --webhook-only  # 部屋を検索せず、ふれあい用の通知先だけを確認
    python fureai.py --debug      # 各画面をdebug/へ保存
    python fureai.py --dry-run    # Discordへ送信しない
"""
import argparse
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import monitor as core
import booking_rules as booking

BASE_DIR = Path(__file__).parent
SP_URL = "https://www.fureai-net.city.kawasaki.jp/sp/"
MARK_AVAILABLE = ("空き", "○", "◯", "〇")
MARK_PARTIAL = ("一部",)
LIST_TIMEOUT_MS = 20000
MAX_LIST_PAGES = 30
FUREAI_REVISION = "2026-11-18"

# jQuery Mobile が残す非表示の旧画面を操作・解析しない。
_DOM_SETUP = r"""
    const norm = s => (s || '').normalize('NFKC').replace(/[\s\u200b]+/g, '');
    const pages = Array.from(document.querySelectorAll('[data-role="page"]'));
    const root = document.querySelector('.ui-page-active')
        || (pages.length === 1 ? pages[0] : pages.length === 0 ? document.body : null);
    const visible = e => {
        const style = getComputedStyle(e);
        return e.getClientRects().length > 0 && style.display !== 'none'
            && style.visibility !== 'hidden' && !e.closest('[hidden], [aria-hidden="true"]')
            && !e.disabled && e.getAttribute('aria-disabled') !== 'true'
            && !e.classList.contains('ui-disabled');
    };
    const label = e => (e.innerText || e.value || e.textContent || '').trim();
    const links = root ? Array.from(root.querySelectorAll('a, input[type=submit], button'))
        .filter(visible) : [];
"""

_LIST_VIEW_JS = "() => {" + _DOM_SETUP + r"""
    const heading = root && root.querySelector('.title');
    const title = heading ? label(heading) : document.title;
    const text = root ? root.innerText : '';
    const kind = title.includes('館選択') ? '館選択'
        : title.includes('施設選択') ? '施設選択' : '';
    const choices = root ? Array.from(root.querySelectorAll('ul[data-role="listview"] a'))
        .filter(visible).map(label).filter(Boolean) : [];
    const normalized = norm(text);
    const range = normalized.match(/(\d+)[~〜～-](\d+)件を表示/);
    const total = normalized.match(/(\d+)件の候補/);
    const expected = range ? Number(range[2]) - Number(range[1]) + 1 : null;
    const complete = expected !== null ? expected > 0 && choices.length === expected
        : total && Number(total[1]) === 0;
    const ready = !!(root && kind && document.readyState !== 'loading'
        && !document.documentElement.classList.contains('ui-mobile-rendering') && complete);
    return {kind, ready, labels: choices,
        signature: JSON.stringify([kind, range && range[0], choices.map(norm)]),
        next: links.some(e => norm(label(e)) === '次へ'),
        previous: links.some(e => norm(label(e)) === '前へ')};
}"""


def normalize_label(text):
    return re.sub(r"[\s\u200b]+", "", unicodedata.normalize("NFKC", text))


def wait_for_list(page, kind, previous=None):
    """見出しだけでなく、表示件数分のリンクとページ送りの完了を待つ。"""
    script = "(p) => { const v = (" + _LIST_VIEW_JS + ")(); " + (
        "return v.ready && v.kind === p.kind && v.signature !== p.previous ? v : false; }"
    )
    try:
        handle = page.wait_for_function(
            script, arg={"kind": kind, "previous": previous}, timeout=LIST_TIMEOUT_MS)
        try:
            return handle.json_value()
        finally:
            handle.dispose()
    except PWTimeout as e:
        raise ValueError(f"{kind}一覧の読み込み・ページ切替が完了しません") from e


def first_list_page(page, kind):
    view = wait_for_list(page, kind)
    seen = set()
    for _ in range(MAX_LIST_PAGES):
        if view["signature"] in seen:
            raise ValueError(f"{kind}一覧の前ページ送りが循環しています")
        seen.add(view["signature"])
        if not view["previous"]:
            return view
        if not click_text(page, "前へ", exact=True):
            raise ValueError(f"{kind}一覧の先頭へ戻れません")
        view = wait_for_list(page, kind, previous=view["signature"])
    raise ValueError(f"{kind}一覧のページ数上限に達しました")


def select_list_item(page, text, kind, *, allow_partial=False, aliases=()):
    """館・部屋を全ページから探す。完全一致を優先し、曖昧な候補は選ばない。"""
    wanted = {normalize_label(t) for t in (text, *aliases)}
    view = first_list_page(page, kind)
    seen, labels, partial = set(), [], []
    for page_number in range(MAX_LIST_PAGES):
        if view["signature"] in seen:
            raise ValueError(f"{kind}一覧の次ページ送りが循環しています")
        seen.add(view["signature"])
        labels.extend(view["labels"])
        exact = [s for s in view["labels"] if normalize_label(s) in wanted]
        if len(exact) > 1:
            raise ValueError(f"「{text}」の候補が複数あります: {exact}")
        if exact:
            if not click_text(page, exact[0], exact=True):
                raise ValueError(f"「{text}」の選択に失敗しました")
            return
        if allow_partial:
            partial.extend((page_number, s) for s in view["labels"]
                           if any(t in normalize_label(s) for t in wanted))
        if not view["next"]:
            break
        print(f"[INFO] {kind}一覧: 「{text}」を次ページで探します")
        if not click_text(page, "次へ", exact=True):
            raise ValueError(f"{kind}一覧の次ページへ進めません")
        view = wait_for_list(page, kind, previous=view["signature"])
    else:
        raise ValueError(f"{kind}一覧のページ数上限に達しました")

    # 設定中の「老人福祉」等の略称は、全ページで一意な館だけを許可。
    if allow_partial and len(partial) == 1:
        number, label = partial[0]
        view = first_list_page(page, kind)
        for _ in range(number):
            if not click_text(page, "次へ", exact=True):
                raise ValueError(f"{kind}一覧の候補ページへ戻れません")
            view = wait_for_list(page, kind, previous=view["signature"])
        if label in view["labels"] and click_text(page, label, exact=True):
            return
        raise ValueError(f"「{text}」の選択に失敗しました")
    if len(partial) > 1:
        raise ValueError(f"「{text}」に一致する館が複数あります: {[s for _, s in partial]}")
    raise ValueError(f"「{text}」が一覧にありません。表示された候補: {', '.join(labels)}")


def dump(page, tag, screenshot=True):
    core.DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if screenshot:
        try:
            page.screenshot(path=str(core.DEBUG_DIR / f"{ts}_fu_{tag}.png"), full_page=True)
        except Exception:
            pass
    try:
        (core.DEBUG_DIR / f"{ts}_fu_{tag}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    print(f"[DEBUG] {tag} を保存")


def wait(page, sec=1.2):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    time.sleep(sec)


def safe_eval(page, script, arg=None):
    """ページ遷移中の「実行コンテキスト破棄」に耐えるevaluate（最大3回リトライ）"""
    last = None
    for i in range(3):
        try:
            return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        except Exception as e:
            last = e
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            time.sleep(0.8)
    raise last


def click_text(page, text, *, exact=False) -> bool:
    ok = safe_eval(page,
        "(p) => {" + _DOM_SETUP + r"""
            if (document.readyState === 'loading'
                || document.documentElement.classList.contains('ui-mobile-rendering')) return false;
            const t = norm(p.text);
            let hits = links.filter(e => norm(label(e)) === t);
            if (!hits.length && !p.exact) hits = links.filter(e => norm(label(e)).includes(t));
            if (hits.length === 1) { hits[0].click(); return true; }
            return false;
        }""", {"text": text, "exact": exact})
    if not ok:
        print(f"[WARN] 「{text}」を一意に選択できません")
    return ok


def parse_vacancy(page):
    """施設空き検索結果(時間帯貸し)画面を解析する。1日分の表示:
    「… 2026年7月18日(土) 空き情報 午前 × 午後 × 夜間 ○ …」
    戻り値: (iso_date, {slot: state}) / 解析不能なら (None, {})"""
    text = safe_eval(page, "() => {" + _DOM_SETUP
                     + "return root ? root.innerText.replace(/\\s+/g, ' ') : ''; }")
    d = core.parse_date_from_text(text)
    if not d:
        return None, {}
    states = {}
    for slot in ("午前", "午後", "夜間"):
        m = re.search(slot + r"[ \u3000]*([○◯〇◎△×✕－\-]|空きなし|一部空き|空き|保守日・主催事業|休館)", text)
        if not m:
            continue
        mark = m.group(1)
        if mark in ("○", "◯", "〇", "◎", "空き"):
            states[slot] = "available"
        elif mark in ("△", "一部空き"):
            states[slot] = "partially"
        elif mark in ("×", "✕", "空きなし"):
            states[slot] = "full"
        else:
            # 休館・主催事業などは利用不可。翌日以降の取得は続ける。
            states[slot] = "closed"
    return d.isoformat(), states


def wait_for_result(page, earliest, after=None):
    """表示中の結果の日付と空き情報が揃うまで待つ。古い画面の再読を防ぐ。"""
    script = "(p) => {" + _DOM_SETUP + r"""
        if (!root || document.readyState === 'loading' ||
            document.documentElement.classList.contains('ui-mobile-rendering')) return false;
        const txt = root.innerText.replace(/\s+/g, ' ');
        const d = txt.match(/(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日/);
        if (!d || !/空き情報.*(?:午前|午後|夜間)/.test(txt)) return false;
        const iso = d[1]+'-'+d[2].padStart(2,'0')+'-'+d[3].padStart(2,'0');
        return iso >= p.earliest && (!p.after || iso > p.after);
    }"""
    try:
        handle = page.wait_for_function(script, arg={"earliest": earliest, "after": after},
                                        timeout=LIST_TIMEOUT_MS, polling=150)
        handle.dispose()
    except PWTimeout as e:
        raise ValueError("結果の日付送り・空き情報の読み込みが完了しません") from e


def walk_to_room_list(page, ward: str, kan: str) -> bool:
    """トップから 施設空き状況→地域から→区→館 と辿って部屋一覧に立つ。"""
    try:
        page.goto(SP_URL, wait_until="domcontentloaded", timeout=60000)
        wait(page)
        if not click_text(page, "施設空き状況"):
            return False
        wait(page)
        if not click_text(page, "地域から"):
            return False
        wait(page)
        if not click_text(page, ward):
            return False
        wait(page)
        select_list_item(page, kan, "館選択", allow_partial=True)
        wait_for_list(page, "施設選択")
        return True
    except Exception as e:
        print(f"[WARN] {ward}/{kan} への移動でエラー: {type(e).__name__}: {e}")
        return False


def on_room_list(page) -> bool:
    try:
        view = safe_eval(page, _LIST_VIEW_JS)
        return view["kind"] == "施設選択" and view["ready"]
    except Exception:
        return False


def collect_room_slots(page, cfg, horizon_end):
    """途中で同じ日へ戻る・日付/必須区分が読めない場合は部屋単位で保留する。"""
    pairs = {}
    previous = None
    for _ in range(160):
        if cfg.get('_room_deadline') is not None and time.monotonic() >= cfg['_room_deadline']:
            if pairs:
                # 確認済みの日だけを上位へ返す。残りは期待日との照合で取得保留になる。
                return pairs
            raise ValueError("1室の検索時間上限に達しました")
        iso, states = parse_vacancy(page)
        if not iso or (previous and iso <= previous):
            raise ValueError("日付送りの完了を確認できません")
        if iso > horizon_end:
            return pairs
        # 老人福祉センターの一般貸出は日曜・祝日（敬老の日を除く）のみ昼間あり。
        # 公式に昼間貸出がない日は、表示に午後欄がなくても取得失敗にしない。
        if cfg.get("day_policy") == "sundays_and_holidays_except_keiro":
            d = date.fromisoformat(iso)
            keiro = d.month == 9 and d.weekday() == 0 and 15 <= d.day <= 21
            if keiro or not (d.weekday() == 6 or core.jpholiday.is_holiday(d)):
                states["午後"] = "closed"
        missing = [slot for slot in cfg["required_slots"] if slot not in states]
        if missing:
            raise ValueError(f"{iso}: 必須時間帯 {', '.join(missing)} を解析できません"
                             f"（読み取れた時間帯: {', '.join(states) or 'なし'}）")
        previous = iso
        for slot, status in states.items():
            pairs[(iso, slot)] = status
        if iso == horizon_end or not click_text(page, "翌日"):
            return pairs
        # 連続リクエストを詰めず、通信全体のアイドル判定ではなく必要な画面を待つ。
        time.sleep(1.1)
        wait_for_result(page, iso, after=iso)
    raise ValueError("日付送りの上限に達しました")


def open_room(page, ward, kan, room_cfg):
    if not on_room_list(page) and not walk_to_room_list(page, ward, kan):
        raise ValueError("部屋一覧に復帰できません")
    select_list_item(page, room_cfg["name"], "施設選択",
                     aliases=room_cfg.get("aliases", ()))
    # タイトルだけ先に現れる遷移途中では、フォームを操作しない。
    page.wait_for_function("() => {" + _DOM_SETUP + """
        const f = root && root.querySelector('form');
        return f && ['selectYear', 'selectMonth', 'selectDay'].every(n => f.elements.namedItem(n));
    }""", timeout=LIST_TIMEOUT_MS)


def start_search(page, today):
    safe_eval(page, "(p) => {" + _DOM_SETUP + """
        const f = root && root.querySelector('form');
        if (!f) throw new Error('期間設定フォームがありません');
        f.elements.namedItem('selectYear').value = String(p.y);
        f.elements.namedItem('selectMonth').value = String(p.m).padStart(2, '0');
        f.elements.namedItem('selectDay').value = String(p.d).padStart(2, '0');
        f.querySelectorAll('input[name=srchSelectWeek]').forEach(cb => {
            cb.checked = ['6', '7', '8'].includes(cb.value);
        });
    }""", {"y": today.year, "m": today.month, "d": today.day})
    if not click_text(page, "検索開始", exact=True):
        raise ValueError("検索開始ボタンを選択できません")
    wait_for_result(page, today.isoformat())


def return_to_room_list(page):
    for _ in range(3):
        view = safe_eval(page, _LIST_VIEW_JS)
        if view["kind"] == "施設選択":
            wait_for_list(page, "施設選択")
            return
        if not click_text(page, "もどる", exact=True):
            return  # 次の部屋ではトップから復帰する。
        wait(page, 0.6)


def room_plan(cfg):
    return [(t, r, f"{t['kan']}・{r['name']}") for t in cfg["targets"]
            for r in t['rooms'] if booking.candidate(r, cfg)]


class RoomSweep:
    """通常回の巡回順。通知条件・通知履歴と切り離し、一度に各室を最大1回処理する。"""
    def __init__(self, cfg, state):
        rows = room_plan(cfg)
        self.labels = [row[2] for row in rows]
        if not rows or len(set(self.labels)) != len(rows):
            raise ValueError("ふれあいネットの対象室が空か、識別名が重複しています")
        cursor = state.get('fureai_scan') or {}
        self.next_room = cursor.get('next_room') if isinstance(cursor, dict) else None
        if self.next_room not in self.labels:
            self.next_room = self.labels[0]
            if state.get('criteria_hash') == core.criteria_hash(cfg):
                # 旧版は、取得した室の枠をslotsの末尾へ書き直していた。
                # 初回のみ順序の手掛かりにする。空き・既通知の判定には使わない。
                for key in reversed(state.get('slots', {})):
                    last = key.split('|', 1)[0]
                    if last in self.labels:
                        self.next_room = self.successor(last)
                        break
        start = self.labels.index(self.next_room)
        self.rows = rows[start:] + rows[:start]
        self.attempted = []
        self.started = time.monotonic()
        self.budget = float(cfg.get('scan_budget_minutes', 24)) * 60
        if not 0 < self.budget <= 30 * 60:
            raise ValueError("scan_budget_minutes は0より大きく30以下で指定してください")

    def successor(self, room):
        return self.labels[(self.labels.index(room) + 1) % len(self.labels)]

    def persist_start(self, state):
        cursor = state.get('fureai_scan')
        if isinstance(cursor, dict) and cursor.get('next_room') == self.next_room:
            return
        # 初回の送信が失敗しても、旧slotsの書き込み順から再推定して飛ばさない。
        doc = dict(state) if state else {'slots': {}, 'matched': [], 'ever_matched': []}
        doc['fureai_scan'] = {'next_room': self.next_room}
        core.write_state_document(doc)

    def time_to_yield(self):
        return bool(self.attempted) and time.monotonic() - self.started >= self.budget

    def advance(self, room, outcome):
        # Discord送信／既通知キーの確定より先に位置を進めない。
        # 解析失敗は未知のまま1周後に再試行可能にする。失敗を基準登録しない。
        next_room = self.successor(room)
        state = core.load_state() or {'slots': {}, 'matched': [], 'ever_matched': []}
        state['fureai_scan'] = {'next_room': next_room, 'last_room': room, 'last_result': outcome}
        core.write_state_document(state)
        self.attempted.append(room)
        self.next_room = next_room

    def remaining(self):
        return [r for _, _, r in self.rows if r not in self.attempted]


def observed_ranges(days):
    """実際に取得できた日だけを、既存の差分エンジンに渡せる連続範囲にする。"""
    ranges = []
    for day in sorted(days):
        if ranges and ranges[-1][0] + timedelta(days=ranges[-1][1]) == day:
            ranges[-1][1] += 1
        else:
            ranges.append([day, 1])
    return [(day.isoformat(), count) for day, count in ranges]


def fetch_availability(cfg: dict, debug: bool, on_room=None, *, sweep=None):
    all_slots = {}
    errors = []
    failed_rooms = set()
    today, end = core.search_bounds(cfg)
    focus = None
    if cfg.get('_scan_windows'):
        focus = {date.fromisoformat(s) + timedelta(days=i)
                 for s, n in cfg['_scan_windows'] for i in range(int(n))}
        today, end = min(focus), max(focus)
    dumped_sample = False
    rows = sweep.rows if sweep is not None else room_plan(cfg)
    complete, skipped, visited = [], [], []
    def finish(room, outcome):
        if sweep is not None:
            sweep.advance(room, outcome)
        visited.append(room)

    def trace(event, **detail):
        if cfg.get('notification_diagnostics'):
            core.trace_event(cfg, event, **detail)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def fresh_page(old=None):
            """失敗したセッションを閉じ、新しいセッションで1回だけ再試行する。"""
            if old is not None:
                try:
                    old.context.close()
                except Exception:
                    pass
            return browser.new_context(
                locale="ja-JP", timezone_id="Asia/Tokyo",
                viewport={"width": 480, "height": 1400}).new_page()

        page = fresh_page()
        try:
            if cfg.get('_not_before'):
                core.wait_until(datetime.fromisoformat(cfg['_not_before']))
            for (ward, kan), grouped in groupby(rows, key=lambda v: (v[0]['ward'], v[0]['kan'])):
                if sweep is not None and sweep.time_to_yield():
                    break
                active_rooms = list(grouped)
                print(f"[INFO] === {ward} / {kan} ===")
                trace("building_start", facility=kan, first_room=active_rooms[0][2])
                reached = walk_to_room_list(page, ward, kan)
                if not reached:
                    print(f"[INFO] {kan}: 新しいセッションで再試行します")
                    page = fresh_page(page)
                    time.sleep(3)
                    reached = walk_to_room_list(page, ward, kan)
                if not reached:
                    errors.append(f"{ward}/{kan}: 部屋一覧に到達できません")
                    for _, _, label in active_rooms:
                        failed_rooms.add(label)
                        finish(label, 'failed')
                    dump(page, f"kanfail_{kan}", screenshot=False)
                    continue

                for _, room_cfg, room_label in active_rooms:
                    if sweep is not None and sweep.time_to_yield():
                        break
                    rname = room_cfg["name"]
                    room_start, room_end = today, end
                    expected = None
                    if cfg.get('booking_policy'):
                        entry = dict(room_cfg, facility=kan)
                        checked_at = core.now_jst()
                        days = [today + timedelta(days=i) for i in range((end-today).days + 1)]
                        days = [d for d in days if (focus is None or d in focus)
                                and booking.status(d, cfg, entry, checked_at)[0]]
                        expected = {d for d in days if core.is_target_day(d, '')}
                        if not expected:
                            skipped.append(room_label)
                            finish(room_label, 'no_eligible_dates')
                            continue
                        room_start, room_end = min(expected), max(expected)
                    trace("room_start", room=room_label, first_date=room_start.isoformat(),
                          last_date=room_end.isoformat())
                    room_ts = time.monotonic()
                    room_deadline = room_ts + 5 * 60 if sweep is not None else None
                    pairs = None
                    for attempt in range(2):
                        try:
                            open_room(page, ward, kan, room_cfg)
                            start_search(page, room_start)
                            if debug and not dumped_sample:
                                dump(page, f"result_sample_{kan}_{rname}")
                                dumped_sample = True
                            # 解析エラーが出た試行のデータを別の試行に継ぎ足さない。
                            room_options = dict(cfg)
                            room_options['_room_deadline'] = room_deadline
                            if room_cfg.get("day_policy"):
                                room_options["day_policy"] = room_cfg["day_policy"]
                            pairs = collect_room_slots(page, room_options, room_end.isoformat())
                            if expected is not None:
                                pairs = {k: v for k, v in pairs.items() if date.fromisoformat(k[0]) in expected}
                            if not pairs:
                                raise ValueError("空き状況を解析できません")
                            break
                        except Exception as e:
                            pairs = None
                            print(f"[WARN] {kan}/{rname}: {type(e).__name__}: {e}")
                            if attempt == 0 and (room_deadline is None or time.monotonic() < room_deadline):
                                print(f"[INFO] {kan}/{rname}: 新しいセッションで再試行します")
                                page = fresh_page(page)
                                time.sleep(3)
                            else:
                                errors.append(f"{kan}/{rname}: {type(e).__name__}: {e}")
                                failed_rooms.add(room_label)
                                dump(page, f"roomfail_{kan}_{rname}", screenshot=False)
                                break
                    if pairs is None:
                        trace("room_failed", room=room_label, seconds=round(time.monotonic()-room_ts, 1))
                        finish(room_label, 'failed')
                        continue
                    seen = {date.fromisoformat(iso) for iso, _ in pairs}
                    missing = sorted((expected or seen) - seen)
                    if missing:
                        errors.append(f"{room_label}: 対象日のうち{len(missing)}日を未取得")
                        print(f"[WARN] {errors[-1]}")
                        failed_rooms.add(room_label)
                    n = sum(1 for v in pairs.values() if v in core.AVAILABLE_STATES)
                    print(f"[INFO] {kan}/{rname}: {len(pairs)}コマ（空き{n}）")
                    for (iso, slot), state in pairs.items():
                        all_slots[f"{room_label}|{slot}|{iso}"] = state
                    if on_room is not None:
                        raw = {f'{room_label}|{slot}|{iso}': state for (iso, slot), state in pairs.items()}
                        on_room(raw, room_label, observed_ranges(seen))
                    if not missing:
                        complete.append(room_label)
                    finish(room_label, 'partial' if missing else 'checked')
                    trace("room_finish", room=room_label, days=len(seen), missing_dates=[d.isoformat() for d in missing],
                          seconds=round(time.monotonic()-room_ts, 1))
                    time.sleep(0.8)
                    try:
                        return_to_room_list(page)
                    except Exception as e:
                        # 収集済みのデータは有効。次の部屋でトップから復帰する。
                        print(f"[WARN] {kan}: 部屋一覧へ戻れません: {e}")
        finally:
            browser.close()
    deferred = [label for _, _, label in rows if label not in visited]
    print(f"[INFO] 取得結果: 完了{len(complete)}/{len(rows)}室、取得不完全{len(failed_rooms)}室、"
          f"受付対象日なし{len(skipped)}室、次回へ{len(deferred)}室")
    for room in sorted(failed_rooms):
        print(f"[WARN] 取得保留: {room}")
    return all_slots, not errors, errors, failed_rooms


def apply_fees(matched: dict, cfg: dict):
    """旧呼出箇所とも互換に、共通の料金・定員・利用条件を付与する。"""
    for g in matched.values():
        entry = core.match_room(g["room"], core.room_catalog(cfg))
        if entry:
            core.annotate_room(g, entry, cfg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--webhook-only", action="store_true", help="検索・履歴更新をせずDiscord通知先を確認")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument('--opening-watch', action='store_true')
    ap.add_argument("--notify-all", action="store_true")
    ap.add_argument("--reset-state", action="store_true", help="通知せず現在の状態を基準として再登録")
    ap.add_argument("--config", default=str(BASE_DIR / "config_fureai.yaml"))
    ap.add_argument("--state", default=None)
    args = ap.parse_args()

    if args.reset_state and (args.notify_all or args.test):
        ap.error("--reset-state は --notify-all / --test と併用できません")
    if args.opening_watch and any((args.test, args.reset_state, args.notify_all)):
        ap.error('--opening-watch はtest/reset-state/notify-allと併用できません')
    if args.webhook_only and any((args.test, args.reset_state, args.notify_all, args.opening_watch)):
        ap.error('--webhook-only は単独で指定してください')
    cfg = core.load_config(args.config)
    if (cfg.get('fureai_scan_revision') != FUREAI_REVISION or
            not callable(getattr(core, 'write_state_document', None))):
        ap.exit(1, "[ERROR] fureai.py・monitor.py・config_fureai.yaml を11月18日版で揃えてください。\n")
    print(f"[INFO] ふれあい巡回版: {FUREAI_REVISION}")
    if not args.dry_run and not args.reset_state and not cfg.get('discord_webhook_url'):
        ap.exit(1, "[ERROR] ふれあいネットの通知先が未設定です。ActionsのSecret DISCORD_WEBHOOK_FUREAI を確認してください。\n")
    if args.webhook_only:
        try:
            core.post_discord(cfg.get('discord_webhook_url', ''), {
                'content': 'ふれあいネット：通知先の接続テストです。部屋の検索・通知履歴の変更はしていません。',
                'allowed_mentions': {'parse': []}
            }, args.dry_run)
        except RuntimeError as error:
            ap.exit(1, f"[ERROR] ふれあいネット通知先テスト: {error}\n")
        return

    # 共通エンジンのグローバルをふれあいネット用に設定
    core.SLOT_WINDOWS = {k: tuple(v) for k, v in cfg["slot_windows"].items()}
    core.TIME_SLOT_WORDS = tuple(sorted(core.SLOT_WINDOWS.keys(), key=len, reverse=True))
    core.NOTIFY_LABEL = cfg.get("notify_label", "午後＋夜間")
    core.STATE_PATH = Path(args.state) if args.state else BASE_DIR / cfg.get("state_file", "state_fureai.json")
    core.BASE_URL = SP_URL

    if args.opening_watch:
        cfg = core.opening_config(cfg, args)
        if cfg is None:
            return
    if not cfg.get('_not_before') and not booking.service_open(cfg, core.now_jst()):
        print('[INFO] 予約システム受付時間外。検索せず履歴を保持します')
        return

    if cfg.get('notify_during_scan') and not any((args.test, args.reset_state, args.dry_run, args.notify_all)):
        success = run_incremental(cfg, args)
        sys.exit(0 if success else 1)

    raw, ok, errors, failed_rooms = fetch_availability(cfg, debug=args.debug or args.test)

    if args.test:
        matched = core.find_matched(core.build_groups(raw), cfg)
        apply_fees(matched, cfg)
        core.send_test_notification(cfg, raw, ok, dry_run=args.dry_run,
                                    matched=matched, errors=errors)
        sys.exit(0)

    if not ok and not failed_rooms:
        print("[ERROR] 取得の完了を確認できません。状態を更新しません")
        sys.exit(1)
    success = core.process_snapshot(cfg, args, raw, failed_rooms=failed_rooms,
                                    explicit_status=True, annotate=apply_fees)
    sys.exit(0 if success and ok else 1)


def run_incremental(cfg, args):
    rooms = {r for _, _, r in room_plan(cfg)}
    opening = bool(cfg.get('_scan_windows'))
    sweep = None
    if not opening:
        state = core.load_state()
        sweep = RoomSweep(cfg, state)
        sweep.persist_start(state)
    first = sweep.next_room if sweep is not None else None
    print("[INFO] " + (f"通常巡回を「{first}」から再開 / 目安{sweep.budget/60:g}分" if sweep else "受付開始日の集中確認"))
    started = time.monotonic()
    core.trace_event(cfg, 'run_start', revision=FUREAI_REVISION, mode='opening' if opening else 'normal',
                     target_rooms=len(rooms), first_room=first, budget_seconds=sweep.budget if sweep else None)
    checked = set()
    failed = set()
    errors = []
    result = "interrupted"
    def consume(raw, room, ranges):
        if not core.process_snapshot(cfg, args, raw, failed_rooms=rooms-{room},
                                     observed_ranges=ranges, explicit_status=True,
                                     positive_only=opening):
            raise RuntimeError('通知に失敗したため検索を停止。送信済みの履歴は保存しました')
        checked.add(room)
    try:
        raw, ok, errors, failed = fetch_availability(cfg, args.debug, on_room=consume, sweep=sweep)
        if checked and opening:
            ranges = [(s, int(n)) for s, n in cfg['_scan_windows']]
            if not core.process_snapshot(cfg, args, raw, failed_rooms=failed,
                                         observed_ranges=ranges, explicit_status=True):
                result = "failed"
                return False
        if not ok or (not checked and (not sweep or not sweep.attempted)):
            result = "failed"
            return False
        result = "deferred" if sweep and sweep.remaining() else "complete"
        return True
    except RuntimeError as error:
        print(f'[ERROR] {error}')
        result = "failed"
        return False
    finally:
        deferred = sweep.remaining() if sweep else sorted(rooms - checked - failed)
        core.trace_event(cfg, 'run_end', revision=FUREAI_REVISION, status=result,
                         completed_rooms=len(checked - failed), failed_rooms=sorted(failed),
                         deferred_rooms=deferred, next_room=sweep.next_room if sweep else None,
                         error_count=len(errors), seconds=round(time.monotonic()-started, 1))
        if sweep:
            print(f"[INFO] 巡回結果: {result} / 次回の先頭: {sweep.next_room} / 未巡回{len(deferred)}室")


if __name__ == "__main__":
    main()
