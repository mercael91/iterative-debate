#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Итерационные дебаты нескольких разных сетей на ОДИН вопрос.

Тема и факты — из файла, роли с конфликтом интересов — из конфига, ответы в жёстком
формате, остановка по схождению, стенограмма после каждого круга.

    python iterative_debate.py --probe --env-file .env --config actors.json
    python iterative_debate.py --topic-file topic.md --dry-run --config actors.json
    python iterative_debate.py --topic-file topic.md --config actors.json \
           --env-file .env --rounds 20 --out-dir out

Роли: список объектов {name, base, key_env, model, role} в JSON (--config).
Без --config берутся два дефолтных участника: DeepSeek напрямую и любой
OpenAI-совместимый эндпоинт из DEBATE_B_BASE / DEBATE_B_MODEL / DEBATE_B_KEY.
Зависимостей нет: только стандартная библиотека Python 3.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

FORMAT_RULES = (
    "\n\nОТВЕТЬ СТРОГО В ТАКОМ ВИДЕ (не длиннее 160 слов):\n"
    "КРИТИКА: что не так в позициях других (или в общей рамке).\n"
    "НОВЫЙ АРГУМЕНТ: один, с числом, сроком или конкретным фактом. "
    "Если нового нет — напиши «нет».\n"
    "ПОЗИЦИЯ: одно действие, которое делаем в ближайшие 3 дня, с ценой и адресатом.\n"
)

#: Лестница бюджета: reasoning-модели тратят max_tokens на рассуждение и отдают
#: обрезанный content («К»). При finish_reason=length поднимаем лимит и повторяем.
BUDGET_LADDER = (700, 1600, 4000)

NO_ARGUMENT = re.compile(r"НОВЫЙ\s+АРГУМЕНТ\s*:\s*нет", re.IGNORECASE)

OPERATOR_ROLE = (
    "Ты ведёшь дело руками: знаешь, что реально делается за день, сколько стоит время "
    "и где ломается. Твоя задача — только то, что можно начать завтра без нового кода."
)
SKEPTIC_ROLE = (
    "Ты — заказчик, который уже дважды не заплатил. Ищешь, где цена завышена, где "
    "обещание не подтверждается, и почему такому исполнителю не дадут деньги вперёд."
)


#: Значения из --env-file держим отдельно: окружение процесса не трогаем.
ENV: dict[str, str] = {}


def key(name: str, default: str = "") -> str:
    """Значение по имени: сначала из --env-file, потом из окружения процесса."""
    if name in ENV:
        return ENV[name]
    return os.environ.get(name, default)


def default_actors() -> list[dict]:
    """Два участника по умолчанию: DeepSeek напрямую + второй OpenAI-совместимый эндпоинт."""
    actors = [
        {
            "name": "Оператор",
            "base": key("DEBATE_A_BASE", "https://api.deepseek.com/v1"),
            "key_env": key("DEBATE_A_KEY_ENV", "DEEPSEEK_API_KEY"),
            "model": key("DEBATE_A_MODEL", "deepseek-chat"),
            "role": OPERATOR_ROLE,
        }
    ]
    base = key("DEBATE_B_BASE")
    if base:
        actors.append(
            {
                "name": "Скептик",
                "base": base,
                "key_env": "DEBATE_B_KEY",
                "model": key("DEBATE_B_MODEL"),
                "role": SKEPTIC_ROLE,
            }
        )
    return actors


