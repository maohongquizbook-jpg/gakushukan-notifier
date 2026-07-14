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
from datetime import date, datetime
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import monitor as core

BASE_DIR = Path(__file__).parent
SP_URL = "https://www.fureai-net.city.kawasaki.jp/sp/"
MARK_AVAILABLE = ("空き", "○", "◯", "〇")
MARK_PARTIAL = ("一部",)


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


def click_text(page, text) -> bool:
    ok = page.evaluate(
        """(t) => {
            const links = Array.from(document.querySelectorAll('a, input[type=submit], button'));
            const hit = links.find(e => (e.innerText || e.value || '').trim() === t)
                     || links.find(e => (e.innerText || e.value || '').includes(t));
            if (hit) { hit.click(); return true; }
            return false;
        }""", text)
    if not ok:
        print(f"[WARN] 「{text}」が見つかりません")
    return ok


def parse_vacancy(page):
    """施設空き検索結果(時間帯貸し)画面を解析する。1日分の表示:
    「… 2026年7月18日(土) 空き情報 午前 × 午後 × 夜間 ○ …」
    戻り値: (iso_date, {slot: state}) / 解析不能なら (None, {})"""
    text = page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ')")
    d = core.parse_date_from_text(text)
    if not d:
        return None, {}
    states = {}
    for slot in ("午前", "午後", "夜間"):
        m = re.search(slot + r"[ \u3000]*([○◯〇◎△×✕－\-]|空きなし|一部空き|空き)", text)
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
            states[slot] = "closed"
    return d.isoformat(), states


def fetch_availability(cfg: dict, debug: bool):
    all_slots = {}
    ok = True
    errors = []
    today = date.today()
    dumped_sample = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            locale="ja-JP", viewport={"width": 480, "height": 1400}).new_page()

        for tgt in cfg["targets"]:
            ward, kan = tgt["ward"], tgt["kan"]
            print(f"[INFO] === {ward} / {kan} ===")
            try:
                page.goto(SP_URL, wait_until="domcontentloaded", timeout=60000)
                wait(page)
                click_text(page, "施設空き状況"); wait(page)
                click_text(page, "地域から"); wait(page)
                if not click_text(page, ward):
                    ok = False
                    errors.append(f"{ward}: 区リンクが見つかりません")
                    continue
                wait(page)
                if not click_text(page, kan):
                    print(f"[WARN] 館「{kan}」が見つかりません")
                    dump(page, f"kanfail_{kan}")
                    ok = False
                    errors.append(f"{ward}/{kan}: 館が見つかりません")
                    continue
                wait(page)

                # 部屋一覧を全ページ収集（「次へ」対応）し、対象部屋を順に処理
                for room_cfg in tgt["rooms"]:
                    rname = room_cfg["name"]
                    # 部屋一覧ページに戻っている前提。部屋リンクを探す（次へページも探索）
                    found = False
                    for _ in range(5):
                        if click_text(page, rname):
                            found = True
                            break
                        if not click_text(page, "次へ"):
                            break
                        wait(page, 0.8)
                    if not found:
                        print(f"[WARN] 部屋「{rname}」が見つかりません（{kan}）")
                        errors.append(f"{kan}/{rname}: 部屋が見つかりません")
                        dump(page, f"roomfail_{kan}_{rname}", screenshot=False)
                        # 一覧の先頭ページへ戻す
                        page.goto(SP_URL, wait_until="domcontentloaded", timeout=60000)
                        wait(page)
                        click_text(page, "施設空き状況"); wait(page)
                        click_text(page, "地域から"); wait(page)
                        click_text(page, ward); wait(page)
                        click_text(page, kan); wait(page)
                        continue
                    wait(page)

                    # 期間設定: 今日の日付＋土日祝チェック → 検索開始
                    page.evaluate(
                        """(p) => {
                            const f = document.FORM1 || document.forms[0];
                            if (!f) return;
                            if (f.selectYear) f.selectYear.value = String(p.y);
                            if (f.selectMonth) f.selectMonth.value = String(p.m).padStart(2, '0');
                            if (f.selectDay) f.selectDay.value = String(p.d).padStart(2, '0');
                            document.querySelectorAll('input[name=srchSelectWeek]').forEach(cb => {
                                cb.checked = ['6', '7', '8'].includes(cb.value);
                            });
                        }""",
                        {"y": today.year, "m": today.month, "d": today.day})
                    if not click_text(page, "検索開始"):
                        page.evaluate("() => (document.FORM1 || document.forms[0]).submit()")
                    wait(page, 1.5)

                    if debug and not dumped_sample:
                        dump(page, f"result_sample_{kan}_{rname}")
                        dumped_sample = True

                    # 土日祝フィルタ済みの1日表示を「翌日」で送りながら収集
                    from datetime import timedelta
                    horizon_end = (today + timedelta(
                        days=int(cfg.get("horizon_days", 70)))).isoformat()
                    pairs = {}
                    prev_iso = None
                    for _ in range(80):
                        iso, states = parse_vacancy(page)
                        if not iso or iso == prev_iso:
                            break
                        prev_iso = iso
                        for slot, state in states.items():
                            pairs[(iso, slot)] = state
                        if iso >= horizon_end:
                            break
                        if not click_text(page, "翌日"):
                            break
                        wait(page, 0.6)

                    if not pairs:
                        print(f"[WARN] {kan}/{rname}: 空き状況を解析できませんでした")
                        dump(page, f"parsefail_{kan}_{rname}", screenshot=False)
                        ok = False
                        errors.append(f"{kan}/{rname}: 空き状況を解析できず")
                    else:
                        n = sum(1 for v in pairs.values() if v in core.AVAILABLE_STATES)
                        print(f"[INFO] {kan}/{rname}: {len(pairs)}コマ（空き{n}）")
                        room_label = f"{kan}・{rname}"
                        for (iso, slot), state in pairs.items():
                            key = f"{room_label}|{slot}|{iso}"
                            all_slots[key] = state
                            # 料金・定員表示用
                    time.sleep(1)

                    # 部屋一覧へ戻る
                    for back in ("もどる", "戻る"):
                        if click_text(page, back):
                            break
                    wait(page, 0.8)
            except Exception as e:
                print(f"[ERROR] {ward}/{kan} でエラー: {e}")
                ok = False
                errors.append(f"{ward}/{kan}: 実行時エラー {type(e).__name__}")
        browser.close()
    if errors:
        ok = False
    return all_slots, ok, errors


