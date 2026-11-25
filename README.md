# iterative-debate

Скилл для Hermes Agent: проверка решения спором нескольких РАЗНЫХ семейств LLM.

На выходе — не «ещё один план», а отброшенные идеи, вердикт по каждому спорному пункту
(подтверждено / опровергнуто / не проверено) и одно выбранное действие со сроком и адресатом.

## Что внутри

- `SKILL.md` — сам процесс: режимы, 8 инвариантов, формат ответа, условие остановки,
  грабли (все наблюдавшиеся) и критерии «сработало».
- `scripts/iterative_debate.py` — драйвер: круги, окно контекста, остановка по схождению,
  стенограмма после каждого круга, `--probe` (проверка сетей), `--dry-run`, лестница
  бюджета для reasoning-моделей, fail-fast при сбое участника. Только stdlib Python 3.
- `templates/actors.example.json` — шаблон ролей.
- `templates/rejected.example.txt` — шаблон списка уже отклонённых позиций для `--rejected`.
- `scripts/mine_debate.py` — разбор готовой стенограммы числами: повторяемость действий,
  доля обрывов, сходство аргументов на лаге, итог голосования.
- `docs/case-study.md` — разбор реального прогона (20 кругов, 60 реплик): что дебаты дали,
  что из них нельзя выжать и какие правила из этого выросли.

## Установка

```bash
# как скилл Hermes (один файл)
hermes skills install https://raw.githubusercontent.com/mercael91/iterative-debate/main/SKILL.md

# драйвер
git clone https://github.com/mercael91/iterative-debate.git
cd iterative-debate
python scripts/iterative_debate.py --help
```

## Быстрый старт

```bash
# 1. какие сети живы (печатает код и текст ошибки)
python scripts/iterative_debate.py --probe --env-file .env --config actors.json

# 2. промпты без вызовов
python scripts/iterative_debate.py --topic-file topic.md --dry-run --config actors.json

# 3. боевой прогон (в фоне — reasoning-модели отвечают по 20–60 с)
python scripts/iterative_debate.py --topic-file topic.md --config actors.json \
       --env-file .env --out-dir out --rejected rejected.txt
```

По умолчанию 10 кругов и финальное голосование по предложенным действиям (`--no-vote`
отключает, `--rounds N` поднимает лимит). Итог прогона — `debate_last.json` с полями
`stop_reason` (`converged` / `stalled` / `round_limit` / `actor_dead`), `truncated`,
`rate_limited`, `model_mismatches`, `served_models`, `positions`, `vote`, `tally`.

Флаги, которые нужны на бесплатных пулах и для честного вердикта:

| Флаг | Зачем |
|---|---|
| `--min-interval 3.5` | зазор между вызовами; у OpenRouter `:free` 20 запросов/мин (50/сутки, 1000/сутки при ≥$10 кредитов, лимит на аккаунт) |
| `--judge judge.json` | отдельный судья выносит итог вместо голосования участников — снимает самоголосование |
| `--rejected rejected.txt` | список уже отклонённых позиций уходит в промпт, чтобы не предлагались снова |
| `--no-vote` | только спор, без финального выбора |

`topic.md` — вопрос и ФАКТЫ с числами, с прямым запретом выдумывать другие цифры.
`actors.json` — список `{name, base, key_env, model, role}`; шаблон в `templates/`.

Без `--config` драйвер берёт двух участников: DeepSeek напрямую (`DEEPSEEK_API_KEY`) и любой
OpenAI-совместимый эндпоинт из `DEBATE_B_BASE` / `DEBATE_B_MODEL` / `DEBATE_B_KEY`.

## Лицензия

MIT — см. `LICENSE`.
