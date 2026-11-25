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
import difflib
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

#: Значение из поля model ответа — контроль подмены сети роутером.
LAST_SERVED: dict[str, str] = {}
RATE_HITS = [0]   # сколько раз сервер ответил 429 (включая пережитые повторы)
_last_call_ts = [0.0]

NO_ARGUMENT = re.compile(r"НОВЫЙ\s+АРГУМЕНТ\s*:\s*нет", re.IGNORECASE)
NUM_TOKEN = re.compile(r"\d+(?:[.,]\d+)?")
POS_MIN = 20  # позиция короче — считаем обрывом, а не решением
VOTE_CHOICE = re.compile(r"ВЫБОР\s*:\s*(\d+)")
VOTE_CONF = re.compile(r"УВЕРЕННОСТЬ\s*:\s*([01](?:[.,]\d+)?)")


def blocks(text: str) -> dict:
    """Разбор жёсткого формата: три блока + признак «позиция дописана»."""
    def grab(pat: str) -> str:
        m = re.search(pat, text, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m and m.group(1) else ""

    crit = grab(r"КРИТИКА:\s*(.+)")
    arg = grab(r"НОВЫЙ АРГУМЕНТ:\s*(.+)")
    pos = grab(r"ПОЗИЦИЯ:\s*(.+)")
    return {"crit": crit, "arg": arg, "pos": pos, "ok": len(pos) >= POS_MIN}


def cluster_positions(reps: list[tuple[str, str]], threshold: float = 0.62) -> list[dict]:
    """Схлопывает предложенные действия: одно и то же дело разными словами — один вариант."""
    clusters: list[dict] = []
    for who, text in reps:
        b = blocks(text)
        if not b["ok"]:
            continue
        pos = b["pos"][:400]
        for c in clusters:
            if difflib.SequenceMatcher(None, c["key"], pos.lower()).ratio() >= threshold:
                c["who"].add(who)
                c["n"] += 1
                if len(pos) > len(c["text"]):
                    c["text"] = pos
                break
        else:
            clusters.append({"key": pos.lower(), "text": pos, "who": {who}, "n": 1})
    clusters.sort(key=lambda c: -c["n"])
    return clusters

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


def _once(base: str, api_key: str, model: str, messages: list, max_tokens: int,
          min_interval: float = 0.0) -> tuple[str, str]:
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
    for attempt in range(5):
        # пейсинг: у бесплатных пулов лимит в минуту, а не в час — держим зазор между вызовами
        if min_interval > 0:
            gap = time.time() - _last_call_ts[0]
            if gap < min_interval:
                time.sleep(min_interval - gap)
        _last_call_ts[0] = time.time()
        try:
            r = json.load(urllib.request.urlopen(req, timeout=180))
            choice = (r.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            LAST_SERVED["model"] = str(r.get("model") or "")
            return (msg.get("content") or "").strip(), str(choice.get("finish_reason") or "")
        except urllib.error.HTTPError as exc:
            # тело ошибки обязательно: «HTTP 409» бесполезен, QUOTA_EXHAUSTED — прямой ответ
            detail = ""
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", "replace")
                try:
                    err = (json.loads(body_text) or {}).get("error") or {}
                    detail = (str(err.get("code") or "")).strip()
                    m = (str(err.get("message") or "")).strip()
                    if m:
                        detail = (detail + ": " + m[:70]) if detail else m[:70]
                except Exception:  # noqa: BLE001 — не JSON, берём как есть
                    detail = body_text[:70]
            except Exception:  # noqa: BLE001
                detail = ""
            if exc.code == 429:
                # лимит частоты: ждём Retry-After, если сервер его дал
                RATE_HITS[0] += 1
                wait = 0.0
                try:
                    wait = float(exc.headers.get("Retry-After") or 0)
                except Exception:  # noqa: BLE001
                    wait = 0.0
                last = ("HTTP 429 %s" % detail).strip()
                # ждём ровно столько, сколько сказал сервер; без заголовка — нарастающая пауза
                time.sleep(wait if wait > 0 else min(5.0 * (attempt + 1), 30.0))
                continue
            last = ("HTTP %s %s" % (exc.code, detail)).strip()
        except Exception as exc:  # noqa: BLE001 — сеть/таймаут: пробуем ещё
            last = type(exc).__name__
        time.sleep(3 * (attempt + 1))
    return "СБОЙ: %s" % last, ""


def call(base: str, api_key: str, model: str, messages: list, max_tokens: int | None = None,
         min_interval: float = 0.0) -> str:
    best = ""
    if max_tokens:
        ladder = tuple(min(max_tokens * (2 ** i), 16000) for i in range(3))
    else:
        ladder = BUDGET_LADDER
    for budget in ladder:
        text, finish = _once(base, api_key, model, messages, budget, min_interval)
        if text.startswith("СБОЙ"):
            return text
        if len(text) > len(best):
            best = text
        if finish != "length" and len(text) >= 40:
            return text
        if finish != "length":
            return text if text else best
    return best


def build_messages(actor: dict, topic: str, ctx: str, rnd: int, rounds: int,
                   rejected: list[str] | None = None) -> list:
    user = topic
    user += ("\n\nЧТО ГОВОРИЛИ ДРУГИЕ (последние реплики):\n" + ctx) if ctx else "\n\nТы первый."
    if rejected:
        user += ("\n\nУЖЕ ОТКЛОНЕНО (предлагать повторно нельзя, даже в новой формулировке):\n"
                 + "\n".join("- " + r[:200] for r in rejected))
    user += FORMAT_RULES + "\n\nКруг %d из %d." % (rnd, rounds)
    return [
        {"role": "system", "content": actor["role"] + " Отвечай только по-русски, без вступлений."},
        {"role": "user", "content": user},
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic-file", default="", help="файл: вопрос + ФАКТЫ с числами")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--min-rounds", type=int, default=4, help="раньше этого круга не сходиться")
    ap.add_argument("--history", type=int, default=4, help="сколько последних чужих реплик видит модель")
    ap.add_argument("--out-dir", default="", help="куда писать стенограмму (по умолчанию рядом с темой)")
    ap.add_argument("--config", default="", help="JSON со списком ролей вместо дефолтных участников")
    ap.add_argument("--env-file", action="append", default=[], help="файл с ключами (KEY=VALUE); можно несколько")
    ap.add_argument("--dry-run", action="store_true", help="напечатать промпты без вызовов сети")
    ap.add_argument("--probe", action="store_true", help="пробный вызов каждой сети (1 короткий запрос)")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="стартовый бюджет реплики (0 = лестница 700→1600→4000)")
    ap.add_argument("--rejected", default="",
                    help="файл со списком уже отклонённых позиций (по строке): повторно предлагать нельзя")
    ap.add_argument("--no-vote", action="store_true",
                    help="не проводить финальное голосование по предложенным действиям")
    ap.add_argument("--judge", default="",
                    help="JSON с ОДНИМ актором-судьёй: он выбирает итог вместо голосования участников")
    ap.add_argument("--min-interval", type=float, default=0.0,
                    help="минимальный зазор между вызовами в секундах (для :free пулов 3.5 при 20 RPM)")
    a = ap.parse_args()

    rejected: list[str] = []
    if a.rejected:
        rejected = [s.strip() for s in
                    pathlib.Path(a.rejected).read_text(encoding="utf-8").splitlines()
                    if s.strip() and not s.strip().startswith("#")]

    for path in a.env_file:
        load_env(path)

    if a.config:
        actors = json.loads(pathlib.Path(a.config).read_text(encoding="utf-8"))
    else:
        actors = default_actors()

    judge = None
    if a.judge:
        jdata = json.loads(pathlib.Path(a.judge).read_text(encoding="utf-8"))
        judge = jdata[0] if isinstance(jdata, list) else jdata

    if a.probe:
        alive = 0
        for actor in actors:
            text = call(
                actor["base"],
                key(actor["key_env"]),
                actor["model"],
                [{"role": "user", "content": "ответь одним словом: ок"}],
                max_tokens=20,
                min_interval=a.min_interval,
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
            for m in build_messages(actor, topic, "", 1, a.rounds, rejected):
                print("\n[" + m["role"] + "]\n" + m["content"])
        print("=" * 70)
        print("DRY-RUN: вызовов сети не было, ролей %d" % len(actors))
        return 0

    transcript: list[dict] = []
    converged_at = None
    stalled_at = None
    aborted = ""
    stop_reason = ""
    truncated = 0
    rate_limited = 0
    mismatches = 0
    served_models: list[str] = []
    vote_result: dict = {}

    def dump() -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Дебаты (%s) — до %d кругов\n" % (stamp, a.rounds), topic]
        for m in transcript:
            lines.append("\n## Круг %d — %s\n%s\n" % (m["round"], m["who"], m["text"]))
        opts = vote_result.get("options") or []
        if opts:
            lines.append("\n## Голосование\n")
            for i, c in enumerate(opts, 1):
                lines.append("%d) %s  — предложили: %s"
                             % (i, c["text"][:300], ", ".join(sorted(c["who"]))))
            lines.append("")
            for v in vote_result.get("votes", []):
                lines.append("- %s: выбор %s, уверенность %s"
                             % (v["who"], v.get("choice") or "—", v.get("conf", "—")))
            w = vote_result.get("winner")
            if w and w.get("count") is None:
                lines.append("\n**Выбрано: %d) %s** — решение судьи, уверенность %.2f"
                             % (w["num"], w["text"][:300], w["conf"]))
            elif w:
                lines.append("\n**Выбрано: %d) %s** — голосов %d из %d, средняя уверенность %.2f"
                             % (w["num"], w["text"][:300], w["count"], len(actors), w["conf"]))
            elif opts:
                lines.append("\n**Голосование не дало решения** — выбор за человеком.")
        out.write_text("\n".join(lines), encoding="utf-8")
        last_json.write_text(
            json.dumps(
                {
                    "out": str(out),
                    "rounds": transcript[-1]["round"] if transcript else 0,
                    "converged_at": converged_at,
                    "stalled_at": stalled_at,
                    "stop_reason": stop_reason,
                    "aborted": aborted,
                    "truncated": truncated,
                    "rate_limited": rate_limited,
                    "model_mismatches": mismatches,
                    "served_models": sorted(set(served_models)),
                    "judge": bool(judge),
                    "positions": len(opts),
                    "vote": (vote_result.get("winner") or {}).get("num"),
                    "tally": vote_result.get("tally") or {},
                    "failures": sum(1 for m in transcript if m["text"].startswith("СБОЙ")),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    seen_numbers: set[str] = set()
    no_fresh = 0
    streak: dict[str, int] = {}
    for rnd in range(1, a.rounds + 1):
        texts = {}
        fresh_numbers: set[str] = set()
        for actor in actors:
            history = [m for m in transcript if m["who"] != actor["name"]][-a.history:]
            ctx = "\n\n".join(
                "### %s (круг %d):\n%s" % (m["who"], m["round"], m["text"][:1100]) for m in history
            )
            msgs = build_messages(actor, topic, ctx, rnd, a.rounds, rejected)
            LAST_SERVED["model"] = ""
            text = call(actor["base"], key(actor["key_env"]), actor["model"], msgs,
                        a.max_tokens or None, a.min_interval)
            served = LAST_SERVED.get("model", "")
            if served and not text.startswith("СБОЙ"):
                served_models.append(served)
                if served != actor["model"]:
                    # роутер может подменить сеть молча: показываем это прямо в стенограмме
                    mismatches += 1
                    text = "⟪сеть: %s вместо %s⟫\n" % (served, actor["model"]) + text
            if text.startswith("СБОЙ: HTTP 429"):
                rate_limited = RATE_HITS[0]   # лимит частоты: сеть жива, участника не считаем мёртвым
            elif not text.startswith("СБОЙ"):
                b = blocks(text)
                if not b["ok"]:
                    truncated += 1
                    text = (text + "\n\n⟪ОБРЫВ: позиция не дописана — решением не считается⟫").strip()
                else:
                    fresh_numbers |= set(NUM_TOKEN.findall(b["arg"] + " " + b["pos"])) - seen_numbers
                rate_limited = RATE_HITS[0]
            transcript.append({"round": rnd, "who": actor["name"], "text": text})
            texts[actor["name"]] = text
            if text.startswith("СБОЙ") and not text.startswith("СБОЙ: HTTP 429"):
                streak[actor["name"]] = streak.get(actor["name"], 0) + 1
            else:
                streak[actor["name"]] = 0
            print("[%d] %s: %s" % (rnd, actor["name"], text.replace("\n", " ")[:160]), flush=True)
        seen_numbers |= fresh_numbers
        dump()
        dead = [name for name, n in streak.items() if n >= 2]
        if dead:
            # один участник молчит = дебаты вырождаются в монолог; лучше встать и позвать --probe
            aborted = "сбой участников %s два круга подряд — остановка" % ", ".join(dead)
            stop_reason = "actor_dead"
            print("СТОП: " + aborted + "; проверь --probe", flush=True)
            break
        answered = [t for t in texts.values() if not t.startswith("СБОЙ")]
        # сходиться можно только когда ответили ВСЕ: иначе один живой участник объявит
        # согласие за мёртвого, и fail-fast до него не дойдёт
        if (rnd >= a.min_rounds and len(answered) == len(actors)
                and all(NO_ARGUMENT.search(t) for t in answered)):
            converged_at = rnd
            stop_reason = "converged"
            print("сошлись на круге %d" % rnd, flush=True)
            break
        # застой: новых чисел нет два круга подряд — дальше переубеждение, а не поиск ходов
        if fresh_numbers:
            no_fresh = 0
        else:
            no_fresh += 1
            if rnd >= a.min_rounds and no_fresh >= 2:
                stalled_at = rnd
                stop_reason = "stalled"
                print("СТОП: два круга без новых чисел — варианты не расширяются", flush=True)
                break
        time.sleep(2)

    if not stop_reason:
        stop_reason = "round_limit"


    # ── финальный вердикт: не «кто прав», а какое ОДНО действие берём ──
    if not a.no_vote and not aborted and (judge is not None or len(actors) >= 2):
        clusters = cluster_positions([(m["who"], m["text"]) for m in transcript
                                      if m["who"] != "Голосование"])
        voters = [judge] if judge is not None else actors
        if len(clusters) >= 2:
            listing = "\n".join("%d) %s" % (i + 1, c["text"][:300]) for i, c in enumerate(clusters))
            ask = ("Дебаты закончены. Ниже действия, которые предлагались участниками:\n" + listing
                   + "\n\nВыбери ОДНО действие и ответь строго тремя строками:\n"
                     "ВЫБОР: <номер>\nПРИЧИНА: <до 20 слов>\nУВЕРЕННОСТЬ: <0..1>")
            votes = []
            for voter in voters:
                raw = call(voter["base"], key(voter["key_env"]), voter["model"],
                           [{"role": "system",
                             "content": voter["role"] + " Отвечай только по-русски, без вступлений."},
                            {"role": "user", "content": ask}], a.max_tokens or None, a.min_interval)
                m_choice, m_conf = VOTE_CHOICE.search(raw), VOTE_CONF.search(raw)
                conf = float(m_conf.group(1).replace(",", ".")) if m_conf else 0.0
                votes.append({"who": voter["name"], "choice": int(m_choice.group(1)) if m_choice else None,
                              "conf": conf, "raw": raw[:200]})
            tally: dict[int, int] = {}
            for v in votes:
                if v["choice"] and 1 <= v["choice"] <= len(clusters):
                    tally[v["choice"]] = tally.get(v["choice"], 0) + 1
            winner = None
            if tally:
                top = max(tally.values())
                cands = [n for n, c in tally.items() if c == top]

                def mean_conf(num: int) -> float:
                    cs = [v["conf"] for v in votes if v["choice"] == num]
                    return sum(cs) / len(cs) if cs else 0.0

                cands.sort(key=lambda n: -mean_conf(n))
                best = cands[0]
                winner = {"num": best, "text": clusters[best - 1]["text"],
                          "count": None if judge is not None else tally[best], "conf": mean_conf(best)}
            vote_result = {"options": clusters, "votes": votes, "winner": winner, "tally": tally}
            dump()
            who = "СУДЬЯ" if judge is not None else "ГОЛОСОВАНИЕ"
            if winner:
                print("%s: выбрано %d) %s — %s"
                      % (who, winner["num"], winner["text"][:120],
                         "уверенность %.2f" % winner["conf"] if judge is not None
                         else "%d из %d" % (winner["count"], len(actors))), flush=True)
            else:
                print("%s: решения нет — выбор за человеком" % who, flush=True)

    dump()
    print(json.dumps({"out": str(out), "converged_at": converged_at, "stalled_at": stalled_at,
                      "stop_reason": stop_reason, "aborted": aborted, "truncated": truncated,
                      "vote": (vote_result.get("winner") or {}).get("num")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
