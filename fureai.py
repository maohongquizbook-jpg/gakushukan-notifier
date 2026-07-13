#!/usr/bin/env python3
"""
川崎市 ふれあいネット 調査スクリプト【フェーズ1・v3】

採取内容:
 A) かんたん画面: 予約→地域から→各区→代表館→施設(部屋)一覧→月表示→週表示
    ・教育文化会館(川崎区)・幸市民館(幸区)・中原市民館(中原区)の部屋一覧
 B) スマートフォン画面(/sp/): 施設空き状況 の画面遷移

使い方: python fureai.py --debug
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

KANTAN_URL = "https://www.fureai-net.city.kawasaki.jp/web/"
SP_URL = "https://www.fureai-net.city.kawasaki.jp/sp/"
DEBUG_DIR = Path(__file__).parent / "debug"

# (区, 館) の採取対象
TARGETS = [("川崎区", "教育文化会館"), ("幸区", "幸市民館"), ("中原区", "中原市民館")]

NAV_WORDS = ("サイトマップ", "ヘルプ", "ホーム", "ログイン", "戻る", "メニュー",
             "本文", "すべて", "利用者登録", "各種申請書", "施設案内", "予約",
             "抽選", "小", "中", "大", "緑", "青", "赤")


def dump(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_fureai_{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{ts}_fureai_{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"[DEBUG] {tag} を保存")
    except Exception as e:
        print(f"[WARN] dump失敗 {tag}: {e}")


def wait(page, sec=2.5):
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    time.sleep(sec)


def click_link_by_text(page, text) -> bool:
    """innerTextが一致する<a>をJSで直接クリック（画像リンク・非表示でも動く）"""
    ok = page.evaluate(
        """(t) => {
            const links = Array.from(document.querySelectorAll('a'));
            const hit = links.find(a => a.innerText.trim() === t)
                     || links.find(a => a.innerText.includes(t))
                     || links.find(a => (a.querySelector('img')?.alt || '').includes(t));
            if (hit) { hit.click(); return true; }
            return false;
        }""", text)
    print(f"[INFO] リンク「{text}」: {'クリック' if ok else '見つからず'}")
    return ok


def click_first_content_link(page) -> str:
    """ナビ以外の本文リンク（doAction/sendBld系）の先頭をクリック"""
    name = page.evaluate(
        """(navWords) => {
            const links = Array.from(document.querySelectorAll('a')).filter(a => {
                const t = a.innerText.trim();
                const h = a.getAttribute('href') || '';
                if (!t || t.length < 2) return false;
                if (navWords.some(w => t === w || t.includes('スキップ'))) return false;
                return h.includes('doAction') || h.includes('sendBld') || h.includes('send');
            });
            if (links.length) { const t = links[0].innerText.trim(); links[0].click(); return t; }
            return null;
        }""", list(NAV_WORDS))
    print(f"[INFO] 本文リンク先頭をクリック: {name}")
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            locale="ja-JP", viewport={"width": 1300, "height": 1600}).new_page()

        # ---- A) かんたん画面 ----
        for i, (ward, kan) in enumerate(TARGETS):
            print(f"[INFO] === {ward} / {kan} ===")
            page.goto(KANTAN_URL, wait_until="domcontentloaded", timeout=60000)
            wait(page)
            click_link_by_text(page, "予約")
            wait(page)
            click_link_by_text(page, "地域から")
            wait(page)
            if not click_link_by_text(page, ward):
                continue
            wait(page)
            if not click_link_by_text(page, kan):
                dump(page, f"{i}0_{ward}_kanlist_fail")
                continue
            wait(page)
            dump(page, f"{i}1_{kan}_room_list")   # 部屋(施設)一覧
            room = click_first_content_link(page)
            wait(page)
            dump(page, f"{i}2_{kan}_month_view")  # 一ヶ月表示
            # カレンダーの空きアイコン（全て空き/一部空き）をクリック → 週表示
            page.evaluate(
                """() => {
                    const cand = Array.from(document.querySelectorAll('a')).find(a => {
                        const alt = a.querySelector('img')?.alt || '';
                        return alt.includes('空');
                    });
                    if (cand) cand.click();
                }"""
            )
            wait(page)
            dump(page, f"{i}3_{kan}_week_view")   # 週(日別)表示

        # ---- B) スマートフォン画面 ----
        print("[INFO] === スマートフォン画面 ===")
        page.goto(SP_URL, wait_until="domcontentloaded", timeout=60000)
        wait(page)
        dump(page, "90_sp_home")
        if click_link_by_text(page, "施設空き状況"):
            wait(page)
            dump(page, "91_sp_vacancy_step1")
            name = click_first_content_link(page)
            wait(page)
            dump(page, "92_sp_vacancy_step2")

        print("[INFO] 調査完了。Artifactsから debug/ を回収してください。")
        browser.close()


if __name__ == "__main__":
    main()
