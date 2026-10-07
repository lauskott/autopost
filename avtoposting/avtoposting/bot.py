"""
Автопостинг для Telegram-каналов (LausStudio).

Как это работает:
  * Посты лежат в папке channels/<имя_канала>/.
  * Имя файла задаёт время публикации по Владивостоку:
        2026-10-09_23-00.txt        — обычный пост
        2026-10-08_23-00.quiz.txt   — викторина
    После времени можно дописать что угодно: 2026-10-09_23-00_equals.txt
  * Бот сам публикует пост, когда наступает его время,
    и запоминает отправленное в sent.json, чтобы не выложить дважды.

Команды:
  python bot.py            — запустить и держать включённым
  python bot.py --list     — показать расписание
  python bot.py --check    — проверить все посты на ошибки, ничего не отправляя
  python bot.py --now ФАЙЛ [--to @канал]
                           — отправить пост прямо сейчас (для проверки вида)
  python bot.py --once     — один проход и выход (для сервера/планировщика)
  python bot.py --once --ahead 20
                           — то же, но если пост выходит в ближайшие 20 минут,
                             дождаться его времени и выложить ровно в срок
                             (так бот работает на GitHub Actions)

Токен берётся из переменной окружения TELEGRAM_TOKEN, а если её нет —
из config.json. На сервере токен хранится в секретах, а не в файле.

Разметка в текстовых постах:
  **жирный**   `код в строке`   ||скрытый текст||
  Блок кода (моноширинный):        Скрытый блок (спойлер):
      ```java                          ```spoiler
      ...                              ...
      ```                              ```
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    print("Нужен Python 3.9 или новее.")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Не установлена библиотека requests. Выполни:  pip install -r requirements.txt")
    sys.exit(1)


BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
SENT_PATH = BASE / "sent.json"
CHANNELS_DIR = BASE / "channels"

NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})")
FENCE_RE = re.compile(r"^```\s*(\w*)\s*$")

CHECK_EVERY_SECONDS = 20
TEXT_LIMIT = 4096
QUESTION_LIMIT = 300
OPTION_LIMIT = 100
EXPLANATION_LIMIT = 200


class PostError(Exception):
    """Ошибка в файле поста — нужно исправить сам файл."""


class ApiError(Exception):
    """Telegram ответил ошибкой (нет прав, неверный канал и т.п.)."""


class NetworkError(Exception):
    """Не удалось достучаться до Telegram."""


@dataclass
class Post:
    path: Path
    channel_folder: str
    chat: str | None
    when: datetime
    kind: str  # "text" или "quiz"

    @property
    def key(self) -> str:
        return self.path.relative_to(BASE).as_posix()


# ---------------------------------------------------------------- лог

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%d.%m %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- конфиг и состояние

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print("Нет файла config.json рядом с bot.py.")
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"Ошибка в config.json (строка {e.lineno}): {e.msg}")
        sys.exit(1)

    env_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    file_token = str(cfg.get("token", "")).strip()
    if env_token and re.fullmatch(r"\d{5,}:[\w-]{30,}", file_token):
        print("В config.json лежит настоящий токен бота, а на GitHub его видят все. "
              "Сотри его из config.json, а в @BotFather сделай /revoke и положи новый токен "
              "в секрет TELEGRAM_TOKEN.")
        sys.exit(1)
    token = env_token or file_token
    if not token or "ВСТАВЬ" in token:
        print("Не найден токен бота. На ноутбуке вставь его в config.json в поле \"token\". "
              "На GitHub добавь секрет TELEGRAM_TOKEN (Settings → Secrets and variables → Actions).")
        sys.exit(1)
    cfg["token"] = token
    cfg.setdefault("timezone", "Asia/Vladivostok")
    cfg.setdefault("max_late_hours", 3)
    cfg.setdefault("channels", {})
    return cfg


def load_sent() -> dict:
    if not SENT_PATH.exists():
        return {}
    try:
        return json.loads(SENT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log("sent.json повреждён — начинаю с чистого листа (старый сохранён как sent.broken.json)")
        SENT_PATH.replace(BASE / "sent.broken.json")
        return {}


def save_sent(sent: dict) -> None:
    tmp = SENT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SENT_PATH)


# ---------------------------------------------------------------- поиск постов

def parse_post_path(path: Path, cfg: dict, tz: ZoneInfo) -> Post | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    try:
        when = datetime(y, mo, d, h, mi, tzinfo=tz)
    except ValueError:
        raise PostError(f"{path.name}: такой даты или времени не бывает")
    kind = "quiz" if path.name.lower().endswith(".quiz.txt") else "text"
    folder = path.parent.name
    chat = cfg["channels"].get(folder)
    return Post(path=path, channel_folder=folder, chat=chat, when=when, kind=kind)


def find_posts(cfg: dict, tz: ZoneInfo, problems: list[str] | None = None) -> list[Post]:
    posts = []
    if not CHANNELS_DIR.exists():
        return posts
    for folder in sorted(p for p in CHANNELS_DIR.iterdir() if p.is_dir()):
        for path in sorted(folder.glob("*.txt")):
            try:
                post = parse_post_path(path, cfg, tz)
            except PostError as e:
                if problems is not None:
                    problems.append(str(e))
                continue
            if post is None:
                if problems is not None:
                    problems.append(
                        f"{folder.name}/{path.name}: имя файла должно начинаться с даты и времени, "
                        f"например 2026-10-09_23-00.txt"
                    )
                continue
            posts.append(post)
    posts.sort(key=lambda p: (p.when, p.key))
    return posts


# ---------------------------------------------------------------- разметка текста

def inline_format(line: str) -> str:
    parts = line.split("`")
    if len(parts) % 2 == 0:  # непарный ` — оставляем строку как есть
        parts = [line]
    out = []
    for i, part in enumerate(parts):
        esc = html.escape(part, quote=False)
        if i % 2 == 1:
            out.append(f"<code>{esc}</code>")
        else:
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            esc = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", esc)
            out.append(esc)
    return "".join(out)


def text_to_html(text: str, name: str = "") -> str:
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i].strip())
        if m:
            lang = m.group(1).lower()
            start = i + 1
            block = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                block.append(lines[i])
                i += 1
            if i >= len(lines):
                raise PostError(f"{name}: блок ``` со строки {start} не закрыт")
            i += 1
            body = html.escape("\n".join(block), quote=False)
            if lang == "spoiler":
                out.append(f"<tg-spoiler>{body}</tg-spoiler>")
            elif lang:
                out.append(f'<pre><code class="language-{lang}">{body}</code></pre>')
            else:
                out.append(f"<pre>{body}</pre>")
        else:
            out.append(inline_format(lines[i]))
            i += 1
    result = "\n".join(out).strip()
    if not result:
        raise PostError(f"{name}: пост пустой")
    visible = html.unescape(re.sub(r"<[^>]+>", "", result))
    if len(visible) > TEXT_LIMIT:
        raise PostError(f"{name}: пост длиннее {TEXT_LIMIT} символов ({len(visible)})")
    return result


def parse_quiz(text: str, name: str = "") -> dict:
    question, options, explanation = [], [], []
    correct = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("+ ") or line.startswith("- "):
            if line.startswith("+ "):
                if correct is not None:
                    raise PostError(f"{name}: правильный вариант (+) должен быть только один")
                correct = len(options)
            options.append(line[2:].strip())
        elif line.startswith(">"):
            explanation.append(line[1:].strip())
        else:
            if options:
                raise PostError(f"{name}: строка «{line}» после вариантов. Вариант начинается с «- » или «+ », пояснение с «> »")
            question.append(line)

    q = " ".join(question).strip()
    expl = " ".join(explanation).strip()
    if not q:
        raise PostError(f"{name}: нет вопроса")
    if len(q) > QUESTION_LIMIT:
        raise PostError(f"{name}: вопрос длиннее {QUESTION_LIMIT} символов")
    if not 2 <= len(options) <= 10:
        raise PostError(f"{name}: вариантов должно быть от 2 до 10, сейчас {len(options)}")
    for o in options:
        if not o or len(o) > OPTION_LIMIT:
            raise PostError(f"{name}: вариант «{o}» пустой или длиннее {OPTION_LIMIT} символов")
    if correct is None:
        raise PostError(f"{name}: отметь правильный вариант знаком «+ » вместо «- »")
    if len(expl) > EXPLANATION_LIMIT:
        raise PostError(f"{name}: пояснение длиннее {EXPLANATION_LIMIT} символов ({len(expl)})")

    payload = {
        "question": q,
        "options": [{"text": o} for o in options],
        "type": "quiz",
        "correct_option_id": correct,
        "is_anonymous": True,
    }
    if expl:
        payload["explanation"] = expl
    return payload


def read_post_file(post: Post) -> str:
    return post.path.read_text(encoding="utf-8-sig")


def build_request(post: Post) -> tuple[str, dict]:
    name = f"{post.channel_folder}/{post.path.name}"
    text = read_post_file(post)
    if post.kind == "quiz":
        return "sendPoll", parse_quiz(text, name)
    return "sendMessage", {
        "text": text_to_html(text, name),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


# ---------------------------------------------------------------- Telegram API

def api(token: str, method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload or {}, timeout=30)
        except requests.RequestException as e:
            raise NetworkError(str(e)) from e
        try:
            data = r.json()
        except ValueError:
            raise NetworkError(f"странный ответ от Telegram (HTTP {r.status_code})")
        if data.get("ok"):
            return data["result"]
        retry = data.get("parameters", {}).get("retry_after")
        if retry and attempt < 2:
            time.sleep(int(retry) + 1)
            continue
        raise ApiError(data.get("description", "неизвестная ошибка"))
    raise ApiError("Telegram просит подождать, попробую позже")


def explain_api_error(msg: str) -> str:
    low = msg.lower()
    if "chat not found" in low:
        return msg + " → проверь адрес канала в config.json"
    if "not enough rights" in low or "need administrator" in low or "not a member" in low:
        return msg + " → сделай бота админом канала с правом «Публикация сообщений»"
    if "can't parse entities" in low:
        return msg + " → ошибка разметки в посте"
    if "unauthorized" in low:
        return msg + " → неверный токен бота"
    return msg


def publish(cfg: dict, post: Post, chat_override: str | None = None) -> None:
    chat = chat_override or post.chat
    if not chat:
        raise PostError(
            f"для папки «{post.channel_folder}» не указан канал в config.json → \"channels\""
        )
    method, payload = build_request(post)
    payload["chat_id"] = chat
    api(cfg["token"], method, payload)


# ---------------------------------------------------------------- основной цикл

def run_pass(cfg: dict, tz: ZoneInfo, sent: dict, reported: set) -> bool:
    """Выкладывает всё, чему пришло время. Возвращает False, если были ошибки."""
    ok = True
    now = datetime.now(tz)
    max_late = timedelta(hours=float(cfg["max_late_hours"]))
    for post in find_posts(cfg, tz):
        if post.key in sent or post.when > now:
            continue
        late = now - post.when
        if late > max_late:
            sent[post.key] = {"status": "skipped", "at": now.isoformat(timespec="seconds")}
            save_sent(sent)
            log(f"ПРОПУЩЕН {post.key}: опоздание больше {cfg['max_late_hours']} ч. "
                f"Переименуй файл на новое время, если хочешь его выложить.")
            continue
        try:
            publish(cfg, post)
        except NetworkError as e:
            if "network" not in reported:
                log(f"Нет связи с Telegram: {e}. Проверь интернет или включи VPN. Повторю автоматически.")
                reported.add("network")
            return False
        except (ApiError, PostError) as e:
            ok = False
            if post.key not in reported:
                msg = explain_api_error(str(e)) if isinstance(e, ApiError) else str(e)
                log(f"ОШИБКА {post.key}: {msg}. Исправь — бот попробует снова сам.")
                reported.add(post.key)
            continue
        reported.discard("network")
        reported.discard(post.key)
        sent[post.key] = {"status": "sent", "at": datetime.now(tz).isoformat(timespec="seconds")}
        save_sent(sent)
        log(f"ОПУБЛИКОВАН {post.key} → {post.chat}")
    return ok


def check_bot(cfg: dict) -> None:
    try:
        me = api(cfg["token"], "getMe")
    except NetworkError as e:
        log(f"Пока нет связи с Telegram ({e}). Если ты в России — включи VPN. Буду пробовать дальше.")
        return
    except ApiError as e:
        print(f"Telegram не принял токен: {explain_api_error(str(e))}")
        sys.exit(1)
    log(f"Бот @{me.get('username')} на связи")


def wait_for_soon_posts(cfg: dict, tz: ZoneInfo, sent: dict, reported: set, ahead_minutes: float) -> bool:
    """Если пост выходит в ближайшие ahead_minutes минут — дождаться и выложить в срок."""
    ok = True
    while True:
        now = datetime.now(tz)
        horizon = now + timedelta(minutes=ahead_minutes)
        soon = [p for p in find_posts(cfg, tz) if p.key not in sent and now < p.when <= horizon]
        if not soon:
            return ok
        nxt = soon[0]
        wait = (nxt.when - now).total_seconds()
        log(f"Жду {nxt.key}: выйдет в {nxt.when:%H:%M} (через {int(wait // 60)} мин {int(wait % 60)} с)")
        time.sleep(wait + 1)
        ok = run_pass(cfg, tz, sent, reported) and ok


def cmd_run(cfg: dict, tz: ZoneInfo, once: bool, ahead_minutes: float = 0) -> bool:
    check_bot(cfg)
    sent = load_sent()
    reported: set = set()
    if once:
        ok = run_pass(cfg, tz, sent, reported)
        if ahead_minutes > 0:
            ok = wait_for_soon_posts(cfg, tz, sent, reported, ahead_minutes) and ok
        return ok

    upcoming = [p for p in find_posts(cfg, tz) if p.key not in sent and p.when > datetime.now(tz)]
    if upcoming:
        nxt = upcoming[0]
        log(f"В очереди {len(upcoming)} пост(ов). Ближайший: {nxt.key} — {nxt.when:%d.%m в %H:%M}")
    else:
        log("Запланированных постов нет. Добавь файлы в папку channels/.")
    log("Работаю. Не закрывай это окно. Остановить: Ctrl+C")
    while True:
        run_pass(cfg, tz, sent, reported)
        time.sleep(CHECK_EVERY_SECONDS)


def cmd_list(cfg: dict, tz: ZoneInfo) -> None:
    sent = load_sent()
    problems: list[str] = []
    posts = find_posts(cfg, tz, problems)
    now = datetime.now(tz)
    if not posts:
        print("Постов нет.")
    for p in posts:
        if p.key in sent:
            status = "отправлен" if sent[p.key]["status"] == "sent" else "пропущен"
        elif p.when <= now:
            status = "ждёт отправки"
        else:
            status = "в очереди"
        kind = "викторина" if p.kind == "quiz" else "пост"
        chat = p.chat or "КАНАЛ НЕ УКАЗАН"
        print(f"{p.when:%d.%m %H:%M}  {kind:<9}  {chat:<20}  {p.path.name:<36}  {status}")
    for msg in problems:
        print(f"!  {msg}")


def cmd_check(cfg: dict, tz: ZoneInfo) -> bool:
    problems: list[str] = []
    posts = find_posts(cfg, tz, problems)
    for p in posts:
        try:
            if not p.chat:
                raise PostError(f"для папки «{p.channel_folder}» не указан канал в config.json")
            build_request(p)
        except PostError as e:
            problems.append(str(e))
    if problems:
        print("Найдены проблемы:")
        for msg in problems:
            print(f"  ✗ {msg}")
        return False
    print(f"Всё в порядке: проверено постов — {len(posts)}.")
    return True


def cmd_now(cfg: dict, tz: ZoneInfo, file: str, to: str | None) -> None:
    path = Path(file)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.exists():
        print(f"Файл не найден: {file}")
        sys.exit(1)
    kind = "quiz" if path.name.lower().endswith(".quiz.txt") else "text"
    folder = path.parent.name
    post = Post(path=path, channel_folder=folder, chat=cfg["channels"].get(folder),
                when=datetime.now(tz), kind=kind)
    try:
        publish(cfg, post, chat_override=to)
    except PostError as e:
        print(f"Ошибка в посте: {e}")
        sys.exit(1)
    except ApiError as e:
        print(f"Telegram отказал: {explain_api_error(str(e))}")
        sys.exit(1)
    except NetworkError as e:
        print(f"Нет связи с Telegram: {e}. Проверь интернет или включи VPN.")
        sys.exit(1)
    print(f"Отправлено в {to or post.chat}. В расписании этот пост не отмечен — он выйдет и по плану.")


def get_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        print(f"Не найден часовой пояс {name}. На Windows выполни:  pip install tzdata")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Автопостинг в Telegram-каналы")
    parser.add_argument("--list", action="store_true", help="показать расписание")
    parser.add_argument("--check", action="store_true", help="проверить посты на ошибки")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--now", metavar="ФАЙЛ", help="отправить пост прямо сейчас")
    parser.add_argument("--to", metavar="@канал", help="куда отправить пост для --now")
    parser.add_argument("--ahead", metavar="МИНУТ", type=float, default=0,
                        help="с --once: дождаться постов, которые выходят в ближайшие N минут")
    args = parser.parse_args()

    if args.check or args.list:
        # для проверки токен не обязателен
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")) if CONFIG_PATH.exists() else {}
        cfg.setdefault("channels", {})
        tz = get_tz(cfg.get("timezone", "Asia/Vladivostok"))
        if args.list:
            cmd_list(cfg, tz)
        if args.check:
            ok = cmd_check(cfg, tz)
            sys.exit(0 if ok else 1)
        return

    cfg = load_config()
    tz = get_tz(cfg["timezone"])
    if args.now:
        cmd_now(cfg, tz, args.now, args.to)
        return
    try:
        ok = cmd_run(cfg, tz, once=args.once, ahead_minutes=args.ahead)
    except KeyboardInterrupt:
        log("Остановлен.")
        return
    if args.once and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
