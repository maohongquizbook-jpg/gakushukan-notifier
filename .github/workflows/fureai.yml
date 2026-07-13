#!/usr/bin/env python3
"""
川崎市 ふれあいネット（公共施設利用予約システム）調査用スクリプト【フェーズ1】

ふれあいネットは新宿のeprsとは別エンジンのため、まず画面構造を採取する。
--debug 実行で debug/ にトップページと空き照会画面のHTML・スクリーンショットを保存し、
GitHub ActionsのArtifactsから回収して構造解析 → 本実装（フェーズ2）に進む。

使い方: python fureai.py --debug
"""
import argparse
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://www.fureai-net.city.kawasaki.jp/"
DEBUG_DIR = Path(__file__).parent / "debug"


def dump(page, tag):
    DEBUG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_fureai_{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{ts}_fureai_{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f"[DEBUG] {tag} を保存しました")
    except Exception as e:
        print(f"[WARN] dump失敗 {tag}: {e}")


def try_click(page, texts):
    for t in texts:
        for loc in (page.get_by_role("link", name=t), page.get_by_role("button", name=t),
                    page.get_by_text(t)):
            try:
                loc.first.click(timeout=3000)
                print(f"[INFO] 「{t}」をクリック")
                return True
            except Exception:
                continue
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            locale="ja-JP", viewport={"width": 1400, "height": 1200}).new_page()
        print(f"[INFO] {BASE_URL} を開いています…")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        dump(page, "home")

        # 空き照会（ログイン不要の照会機能）へ進んでみる
        if try_click(page, ["空き照会", "空き状況", "施設の空き状況", "空き状況照会",
                            "予約・抽選の申込み", "施設から探す"]):
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                pass
            time.sleep(3)
            dump(page, "step1")
            # さらに進める場合の候補
            if try_click(page, ["会議室", "集会施設", "市民館", "地域から探す", "利用目的から探す"]):
                time.sleep(3)
                dump(page, "step2")
        print("[INFO] 調査完了。debug/ の内容をArtifactsから回収してください。")
        browser.close()


if __name__ == "__main__":
    main()
