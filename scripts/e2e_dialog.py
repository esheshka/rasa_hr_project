#!/usr/bin/env python3
"""
E2E-прогон через REST webhook (нужны: rasa run --enable-api -p 5006 и rasa run actions -p 5055).
Запуск: python scripts/e2e_dialog.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:5006/webhooks/rest/webhook"
ACTIONS_HEALTH = "http://localhost:5055/health"


def send(sender: str, message: str) -> list[dict]:
    payload = json.dumps({"sender": sender, "message": message}).encode("utf-8")
    req = urllib.request.Request(
        BASE,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def last_text(msgs: list[dict]) -> str:
    texts = [m.get("text") or "" for m in msgs if m.get("text")]
    return "\n".join(texts).strip()


def run_case(name: str, sender: str, steps: list[str], check) -> tuple[bool, str]:
    try:
        acc: list[tuple[str, list[dict]]] = []
        for m in steps:
            out = send(sender, m)
            acc.append((m, out))
        ok, detail = check(acc)
        return ok, f"{name}: {'OK' if ok else 'FAIL'} — {detail}"
    except urllib.error.HTTPError as e:
        return False, f"{name}: HTTP {e.code} {e.read()[:200]!r}"
    except Exception as e:
        return False, f"{name}: {type(e).__name__}: {e}"


def main() -> int:
    try:
        urllib.request.urlopen(ACTIONS_HEALTH, timeout=2)
    except Exception:
        print("Пропуск: action server недоступен на :5055 (curl health). Запустите rasa run actions.")
        return 2

    results: list[tuple[bool, str]] = []

    def check_hi(acc):
        joined = "\n".join(last_text(o) for _, o in acc)
        j = joined.lower()
        if j.count("здесь только скрининг") > 2:
            return False, "зацикливание out_of_scope"
        if "hr-ассистент" not in j and "здравствуйте" not in j:
            return False, f"нет приветствия: {joined[:200]!r}"
        # Первый turn — полное приветствие; второй greet — короткий (один блок без двойного блока про интервью)
        if j.count("могу провести короткое интервью") > 1:
            return False, "дублирование полного приветствия"
        return True, "привет + hi"

    results.append(
        run_case(
            "greet_then_hi",
            "e2e_greet_hi",
            ["/restart", "Привет", "hi"],
            check_hi,
        )
    )

    def check_empty_msg(acc):
        joined = "\n".join(last_text(o) for _, o in acc)
        if "Пустое сообщение" not in joined and "не распознано" not in joined:
            return False, joined[:300]
        return True, "пустой ввод"

    results.append(
        run_case(
            "empty_after_greet",
            "e2e_empty",
            ["/restart", "Привет", ""],
            check_empty_msg,
        )
    )

    def check_garbage(acc):
        t = last_text(acc[-1][1])
        if len(t) < 5:
            return False, "пустой ответ"
        return True, "fallback"

    results.append(run_case("garbage", "e2e_garbage", ["asdfghjkl qwerty 12345"], check_garbage))

    def check_roles(acc):
        t = last_text(acc[-1][1])
        if "Data Scientist" not in t or "MLOps" not in t:
            return False, t[:300]
        return True, "роли"

    results.append(run_case("ask_roles", "e2e_roles", ["Какие роли есть?"], check_roles))

    def check_full(acc):
        final = last_text(acc[-1][1])
        if not final:
            return False, "пустой ответ (проверьте rasa run actions)"
        if "подходите" not in final.lower():
            return False, f"итог: {final[:500]}"
        return True, "happy path"

    results.append(
        run_case(
            "full_ds",
            "e2e_full_ds",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "4 года",
                "обучение классификаторов в pytorch и sklearn, эксперименты с нейросетями",
                "удалёнка, зарплата обсуждаемо",
            ],
            check_full,
        )
    )

    def check_unsure_infer(acc):
        final = last_text(acc[-1][1])
        if not final:
            return False, "пустой ответ (проверьте rasa run actions)"
        if "подходите" not in final.lower():
            return False, f"итог: {final[:500]}"
        if "навык" not in final.lower() and "явно" not in final.lower():
            return False, f"ожидали пометку про автоподбор: {final[:500]}"
        return True, "не знаю роль → DS по навыкам"

    results.append(
        run_case(
            "unsure_role_infer_ds",
            "e2e_unsure_ds",
            [
                "/restart",
                "Хочу пройти интервью",
                "не знаю",
                "4 года",
                "обучение классификаторов в pytorch и sklearn, эксперименты с нейросетями",
                "удалёнка, зарплата обсуждаемо",
            ],
            check_unsure_infer,
        )
    )

    def check_fifty_years(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        if "~0" in t and "50" not in t:
            return False, f"«50 лет» ошибочно как ~0: {t[:500]}"
        if "подходите" not in t.lower():
            return False, f"итог: {t[:500]}"
        return True, "50 лет"

    results.append(
        run_case(
            "fifty_years_pass",
            "e2e_50y",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "50 лет",
                "pytorch sklearn catboost, внедрял модели в прод",
                "офис, обсуждаемо",
            ],
            check_fifty_years,
        )
    )

    def check_reject(acc):
        final = last_text(acc[-1][1])
        if not final:
            return False, "пустой ответ"
        if "не проходит" not in final.lower():
            return False, f"ожидали отказ: {final[:500]}"
        return True, "отказ"

    results.append(
        run_case(
            "reject_weak_skills",
            "e2e_reject",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "5 лет",
                "jira scrum agile только координация встреч",
                "офис",
            ],
            check_reject,
        )
    )

    def check_bye(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        if "время" not in t.lower() and "Спасибо" not in t:
            return False, t[:200]
        return True, "goodbye"

    results.append(run_case("goodbye", "e2e_bye", ["/restart", "Пока"], check_bye))

    def check_oos(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        if "скрининг" not in t.lower() and "рол" not in t.lower():
            return False, t[:200]
        return True, "oos"

    results.append(run_case("oos", "e2e_oos", ["/restart", "Расскажи анекдот"], check_oos))

    def check_pre_role(acc):
        t = last_text(acc[-1][1])
        if "хочу пройти интервью" not in t.lower():
            return False, t[:400]
        return True, "подсказка до анкеты"

    results.append(
        run_case(
            "role_before_interview",
            "e2e_pre_role",
            ["/restart", "Data Scientist"],
            check_pre_role,
        )
    )

    def check_form_oos(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        tl = t.lower()
        ok = (
            "шаг" in tl
            and "роль" in tl
        ) or "прервать" in tl
        if not ok:
            return False, f"ожидали подсказку внутри формы, получили: {t[:300]}"
        return True, "остались в форме"

    results.append(
        run_case(
            "form_interrupt_oos",
            "e2e_form_oos",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "Расскажи анекдот",
            ],
            check_form_oos,
        )
    )

    def check_form_cancel(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        if "остановлен" not in t.lower() and "отмен" not in t.lower():
            return False, f"ожидали отмену: {t[:300]}"
        return True, "отмена в форме"

    results.append(
        run_case(
            "form_cancel",
            "e2e_form_cancel",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "отмена интервью",
            ],
            check_form_cancel,
        )
    )

    def check_skills_pelmeni(acc):
        t = last_text(acc[-1][1])
        if not t:
            return False, "пустой ответ"
        if "остановлен" in t.lower():
            return False, "ложная отмена интервью"
        return True, "остались в форме (навыки)"

    results.append(
        run_case(
            "skills_no_false_cancel",
            "e2e_pelmeni",
            [
                "/restart",
                "Хочу пройти интервью",
                "data scientist",
                "3 года",
                "Я просто варил пельмени",
            ],
            check_skills_pelmeni,
        )
    )

    def check_long(acc):
        return (True, "ok") if acc[-1][1] is not None else (False, "none")

    long_msg = "а" * 4000 + " хочу пройти интервью"
    results.append(
        run_case(
            "long_then_start",
            "e2e_long",
            ["/restart", long_msg],
            check_long,
        )
    )

    failed = 0
    for ok, line in results:
        print(line)
        if not ok:
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
