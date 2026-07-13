#!/usr/bin/env python3
"""
川崎市 ふれあいネット 調査スクリプト【フェーズ1・v4: スマートフォン画面版】

/sp/ の空き照会フローを辿って各画面を採取する。
経路: 認証前メニュー → 施設空き状況 → 地域から → 区 → 館 → 施設(部屋) → 空きカレンダー
対象: 教育文化会館(川崎区)・幸市民館(幸区)・中原市民館(中原区)

使い方: python fureai.py --debug
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SP_URL = "https://www.fureai-net.city.kawasaki.jp/sp/"
DEBUG_DIR = Path(__file__).parent / "debug"
TARGETS = [("川崎区", "教育文化会館"), ("幸区", "幸市民館"), ("中原区", "中原市民館")]
NAV_WORDS = ("TOP画面へ", "戻る", "メニュー", "認証", "お知らせ", "こちら",
             "施設案内", "イベント", "抽選", "利用者登録", "tel:")


def dump(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_sp_{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{ts}_sp_{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"[DEBUG] {tag} を保存")
    except Exception as e:
        print(f"[WARN] dump失敗 {tag}: {e}")


def wait(page, sec=1.5):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    time.sleep(sec)


def click_text(page, text) -> bool:
    ok = page.evaluate(
        """(t) => {
            const links = Array.from(document.querySelectorAll('a'));
            const hit = links.find(a => a.innerText.trim() === t)
                     || links.find(a => a.innerText.includes(t));
            if (hit) { hit.click(); return true; }
            return false;
        }""", text)
    print(f"[INFO] 「{text}」: {'クリック' if ok else '見つからず'}")
    return ok


def list_links(page):
    return page.evaluate(
        """(nav) => Array.from(document.querySelectorAll('a'))
            .map(a => ({t: a.innerText.trim(), h: a.getAttribute('href') || ''}))
            .filter(x => x.t && x.h.includes('.do')
                    && !nav.some(w => x.t.includes(w) || x.h.startsWith('tel')))""",
        list(NAV_WORDS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            locale="ja-JP", viewport={"width": 480, "height": 1200}).new_page()

        for i, (ward, kan) in enumerate(TARGETS):
            print(f"[INFO] === {ward} / {kan} ===")
            page.goto(SP_URL, wait_until="domcontentloaded", timeout=60000)
            wait(page)
            click_text(page, "施設空き状況")
            wait(page)
            if i == 0:
                dump(page, "10_vacancy_top")
            click_text(page, "地域から")
            wait(page)
            if i == 0:
                dump(page, "11_area_select")
            if not click_text(page, ward):
                dump(page, f"{i}_ward_fail")
                continue
            wait(page)
            if i == 0:
                dump(page, f"12_{ward}_kan_list")
            if not click_text(page, kan):
                dump(page, f"{i}_kan_fail")
                continue
            wait(page)
            dump(page, f"2{i}_{kan}_room_list")
            print(f"[INFO] {kan} のリンク一覧: {list_links(page)[:15]}")
            # 先頭の部屋（.doリンク）をクリック
            rooms = list_links(page)
            if rooms:
                click_text(page, rooms[0]["t"])
                wait(page)
                dump(page, f"3{i}_{kan}_vacancy_cal")
                # 次表示（翌週/翌月など）があればもう1画面
                nxt = [l for l in list_links(page) if any(w in l["t"] for w in ("翌", "次"))]
                if nxt:
                    click_text(page, nxt[0]["t"])
                    wait(page)
                    dump(page, f"4{i}_{kan}_vacancy_next")

        print("[INFO] 調査完了。Artifactsから debug/ を回収してください。")
        browser.close()


if __name__ == "__main__":
    main()
