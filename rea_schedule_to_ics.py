#!/usr/bin/env python3
"""rasp.rea.ru -> schedule.ics

Забирает расписание группы с rasp.rea.ru (/Schedule/ScheduleCard), добавляет
преподавателей (/Schedule/GetDetails) и собирает календарь iCalendar, на который
можно подписаться с iPhone.

Преподаватели:
  * для каждой пары «предмет + тип занятия» ищутся один раз и кладутся в
    teachers_cache.json (обновляется раз в 30 дней);
  * занятия ближайших 14 дней перепроверяются при каждом запуске (замены).

Зависимости:  pip install beautifulsoup4

Примеры:
  python rea_schedule_to_ics.py                        # недели 1..45
  python rea_schedule_to_ics.py --from-week 6 --to-week 18 --alarm 30
  python rea_schedule_to_ics.py --no-teachers          # без преподавателей
  python rea_schedule_to_ics.py --from-file week.html  # проверка на сохранённом HTML
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from bs4 import BeautifulSoup, Tag

SITE = "https://rasp.rea.ru"
CARD_URL = f"{SITE}/Schedule/ScheduleCard"
DETAILS_URL = f"{SITE}/Schedule/GetDetails"
DEFAULT_GROUP = "15.27д-би06/25м"
MSK = timezone(timedelta(hours=3))  # Москва: UTC+3 круглый год
PAUSE = 1.0  # секунд между запросами к сайту


def today():
    return datetime.now(MSK).date()


# ---------- загрузка ----------

def http_get(url, params):
    req = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={
            # заголовки HTTP — только латиница
            "User-Agent": "Mozilla/5.0 (personal schedule sync)",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{SITE}/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_week(group, week):
    return http_get(CARD_URL, {"selection": group, "weekNum": week, "catfilter": 0})


def fetch_details(group, day, pair):
    return http_get(DETAILS_URL, {"selection": group, "date": f"{day:%d.%m.%Y}", "timeSlot": pair})


# ---------- разбор ----------

def pretty_place(place):
    """'6 корпус - 336, пл. Основная' -> '6 корпус, ауд. 336 (пл. Основная)'."""
    m = re.match(r"(.+?корпус)\s*-\s*(\S+?)(?:,|\s|$)\s*(.*)$", place)
    if not m:
        return place
    corpus, room, site = m.groups()
    return f"{corpus}, ауд. {room}" + (f" ({site})" if site else "")


def parse_week(html):
    """Список занятий недели: dict(date, pair, start, end, title, kind, place, eid)."""
    soup = BeautifulSoup(html, "html.parser")
    lessons = []
    for table in soup.select("table"):
        head = table.select_one("th.dayh")
        if not head:
            continue
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", head.get_text())
        if not m:
            continue
        day = date(int(m[3]), int(m[2]), int(m[1]))

        for row in table.select("tr.slot"):
            link = row.select_one("a.task")
            if not link:  # пустая пара или «Занятия отсутствуют»
                continue
            first_cell = row.select_one("td").get_text(" ")
            pair = re.search(r"(\d+)\s*пара", first_cell)
            times = re.findall(r"\b(\d{1,2}):(\d{2})\b", first_cell)
            if not pair or len(times) < 2:
                continue

            strings = list(link.stripped_strings)
            kind_tag = link.find("i")
            kind = kind_tag.get_text(" ", strip=True) if kind_tag else ""
            rest = [s for s in strings[1:] if s != kind]
            place = " ".join(" ".join(rest).split()).replace(" ,", ",")

            lessons.append({
                "date": day,
                "pair": int(pair[1]),
                "start": dtime(int(times[0][0]), int(times[0][1])),
                "end": dtime(int(times[1][0]), int(times[1][1])),
                "title": strings[0],
                "kind": kind,
                "place": pretty_place(place),
                "eid": link.get("data-elementid", ""),
            })
    return lessons


def parse_details(html):
    """Преподаватели из окна с подробностями: [[ФИО, кафедра], ...]."""
    soup = BeautifulSoup(html, "html.parser")
    teachers = []
    for a in soup.select('a[href^="?q="]'):
        for icon in a.select("i"):  # иконка material-icons: текст «school»
            icon.decompose()
        name = " ".join(a.get_text(" ", strip=True).split())
        if not name:
            continue
        dept = ""
        for sib in a.next_siblings:  # «(Кафедра ...)» стоит сразу после ссылки
            if isinstance(sib, Tag) and sib.name == "a":
                break
            text = sib.get_text(" ") if isinstance(sib, Tag) else str(sib)
            m = re.search(r"\(([^()]+)\)", text)
            if m:
                dept = " ".join(m.group(1).split())
                break
        teachers.append([name, dept])
    return teachers


def merge_adjacent(lessons):
    """Склеивает подряд идущие пары одного предмета (7+8 -> один слот 18:55-22:00)."""
    out = []
    for l in sorted(lessons, key=lambda x: (x["date"], x["pair"])):
        p = out[-1] if out else None
        if (p and p["date"] == l["date"] and p["last_pair"] + 1 == l["pair"]
                and (p["title"], p["kind"], p["place"]) == (l["title"], l["kind"], l["place"])):
            p["end"] = l["end"]
            p["last_pair"] = l["pair"]
        else:
            out.append({**l, "last_pair": l["pair"]})
    return out


# ---------- преподаватели ----------

def cache_key(l):
    return f"{l['title']} | {l['kind']}"


def attach_teachers(lessons, group, cache, refresh_days=14, cache_days=30):
    """Дописывает l['teachers'] каждому занятию. Возвращает число запросов к сайту."""
    now = today()
    near_end = now + timedelta(days=refresh_days)
    memo = {}

    def lookup(l):
        slot = (l["date"], l["pair"])
        if slot not in memo:
            try:
                memo[slot] = parse_details(fetch_details(group, l["date"], l["pair"]))
            except Exception as e:  # сайт ответил ошибкой — просто идём дальше
                print(f"преподаватель {l['date']:%d.%m} пара {l['pair']}: {e}", file=sys.stderr)
                memo[slot] = []
            time.sleep(PAUSE)
        return memo[slot]

    by_key = {}
    for l in lessons:
        by_key.setdefault(cache_key(l), []).append(l)

    # 1) пополняем кэш: один запрос на каждую пару «предмет + тип»
    for k, items in by_key.items():
        entry = cache.get(k)
        if entry and (now - date.fromisoformat(entry["updated"])).days <= cache_days:
            continue
        upcoming = [l for l in items if l["date"] >= now]
        teachers = lookup((upcoming or items)[0])
        if teachers:
            cache[k] = {"teachers": teachers, "updated": now.isoformat()}

    # 2) ближайшие дни смотрим по каждому занятию (замены), остальные — из кэша
    for l in lessons:
        own = lookup(l) if now <= l["date"] <= near_end else []
        l["teachers"] = own or cache.get(cache_key(l), {}).get("teachers", [])
    return len(memo)


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------- iCalendar ----------

def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """Перенос строк по RFC 5545: не больше 75 октетов, не режем многобайтные символы."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def utc(d, t):
    return datetime.combine(d, t, tzinfo=MSK).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(lessons, alarm_min=0):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//rasp.rea.ru personal sync//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Учёба РЭУ",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for l in lessons:
        first, last = l["pair"], l.get("last_pair", l["pair"])
        desc = [f"Пара {first}" if first == last else f"Пары {first}–{last}"]
        teachers = l.get("teachers") or []
        if teachers:
            names = "; ".join(f"{n} ({d})" if d else n for n, d in teachers)
            desc.append(("Преподаватели: " if len(teachers) > 1 else "Преподаватель: ") + names)
        description = "\n".join(desc)

        summary = f"{l['title']} ({l['kind']})" if l["kind"] else l["title"]
        uid = f"{l['eid'] or l['pair']}-{l['date']:%Y%m%d}@rasp.rea.ru"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{utc(l['date'], l['start'])}",
            f"DTEND:{utc(l['date'], l['end'])}",
            f"SUMMARY:{esc(summary)}",
            f"LOCATION:{esc(l['place'])}",
            f"DESCRIPTION:{esc(description)}",
        ]
        if alarm_min:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{esc(l['title'])}",
                f"TRIGGER:-PT{alarm_min}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in lines) + "\r\n"


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="rasp.rea.ru -> .ics")
    ap.add_argument("--group", default=DEFAULT_GROUP)
    ap.add_argument("--from-week", type=int, default=1)
    ap.add_argument("--to-week", type=int, default=45)
    ap.add_argument("--out", default="schedule.ics")
    ap.add_argument("--no-merge", action="store_true", help="не склеивать подряд идущие пары")
    ap.add_argument("--alarm", type=int, default=0, help="напоминание за N минут (0 = без)")
    ap.add_argument("--no-teachers", action="store_true", help="не искать преподавателей")
    ap.add_argument("--cache", default="teachers_cache.json", help="файл кэша преподавателей")
    ap.add_argument("--refresh-days", type=int, default=14,
                    help="сколько ближайших дней перепроверять преподавателей каждый запуск")
    ap.add_argument("--from-file", help="разобрать сохранённый HTML вместо похода на сайт")
    args = ap.parse_args()

    found = []
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            found = parse_week(f.read())
    else:
        for week in range(args.from_week, args.to_week + 1):
            try:
                found += parse_week(fetch_week(args.group, week))
            except Exception as e:  # сайт лёг / сменилась вёрстка — идём дальше
                print(f"неделя {week}: {e}", file=sys.stderr)
            time.sleep(PAUSE)

    unique = sorted({(l["date"], l["pair"]): l for l in found}.values(),
                    key=lambda x: (x["date"], x["pair"]))
    lessons = unique if args.no_merge else merge_adjacent(unique)
    if not lessons:
        sys.exit("Не найдено ни одного занятия — .ics не перезаписан.")

    if not args.no_teachers and not args.from_file:
        cache = load_cache(args.cache)
        requests_made = attach_teachers(lessons, args.group, cache, args.refresh_days)
        save_cache(args.cache, cache)
        missing = sum(1 for l in lessons if not l.get("teachers"))
        print(f"Преподаватели: {requests_made} запросов, без преподавателя: {missing} из {len(lessons)}")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(lessons, args.alarm))
    print(f"Готово: {len(lessons)} событий -> {args.out}")


if __name__ == "__main__":
    main()