def apply_fees(matched: dict, cfg: dict):
    """成立グループに料金を付与する。"""
    fee_map = {}
    for tgt in cfg["targets"]:
        for r in tgt["rooms"]:
            fee_map[f"{tgt['kan']}・{r['name']}"] = r.get("fee")
    import unicodedata
    for g in matched.values():
        norm = unicodedata.normalize("NFKC", g["room"])
        for name, fee in fee_map.items():
            if unicodedata.normalize("NFKC", name) in norm and fee:
                g["fee"] = fee
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-all", action="store_true")
    args = ap.parse_args()

    with open(BASE_DIR / "config_fureai.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("required_slots", ["午後", "夜間"])
    cfg.setdefault("facility_filter", [])
    cfg.setdefault("notify_filled", True)

    # 共通エンジンのグローバルをふれあいネット用に設定
    core.SLOT_WINDOWS = {k: tuple(v) for k, v in cfg["slot_windows"].items()}
    core.TIME_SLOT_WORDS = tuple(sorted(core.SLOT_WINDOWS.keys(), key=len, reverse=True))
    core.NOTIFY_LABEL = cfg.get("notify_label", "午後＋夜間")
    core.STATE_PATH = BASE_DIR / cfg.get("state_file", "state_fureai.json")
    core.BASE_URL = SP_URL

    raw, ok, errors = fetch_availability(cfg, debug=args.debug or args.test)

    if args.test:
        matched = core.find_matched(core.build_groups(raw), cfg)
        apply_fees(matched, cfg)
        core.send_test_notification(cfg, raw, ok, dry_run=args.dry_run,
                                    matched=matched, errors=errors)
        sys.exit(0)

    if not ok and not raw:
        print("[ERROR] 空き状況を取得できませんでした")
        sys.exit(1)

    groups = core.build_groups(raw)
    matched = core.find_matched(groups, cfg)
    apply_fees(matched, cfg)
    n_target = sum(1 for g in groups.values() if g["is_target"])
    print(f"[INFO] 土日祝グループ {n_target} 件中、条件成立 {len(matched)} 件")

    state = core.load_state()
    prev_matched = set(state.get("matched", []))
    ever = set(state.get("ever_matched", []))
    first_run = "slots" not in state

    if args.notify_all:
        targets = sorted(matched.keys())
    elif first_run:
        print("[INFO] 初回実行。状態を保存しました")
        targets = []
    else:
        targets = sorted(set(matched.keys()) - prev_matched)

    if targets:
        targets = sorted(targets, key=lambda k: (matched[k]["date"] or date.max, k))
        items = [(k, matched[k], k in ever) for k in targets]
        core.notify_discord(cfg.get("discord_webhook_url", ""), items, dry_run=args.dry_run)
    else:
        print("[INFO] 新たな条件成立はありません")

    if not first_run and cfg.get("notify_filled", True) and not args.notify_all:
        lost = []
        for k in sorted(prev_matched - set(matched.keys())):
            d = core.parse_date_from_text(k.split("|")[-1])
            if d and d >= date.today():
                lost.append(k)
        if lost and ok:  # 取得失敗時の誤検知を防ぐ
            core.notify_lost(cfg.get("discord_webhook_url", ""), sorted(lost), dry_run=args.dry_run)

    if ok:
        ever |= set(matched.keys())
        core.save_state(raw, list(matched.keys()), ever)
    else:
        print("[WARN] 一部取得に失敗したため状態は更新しません")


if __name__ == "__main__":
    main()