def load_env(path: str) -> None:
    """KEY=VALUE построчно в ENV; уже заданные значения не перезаписываются."""
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            ENV.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _once(base: str, api_key: str, model: str, messages: list, max_tokens: int) -> tuple[str, str]:
    """Один вызов. Возвращает (контент, finish_reason); при сбое — («СБОЙ: ...», "")."""
    if not api_key:
        return "СБОЙ: нет ключа для " + model, ""
    if not model:
        return "СБОЙ: не задана модель", ""
    body = json.dumps(
        {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
    ).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    last = ""
    for attempt in range(3):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=180))
            choice = (r.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            return (msg.get("content") or "").strip(), str(choice.get("finish_reason") or "")
        except urllib.error.HTTPError as exc:
            # тело ошибки обязательно: «HTTP 409» бесполезен, QUOTA_EXHAUSTED — прямой ответ
            detail = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    err = (json.loads(raw) or {}).get("error") or {}
                    detail = (str(err.get("code") or "")).strip()
                    m = (str(err.get("message") or "")).strip()
                    if m:
                        detail = (detail + ": " + m[:70]) if detail else m[:70]
                except Exception:  # noqa: BLE001 — не JSON, берём как есть
                    detail = raw[:70]
            except Exception:  # noqa: BLE001
                detail = ""
            last = ("HTTP %s %s" % (exc.code, detail)).strip()
        except Exception as exc:  # noqa: BLE001 — сеть/таймаут: пробуем ещё
            last = type(exc).__name__
        time.sleep(3 * (attempt + 1))
    return "СБОЙ: %s" % last, ""


def call(base: str, api_key: str, model: str, messages: list, max_tokens: int | None = None) -> str:
    best = ""
    if max_tokens:
        ladder = tuple(min(max_tokens * (2 ** i), 16000) for i in range(3))
    else:
        ladder = BUDGET_LADDER
    for budget in ladder:
        text, finish = _once(base, api_key, model, messages, budget)
        if text.startswith("СБОЙ"):
            return text
        if len(text) > len(best):
            best = text
        if finish != "length" and len(text) >= 40:
            return text
        if finish != "length":
            return text if text else best
    return best


def build_messages(actor: dict, topic: str, ctx: str, rnd: int, rounds: int) -> list:
    user = topic
    user += ("\n\nЧТО ГОВОРИЛИ ДРУГИЕ (последние реплики):\n" + ctx) if ctx else "\n\nТы первый."
    user += FORMAT_RULES + "\n\nКруг %d из %d." % (rnd, rounds)
    return [
        {"role": "system", "content": actor["role"] + " Отвечай только по-русски, без вступлений."},
        {"role": "user", "content": user},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic-file", default="", help="файл: вопрос + ФАКТЫ с числами")
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--min-rounds", type=int, default=4, help="раньше этого круга не сходиться")
    ap.add_argument("--history", type=int, default=4, help="сколько последних чужих реплик видит модель")
    ap.add_argument("--out-dir", default="", help="куда писать стенограмму (по умолчанию рядом с темой)")
    ap.add_argument("--config", default="", help="JSON со списком ролей вместо дефолтных участников")
    ap.add_argument("--env-file", action="append", default=[], help="файл с ключами (KEY=VALUE); можно несколько")
    ap.add_argument("--dry-run", action="store_true", help="напечатать промпты без вызовов сети")
    ap.add_argument("--probe", action="store_true", help="пробный вызов каждой сети (1 короткий запрос)")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="стартовый бюджет реплики (0 = лестница 700→1600→4000)")
    a = ap.parse_args()

    for path in a.env_file:
        load_env(path)

    if a.config:
        actors = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8"))
    else:
        actors = default_actors()
        if len(actors) < 2:
            print(
                "ПРЕДУПРЕЖДЕНИЕ: второй участник не настроен — дебаты вырождаются в монолог.\n"
                "Задай DEBATE_B_BASE / DEBATE_B_MODEL / DEBATE_B_KEY (или --config actors.json).",
                file=sys.stderr,
            )

    if a.probe:
        alive = 0
        for actor in actors:
            text = call(
                actor["base"],
                key(actor["key_env"]),
                actor["model"],
                [{"role": "user", "content": "ответь одним словом: ок"}],
                max_tokens=20,
            )
            ok = not text.startswith("СБОЙ")
            alive += ok
            print("%-12s %-26s %s" % (actor["name"], actor["model"], "ЖИВА: " + text[:40] if ok else text))
        print("живых сетей: %d из %d" % (alive, len(actors)))
        return 0 if alive == len(actors) else 1

    if not a.topic_file:
        ap.error("нужен --topic-file (или --probe)")

    topic = pathlib.Path(a.topic_file).read_text(encoding="utf-8")

    stamp = time.strftime("%Y%m%d_%H%M")
    out_dir = pathlib.Path(a.out_dir) if a.out_dir else pathlib.Path(a.topic_file).resolve().parent
    out = out_dir / ("debate_%s.md" % stamp)
    last_json = out_dir / "debate_last.json"

    if a.dry_run:
        for actor in actors:
            print("=" * 70)
            print("РОЛЬ: %s  |  %s / %s  |  ключ: %s"
                  % (actor["name"], actor["base"], actor["model"], actor["key_env"]))
            print("КЛЮЧ НАЙДЕН:", bool(key(actor["key_env"])))
            for m in build_messages(actor, topic, "", 1, a.rounds):
                print("\n[" + m["role"] + "]\n" + m["content"])
        print("=" * 70)
        print("DRY-RUN: вызовов сети не было, ролей %d" % len(actors))
        return 0

    transcript: list[dict] = []
    converged_at = None
    aborted = ""

    def dump() -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Дебаты (%s) — до %d кругов\n" % (stamp, a.rounds), topic]
        for m in transcript:
            lines.append("\n## Круг %d — %s\n%s\n" % (m["round"], m["who"], m["text"]))
        out.write_text("\n".join(lines), encoding="utf-8")
        last_json.write_text(
            json.dumps(
                {
                    "out": str(out),
                    "rounds": transcript[-1]["round"] if transcript else 0,
                    "converged_at": converged_at,
                    "aborted": aborted,
                    "failures": sum(1 for m in transcript if m["text"].startswith("СБОЙ")),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    streak: dict[str, int] = {}
    for rnd in range(1, a.rounds + 1):
        texts = {}
        for actor in actors:
            history = [m for m in transcript if m["who"] != actor["name"]][-a.history:]
            ctx = "\n\n".join(
                "### %s (круг %d):\n%s" % (m["who"], m["round"], m["text"][:1100]) for m in history
            )
            msgs = build_messages(actor, topic, ctx, rnd, a.rounds)
            text = call(actor["base"], key(actor["key_env"]), actor["model"], msgs,
                        a.max_tokens or None)
            transcript.append({"round": rnd, "who": actor["name"], "text": text})
            texts[actor["name"]] = text
            if text.startswith("СБОЙ"):
                streak[actor["name"]] = streak.get(actor["name"], 0) + 1
            else:
                streak[actor["name"]] = 0
            print("[%d] %s: %s" % (rnd, actor["name"], text.replace("\n", " ")[:160]), flush=True)
        dump()
        dead = [name for name, n in streak.items() if n >= 2]
        if dead:
            # один участник молчит = дебаты вырождаются в монолог; лучше встать и позвать --probe
            aborted = "сбой участников %s два круга подряд — остановка" % ", ".join(dead)
            print("СТОП: " + aborted + "; проверь --probe", flush=True)
            break
        answered = [t for t in texts.values() if not t.startswith("СБОЙ")]
        # сходиться можно только когда ответили ВСЕ: иначе один живой участник объявит
        # согласие за мёртвого, и fail-fast до него не дойдёт
        if (rnd >= a.min_rounds and len(answered) == len(actors)
                and all(NO_ARGUMENT.search(t) for t in answered)):
            converged_at = rnd
            print("сошлись на круге %d" % rnd, flush=True)
            break
        time.sleep(2)

    dump()
    print(json.dumps({"out": str(out), "converged_at": converged_at, "aborted": aborted},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
