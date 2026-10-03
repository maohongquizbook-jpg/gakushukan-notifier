#!/usr/bin/env python3
"""
川崎市 ふれあいネット（SP画面）会議室 空き監視 → Discord通知

経路: /sp/ → 施設空き状況 → 地域から → 区 → 館 → 部屋 → 期間設定(土日祝チェック) →
      検索開始 → 時間帯別空き状況を解析。
判定・差分・通知は monitor.py の共通エンジンを再利用する。

使い方:
    python fureai.py               # 1回チェック（差分があれば通知）
    python fureai.py --test        # テスト通知（現在の成立一覧を必ず送る）
    python fureai.py --debug      # 各画面をdebug/へ保存
    python fureai.py --dry-run    # Discordへ送信しない
"""
import argparse
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import monitor as core

BASE_DIR = Path(__file__).parent
SP_URL = "https://www.fureai-net.city.kawasaki.jp/sp/"
MARK_AVAILABLE = ("空き", "○", "◯", "〇")
MARK_PARTIAL = ("一部",)
LIST_TIMEOUT_MS = 20000
MAX_LIST_PAGES = 30

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
        wait(page, 0.6)
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
    wait(page, 1.5)


def return_to_room_list(page):
    for _ in range(3):
        view = safe_eval(page, _LIST_VIEW_JS)
        if view["kind"] == "施設選択":
            wait_for_list(page, "施設選択")
            return
        if not click_text(page, "もどる", exact=True):
            return  # 次の部屋ではトップから復帰する。
        wait(page, 0.6)


def fetch_availability(cfg: dict, debug: bool):
    all_slots = {}
    errors = []
    failed_rooms = set()
    today, end = core.search_bounds(cfg)
    horizon_end = end.isoformat()
    dumped_sample = False

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
            for tgt in cfg["targets"]:
                active_rooms = [r for r in tgt["rooms"] if r.get("monitor", True)]
                if not active_rooms:
                    continue
                ward, kan = tgt["ward"], tgt["kan"]
                print(f"[INFO] === {ward} / {kan} ===")
                reached = walk_to_room_list(page, ward, kan)
                if not reached:
                    print(f"[INFO] {kan}: 新しいセッションで再試行します")
                    page = fresh_page(page)
                    time.sleep(3)
                    reached = walk_to_room_list(page, ward, kan)
                if not reached:
                    errors.append(f"{ward}/{kan}: 部屋一覧に到達できません")
                    failed_rooms.update(f"{kan}・{r['name']}" for r in active_rooms)
                    dump(page, f"kanfail_{kan}", screenshot=False)
                    continue

                for room_cfg in active_rooms:
                    rname = room_cfg["name"]
                    room_label = f"{kan}・{rname}"
                    pairs = None
                    for attempt in range(2):
                        try:
                            open_room(page, ward, kan, room_cfg)
                            start_search(page, today)
                            if debug and not dumped_sample:
                                dump(page, f"result_sample_{kan}_{rname}")
                                dumped_sample = True
                            # 途中まで取れた部屋も、失敗時は一切採用しない。
                            room_options = dict(cfg)
                            if room_cfg.get("day_policy"):
                                room_options["day_policy"] = room_cfg["day_policy"]
                            pairs = collect_room_slots(page, room_options, horizon_end)
                            if not pairs:
                                raise ValueError("空き状況を解析できません")
                            break
                        except Exception as e:
                            pairs = None
                            print(f"[WARN] {kan}/{rname}: {type(e).__name__}: {e}")
                            if attempt == 0:
                                print(f"[INFO] {kan}/{rname}: 新しいセッションで再試行します")
                                page = fresh_page(page)
                                time.sleep(3)
                            else:
                                errors.append(f"{kan}/{rname}: {type(e).__name__}: {e}")
                                failed_rooms.add(room_label)
                                dump(page, f"roomfail_{kan}_{rname}", screenshot=False)
                    if pairs is None:
                        continue
                    n = sum(1 for v in pairs.values() if v in core.AVAILABLE_STATES)
                    print(f"[INFO] {kan}/{rname}: {len(pairs)}コマ（空き{n}）")
                    for (iso, slot), state in pairs.items():
                        all_slots[f"{room_label}|{slot}|{iso}"] = state
                    time.sleep(0.8)
                    try:
                        return_to_room_list(page)
                    except Exception as e:
                        # 収集済みのデータは有効。次の部屋でトップから復帰する。
                        print(f"[WARN] {kan}: 部屋一覧へ戻れません: {e}")
        finally:
            browser.close()
    total = sum(r.get("monitor", True) for t in cfg["targets"] for r in t["rooms"])
    print(f"[INFO] 取得結果: 成功{total - len(failed_rooms)}/{total}室、保留{len(failed_rooms)}室")
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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-all", action="store_true")
    ap.add_argument("--reset-state", action="store_true", help="通知せず現在の状態を基準として再登録")
    ap.add_argument("--config", default=str(BASE_DIR / "config_fureai.yaml"))
    ap.add_argument("--state", default=None)
    args = ap.parse_args()

    if args.reset_state and (args.notify_all or args.test):
        ap.error("--reset-state は --notify-all / --test と併用できません")
    cfg = core.load_config(args.config)

    # 共通エンジンのグローバルをふれあいネット用に設定
    core.SLOT_WINDOWS = {k: tuple(v) for k, v in cfg["slot_windows"].items()}
    core.TIME_SLOT_WORDS = tuple(sorted(core.SLOT_WINDOWS.keys(), key=len, reverse=True))
    core.NOTIFY_LABEL = cfg.get("notify_label", "午後＋夜間")
    core.STATE_PATH = Path(args.state) if args.state else BASE_DIR / cfg.get("state_file", "state_fureai.json")
    core.BASE_URL = SP_URL

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
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
