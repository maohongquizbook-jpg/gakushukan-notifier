# 新宿区 生涯学習館「土日祝 午後＋夜間 連続空き」通知ツール（Discord Webhook）

レガス新宿 施設予約システム（https://www.shinjuku.eprs.jp/regasu/web/）を定期チェックし、
**土日祝で、同じ部屋の「午後」と「夜間」が両方空いているコマ**が出た瞬間にDiscordへ通知します。

- 祝日は `jpholiday` ライブラリでカレンダー判定（振替休日も対応）
- 一度予約で埋まった後にキャンセル等で**再度空いた場合も通知**（♻️再度空き と表示）
- 通知には `@here` メンションが付きます（不要なら monitor.py の `"content": "@here"` を削除）

## セットアップ

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
```

1. Discordの通知先チャンネルで「チャンネル編集 → 連携サービス → ウェブフック」からURLを発行
2. `config.yaml` の `discord_webhook_url` に貼り付け
3. 動作確認:

```bash
python monitor.py --debug --dry-run      # 取得できるか確認（debug/ に画面キャプチャ保存）
python monitor.py --notify-all --dry-run # 現在条件を満たすコマをコンソールに表示
python monitor.py --notify-all           # 上記をDiscordに実際に送ってみる
```

## 「できるだけ早く」検知するための運用（推奨: 常駐モード）

常時稼働できるマシン（自宅PC・Raspberry Pi・VPS等）で:

```bash
python monitor.py --loop
```

`config.yaml` の `check_interval_min: 5` の間隔（デフォルト5分）でチェックし続けます。
`active_hours: [7, 23]` で深夜帯は自動的に休止します。

systemd で常駐させる例（`/etc/systemd/system/gakushukan.service`）:

```ini
[Unit]
Description=Gakushukan availability monitor
After=network-online.target

[Service]
WorkingDirectory=/path/to/gakushukan-notifier
ExecStart=/usr/bin/python3 monitor.py --loop
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gakushukan
```

### 常時稼働マシンがない場合: GitHub Actions

`.github/workflows/monitor.yml` を同梱しています（プライベートリポジトリにpushし、
Secretsに `DISCORD_WEBHOOK_URL` を登録）。ただし **GitHub Actions のスケジュール実行は
数分〜数十分遅延することが多く**、「判明したら即通知」には向きません。速報性重視なら常駐モードを推奨します。

## 通知条件のカスタマイズ

`config.yaml` の `required_slots` を変えれば条件を変更できます。
例: 午前も含めた終日空きだけ通知 → `[午前, 午後, 夜間]`

## 画面構成が変わった/取得できないとき

このシステムはJavaScript描画のSPAのため、画面のボタン名や表構成が変わると解析に失敗することがあります。

1. `python monitor.py --debug` を実行
2. `debug/` の **スクリーンショット・HTML・XHRログ（JSON API通信の記録）** を確認
3. `monitor.py` の `try_click_text(...)` の文言や `SYMBOL_MAP` を実際の画面に合わせて調整

XHRログにJSON APIが記録されていれば、ブラウザを使わず直接APIを叩く方式に書き換えると
より高速・安定になります（debugログを共有してもらえれば書き換えできます）。

## 注意事項

- チェック間隔は**5分以上**にしてください。過度に短い間隔での自動アクセスはサーバー負荷となり、
  アクセス制限の対象になり得ます（コード側でも3分未満にはならないよう制限しています）。
- 本ツールは空き状況の「閲覧」のみで、予約は行いません。通知を受けたらシステム上で手動で予約してください
  （利用団体登録が必要です）。キャンセルは窓口・電話のみの運用のため、直前の再空きも発生し得ます。
- 列ヘッダ等から日付が読み取れないコマは、表記に (土)(日)(祝) が含まれる場合のみ対象になります。
