"""予約の公開と受付開始を分ける。すべて日本時間、通信・状態更新は行わない。"""
import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo('Asia/Tokyo')
REVISION = '2026-11-16'


def month_start(day, offset):
    index = day.year * 12 + day.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def same_day_months_before(day, months):
    first = month_start(day, -months)
    last = calendar.monthrange(first.year, first.month)[1]
    # 同日が存在しない場合は翌月1日。月末への切り捨てで早期通知しない。
    return first.replace(day=day.day) if day.day <= last else month_start(first, 1)


def nth_weekday(first, weekday, number):
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (number - 1))


def entries(cfg):
    return cfg.get('room_catalog') or [dict(room, facility=target['kan'])
        for target in cfg.get('targets', []) for room in target['rooms']]


def candidate(entry, cfg):
    return (entry.get('monitor', True) and not entry.get('excluded_reason')
            and entry.get('user_confirmed_use') not in {'not_allowed', 'joint_only'}
            and entry.get('capacity', 0) >= int(cfg.get('min_capacity', 0))
            and entry.get('fee', float('inf')) <= cfg.get('max_total_fee', float('inf')))


def policy_for(entry, cfg):
    rule = dict(cfg.get('booking_policy') or {})
    facility = entry.get('facility', '') if entry else ''
    room = entry.get('name', '') if entry else ''
    rule.update(rule.get('facility_overrides', {}).get(facility, {}))
    rule.update(rule.get('room_overrides', {}).get(f'{facility}・{room}', {}))
    return rule


def opening_at(day, cfg, entry=None):
    rule = policy_for(entry, cfg)
    kind = rule.get('kind')
    if not kind:
        return None  # 旧設定とのAPI互換。運用設定は全3系統で明示する。
    if kind == 'gakushukan_general':
        opened = same_day_months_before(day, 2)
        hour, minute = 0, 0
    elif kind == 'chiiki_general':
        opened = month_start(day, -1).replace(day=10)
        facility = (entry or {}).get('facility', '')
        second = {'牛込箪笥地域センター', '戸塚地域センター', '柏木地域センター'}
        third = {'四谷地域センター', '若松地域センター', '大久保地域センター', '落合第一地域センター'}
        fourth = {'榎町地域センター', '落合第二地域センター', '角筈地域センター'}
        n = 2 if facility in second else 3 if facility in third else 4 if facility in fourth else None
        if not n:
            raise ValueError(f'地域センターの休館日が未登録です: {facility}')
        if opened.month in (2, 5, 8, 11) and opened == nth_weekday(opened.replace(day=1), 6, n):
            opened += timedelta(days=1)
        opened += timedelta(days=1)  # 窓口開始日の翌日9時
        hour, minute = 9, 0
    elif kind == 'chiiki_registered':
        first = month_start(day, -2)
        opened = nth_weekday(first, 5, 2 if first.month == 1 else 1) + timedelta(days=1)
        hour, minute = 9, 0
    elif kind == 'fureai_indoor':
        first = month_start(day, -4)
        opened = first.replace(day=29) if calendar.monthrange(first.year, first.month)[1] >= 29 else month_start(first, 1)
        # 12/29～1/3は予約サービス停止。12月分の随時受付は再開後に扱う。
        if opened.month == 12:
            opened = date(opened.year + 1, 1, 4)
        hour, minute = 7, 0
    elif kind == 'epoch_washitsu':
        opened = same_day_months_before(day, 6)
        hour, minute = 8, 30
    else:
        raise ValueError(f'未知の予約受付ルールです: {kind}')
    key = day.isoformat()
    if key in rule.get('opening_overrides', {}):
        dt = datetime.fromisoformat(str(rule['opening_overrides'][key]))
        return dt.replace(tzinfo=JST) if dt.tzinfo is None else dt.astimezone(JST)
    return datetime.combine(opened, time(hour, minute), JST)


