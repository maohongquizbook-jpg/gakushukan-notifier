#!/usr/bin/env python3
"""
川崎市 ふれあいネット 調査スクリプト【フェーズ1・v2】

かんたん画面(/web/)の空き照会フローを自動で辿り、各画面を debug/ に保存する。
経路: かんたん画面 → 予約 → 地域から → 川崎区/幸区/中原区 → 館選択 →
      (先頭の館) → 施設選択 → (先頭の施設) → 一ヶ月空き表示 → 週表示

使い方: python fureai.py --debug
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

KANTAN_URL = "https://www.fureai-net.city.kawasaki.jp/web/"
DEBUG_DIR = Path(__file__).parent / "debug"
WARDS = ["川崎区", "幸区", "中原区"]


def dump(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_fureai_{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{ts}_fureai_{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"[DEBUG] {tag} を保存")
    except Exception as e:
        print(f"[WARN] dump失敗 {tag}: {e}")


def click_any(page, texts, timeout=4000):
    """テキスト・alt属性・value属性のいずれかに一致する要素をクリック"""
    for t in texts:
        candidates = [
            page.get_by_role("link", name=t),
            page.get_by_role("button", name=t),
            page.locator(f'img[alt*="{t}"]'),
            page.locator(f'input[value*="{t}"]'),
            page.get_by_text(t, exact=True),
            page.get_by_text(t),
        ]
        for loc in candidates:
            try:
                loc.first.click(timeout=timeout)
                print(f"[INFO] 「{t}」をクリック")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeout:
                    pass
                time.sleep(2)
                return True
            except Exception:
                continue
    print(f"[WARN] クリック候補が見つからず: {texts}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            locale="ja-JP", viewport={"width": 1300, "height": 1400}).new_page()

        print(f"[INFO] かんたん画面 {KANTAN_URL} を開いています…")
        page.goto(KANTAN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        dump(page, "00_kantan_home")

        # メニュー「予約」
        click_any(page, ["予約"])
        dump(page, "01_yoyaku_menu")

        # 「地域から」
        click_any(page, ["地域から", "地域から探す", "地域"])
        dump(page, "02_chiiki_select")

        # 各区の館一覧を採取（区→館一覧→戻るを繰り返す）
        for i, ward in enumerate(WARDS):
            if click_any(page, [ward]):
                dump(page, f"03_ward{i}_{ward}_kan_list")
                if ward == "川崎区":
                    # 川崎区だけさらに深掘り: 先頭の館→施設一覧→先頭施設→月表示→週表示
                    clicked = page.evaluate(
                        """() => {
                            const links = Array.from(document.querySelectorAll('a'))
                                .filter(a => a.offsetParent !== null && a.innerText.trim().length > 2
                                        && !/戻る|ホーム|メニュー|ログイン/.test(a.innerText));
                            if (links.length) { links[0].click(); return links[0].innerText.trim(); }
                            return null;
                        }"""
                    )
                    print(f"[INFO] 館を選択: {clicked}")
                    time.sleep(3)
                    dump(page, "04_shisetsu_list")
                    clicked2 = page.evaluate(
                        """() => {
                            const links = Array.from(document.querySelectorAll('a'))
                                .filter(a => a.offsetParent !== null && a.innerText.trim().length > 2
                                        && !/戻る|ホーム|メニュー|ログイン/.test(a.innerText));
                            if (links.length) { links[0].click(); return links[0].innerText.trim(); }
                            return null;
                        }"""
                    )
                    print(f"[INFO] 施設を選択: {clicked2}")
                    time.sleep(3)
                    dump(page, "05_month_view")
                    # 月表示の「丸/三角」アイコンをクリックして週(日別)表示へ
                    page.evaluate(
                        """() => {
                            const imgs = Array.from(document.querySelectorAll(
                                'a img[alt*="空"], a img[alt*="全て"], a img[alt*="一部"], td a'))
                                .filter(e => e.offsetParent !== null);
                            if (imgs.length) (imgs[0].closest('a') || imgs[0]).click();
                        }"""
                    )
                    time.sleep(3)
                    dump(page, "06_week_view")
                # 地域選択へ戻る
                page.goto(KANTAN_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(2)
                click_any(page, ["予約"])
                click_any(page, ["地域から", "地域から探す", "地域"])

        print("[INFO] 調査完了。Artifactsから debug/ を回収してください。")
        browser.close()


if __name__ == "__main__":
    main()
