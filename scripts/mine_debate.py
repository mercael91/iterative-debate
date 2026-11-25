#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Что прогон дебатов реально дал — числами по стенограмме.

    python mine_debate.py debate_20260101_1200.md
    python mine_debate.py debate.md --lag 3 --top 5

Печатает: реплики/круги/участники, сколько раз «НОВЫЙ АРГУМЕНТ: нет», долю обрывов
(нет `ПОЗИЦИЯ` или она короче 20 символов), какие ДЕЙСТВИЯ повторяются (ключ — первые
значимые слова позиции, цифры выброшены) и сходство аргументов участника с его же
аргументом `lag` кругов назад.

Зачем: без замера отчёт о дебатах скатывается в пересказ. «Вырождение» надо отличать
от «повторяется само решение»: текст может быть новым при том же множестве ходов.
Зависимостей нет — только стандартная библиотека.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import statistics

ROUND_RE = re.compile(r"\n## Круг (\d+) — ([^\n]+)\n")
ARG_RE = re.compile(r"НОВЫЙ АРГУМЕНТ:\s*(.+)", re.S)
POS_RE = re.compile(r"ПОЗИЦИЯ:\s*(.+)", re.S)
WORD_RE = re.compile(r"[а-яёa-z]{4,}")
STOP = {
    "который", "чтобы", "этого", "этому", "также", "можно", "нужно", "будет", "если",
    "после", "перед", "через", "только", "всего", "более", "менее", "либо", "иначе",
    "the", "and", "for", "with", "that", "this",
}


def parse(text: str) -> list[dict]:
    parts = ROUND_RE.split(text)
    replies = []
    for i in range(1, len(parts), 3):
        rnd, who, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]
        arg = ARG_RE.search(body)
        pos = POS_RE.search(body)
        replies.append(
            {
                "round": rnd,
                "who": who,
                "arg": (arg.group(1).strip() if arg else ""),
                "pos": (pos.group(1).strip() if pos else ""),
            }
        )
    return replies


def action_key(pos: str) -> str:
    """Ключ действия: первые значимые слова позиции без цифр, цен и сроков."""
    words = [w for w in WORD_RE.findall(pos.lower()) if w not in STOP]
    return " ".join(words[:4])


def shingles(text: str) -> set:
    words = [w for w in WORD_RE.findall(text.lower()) if w not in STOP]
    return set(zip(words, words[1:], words[2:])) if len(words) > 2 else set()


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript", help="стенограмма debate_*.md драйвера")
    ap.add_argument("--lag", type=int, default=3, help="лаг сравнения аргументов в кругах")
    ap.add_argument("--top", type=int, default=5, help="сколько повторяющихся действий печатать")
    a = ap.parse_args()

    path = pathlib.Path(a.transcript)
    text = path.read_text(encoding="utf-8")
    vote_text = ""
    if "\n## Голосование" in text:
        text, vote_text = text.split("\n## Голосование", 1)
    replies = parse(text)
    if not replies:
        print("не разобрано ни одной реплики — это точно стенограмма драйвера? (## Круг N — роль)")
        return 1

    total = len(replies)
    rounds = max(r["round"] for r in replies)
    actors = sorted({r["who"] for r in replies})
    no_arg = sum(1 for r in replies if r["arg"].lstrip("* —-\t").lower().startswith("нет"))
    truncated = sum(1 for r in replies if len(r["pos"]) < 20)

    actions: collections.Counter = collections.Counter()
    for r in replies:
        key = action_key(r["pos"]) or action_key(r["arg"])
        if key:
            actions[key] += 1

    # сходство аргументов: свой аргумент против своего же через lag кругов
    sims = []
    by_actor: dict[str, list[dict]] = collections.defaultdict(list)
    for r in replies:
        by_actor[r["who"]].append(r)
    for items in by_actor.values():
        items = sorted(items, key=lambda x: x["round"])
        for i in range(len(items) - a.lag):
            first, later = shingles(items[i]["arg"]), shingles(items[i + a.lag]["arg"])
            if first and later:
                sims.append(jaccard(first, later))

    print(f"реплик: {total} | кругов: {rounds} | участников: {len(actors)} ({', '.join(actors)})")
    print(f"«НОВЫЙ АРГУМЕНТ: нет»: {no_arg} ({no_arg / total * 100:.0f} %)")
    print(f"обрывов (нет ПОЗИЦИИ или короче 20 симв.): {truncated} ({truncated / total * 100:.0f} %)")
    print("\nповторяющиеся действия:")
    for key, n in actions.most_common(a.top):
        print(f"  {n:3} ({n / total * 100:4.0f} %)  {key}")
    if sims:
        print(
            "\nсходство аргументов на лаге %d: среднее %.2f, «почти повтор» (>0,5): %d из %d"
            % (a.lag, statistics.mean(sims), sum(1 for s in sims if s > 0.5), len(sims))
        )
        print(
            "трактовка: высокое сходство аргументов = спор встал; высокий повтор действий "
            "при низком сходстве = формулировки новые, множество ходов то же"
        )
    if vote_text:
        win = re.search(r"\*\*Выбрано: (\d+)\) (.+?)\*\* — (?:голосов (\d+) из (\d+)|решение судьи, уверенность ([\d.]+))",
                        vote_text)
        opts = len(re.findall(r"^\d+\) ", vote_text, re.M))
        print("\nголосование: вариантов %d" % opts)
        if win and win.group(3):
            print("  выбрано: %s) %s — %s из %s голосов" % (win.group(1), win.group(2)[:120],
                                                             win.group(3), win.group(4)))
        elif win:
            print("  выбрано судьёй: %s) %s — уверенность %s" % (win.group(1), win.group(2)[:120],
                                                                 win.group(5)))
        else:
            print("  решения нет — выбор за человеком")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