def status(day, cfg, entry, at):
    """期間・定員の判定。日々のシステム休止時間は履歴を消さず実行側で扱う。"""
    if entry.get('capacity', 0) < int(cfg.get('min_capacity', 0)):
        return False, '定員不足'
    opens = opening_at(day, cfg, entry)
    if opens and at < opens:
        return False, '予約開始前'
    rule = policy_for(entry, cfg)
    limit = int(rule.get('deadline_days', 0))
    if at.date() > day - timedelta(days=limit):
        return False, '受付期間終了'
    if at.date() == day:
        windows = entry.get('slot_times') or cfg.get('slot_windows') or {'午後': (1300, 1700), '夜間': (1800, 2200)}
        # slot_times は表示文字列の場合があるので、解析にはslot_windowsを使う。
        if not isinstance(windows, dict) or any(not isinstance(v, (list, tuple)) for v in windows.values()):
            windows = cfg.get('slot_windows') or {'午後': (1300, 1700), '夜間': (1800, 2200)}
        starts = [int(windows[s][0]) for s in cfg['required_slots'] if s in windows]
        if starts and at.hour * 100 + at.minute >= min(starts):
            return False, '利用開始時刻経過'
    return True, '受付期間内'


def service_open(cfg, at):
    rule = cfg.get('booking_policy') or {}
    if rule.get('kind') == 'fureai_indoor':
        if at.hour < 7 or (at.month, at.day) >= (12, 29) or (at.month, at.day) <= (1, 3):
            return False
    if rule.get('kind') == 'gakushukan_general' and rule.get('maintenance_26', False):
        if at.day == 26 and at.hour < 5:
            return False
        if at.hour == 3 and at.minute < 15:
            return False
    return True


def effective_opening_at(day, cfg, entry=None):
    opens = opening_at(day, cfg, entry)
    if (opens and (cfg.get('booking_policy') or {}).get('maintenance_26', False)
            and opens.day == 26 and opens.hour < 5):
        opens = opens.replace(hour=5)
    return opens


def opening_dates(cfg, at, end, is_target):
    """前後30分の受付開始だけを返す。手動起動して翌日の0時まで待たない。"""
    groups = {}
    rooms = [e for e in entries(cfg) if candidate(e, cfg)]
    day = at.date()
    while day <= end:
        if is_target(day):
            for entry in rooms:
                opens = effective_opening_at(day, cfg, entry)
                if opens and abs((opens - at).total_seconds()) <= 30 * 60:
                    groups.setdefault(opens, set()).add(day)
        day += timedelta(days=1)
    if not groups:
        return None, []
    event = min(groups, key=lambda dt: abs((dt - at).total_seconds()))
    return event, sorted(groups[event])


def split_range(start, end):
    result = []
    while start <= end:
        remaining = (end - start).days + 1
        size = next(n for n in (31, 7, 3, 2, 1) if n <= remaining)
        result.append((start.isoformat(), str(size)))
        start += timedelta(days=size)
    return result


def search_plan(cfg, start, end, at):
    """受付開始前の末尾を検索しない。当日解禁分を先に、重複なしで検索。"""
    if cfg.get('_scan_windows') is not None:
        return list(cfg['_scan_windows'])
    if not cfg.get('booking_policy'):
        return [((start + timedelta(days=n)).isoformat(), '31') for n in range(0, (end-start).days+1, 31)]
    rooms = [e for e in entries(cfg) if candidate(e, cfg)]
    days, priority = [], []
    for n in range((end - start).days + 1):
        day = start + timedelta(days=n)
        eligible = [e for e in rooms if status(day, cfg, e, at)[0]]
        if not eligible:
            continue
        days.append(day)
        if any((effective_opening_at(day, cfg, e) or at).date() == at.date() for e in eligible):
            priority.append(day)
    if not days:
        return []
    # 当日解禁の連続区間を先頭へ。残りも連続区間にまとめてHTTP要求を抑える。
    def windows(selected):
        result = []
        begin = last = None
        for day in sorted(selected):
            if last is not None and day != last + timedelta(days=1):
                result.extend(split_range(begin, last)); begin = None
            if begin is None:
                begin = day
            last = day
        if begin is not None:
            result.extend(split_range(begin, last))
        return result
    return windows(priority) + windows(set(days) - set(priority))
