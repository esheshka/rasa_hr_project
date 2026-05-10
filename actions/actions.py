import logging
import re
from typing import Any, Dict, List, Text

from rasa_sdk import Action, Tracker
from rasa_sdk.events import ActiveLoop, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction

logger = logging.getLogger(__name__)

SKILLS_STUB_CANONICAL = "нет релевантных навыков (заглушка)"
YEARS_STUB_CANONICAL = "нет опыта по роли (заглушка)"


def _format_years_ru(years: float) -> str:
    """Человекочитаемый стаж для итога (избегаем «~50 г.»)."""
    if years <= 0:
        return "не удалось оценить по ответу"
    if years < 1:
        m = max(1, int(round(years * 12)))
        return f"около {m} мес."
    n = int(round(years))
    if n % 100 in (11, 12, 13, 14):
        tail = "лет"
    elif n % 10 == 1:
        tail = "год"
    elif n % 10 in (2, 3, 4):
        tail = "года"
    else:
        tail = "лет"
    return f"около {n} {tail}"


class ActionGreetUser(Action):
    """Первое приветствие — полный текст; повторные greet — короткий ответ без дублирования блока."""

    def name(self) -> Text:
        return "action_greet_user"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Any]:
        text = (tracker.latest_message.get("text") or "").strip()
        if not text:
            dispatcher.utter_message(
                text="Пустое сообщение не распознано. Напишите, например: «хочу пройти интервью» или «какие роли есть»."
            )
            return []
        if tracker.get_slot("intro_done") is True:
            dispatcher.utter_message(response="utter_greet_short")
            return []
        dispatcher.utter_message(response="utter_greet_full")
        return [SlotSet("intro_done", True)]


class ActionMarkScreeningStarted(Action):
    """Ставит флаг, что начался сценарий скрининга (для правил вне формы)."""

    def name(self) -> Text:
        return "action_mark_screening_started"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Any]:
        return [SlotSet("screening_form_started", True)]


ROLE_TITLES_RU: Dict[str, str] = {
    "pm": "Project Manager",
    "da": "Data Analyst",
    "de": "Data Engineer",
    "ds": "Data Scientist",
    "mlops": "MLOps Engineer",
}

# Явная роль не выбрана — подбор по тексту навыков в action_evaluate_candidate
ROLE_UNSPECIFIED = "unsure"

# Фразы на шаге «целевая роль» (короткие ответы безопасны: валидатор только для target_role)
_UNSURE_ROLE_PHRASES = (
    "не знаю какую роль",
    "не знаю какая роль",
    "не уверен в роли",
    "не уверена в роли",
    "любая роль",
    "любая из ролей",
    "подойдет любая роль",
    "подойдёт любая роль",
    "без предпочтений по роли",
    "роль не важна",
    "роли все равно",
    "роли всё равно",
    "сами определите роль",
    "определите роль по навыкам",
)

# Ключевые слова по роли — грубая эвристика для учебного MVP
ROLE_KEYWORDS: Dict[str, List[str]] = {
    "pm": [
        "проект",
        "roadmap",
        "scrum",
        "agile",
        "jira",
        "стейкхолдер",
        "заказчик",
        "координац",
        "планирован",
        "риск",
        "релиз",
        "команда",
    ],
    "da": [
        "sql",
        "дашборд",
        "bi",
        "метрик",
        "отчёт",
        "визуализац",
        "аналит",
        "excel",
        "requirements",
        "требован",
    ],
    "de": [
        "etl",
        "airflow",
        "spark",
        "kafka",
        "пайплайн",
        "pipeline",
        "хранилищ",
        "warehouse",
        "lake",
        "dbt",
        "оркестрац",
    ],
    "ds": [
        "модел",
        "обучен",
        "pytorch",
        "tensorflow",
        "sklearn",
        "feature",
        "фич",
        "нейросет",
        "классификац",
        "регресс",
        "nlp",
        "computer vision",
        "эксперимент",
        "offline",
        "online-оценк",
        "catboost",
        "lightgbm",
        "xgboost",
        "pandas",
        "numpy",
        "boost",
        "machine learning",
        "гиперпараметр",
        "кросс-валидац",
        "обучающ",
        "валидац модел",
    ],
    "mlops": [
        "docker",
        "kubernetes",
        "k8s",
        "ci/cd",
        "деплой",
        "serving",
        "mlflow",
        "мониторинг",
        "gpu",
        "helm",
        "terraform",
        "инфраструктур",
    ],
}


def _infer_role_from_free_text(raw: str) -> str | None:
    """Добавочная эвристика для русских/коротких формулировок (после lookup/синонимов)."""
    if not raw:
        return None
    t = raw.strip().lower().replace("ё", "е")
    if t in ("дата", "data"):
        return None
    if any(
        x in t
        for x in (
            "data scientist",
            "data science",
            "датасаент",
            "дата саент",
            "дата сайент",
            "дата сайнт",
            "дата сатист",
        )
    ):
        return "ds"
    if any(x in t for x in ("data analyst", "аналитик дан", "дата аналит")):
        return "da"
    if any(x in t for x in ("data engineer", "инженер дан", "дата инжен")):
        return "de"
    if any(x in t for x in ("project manager", "менеджер проект", "проджект", "проект менедж")):
        return "pm"
    if "mlops" in t or "млопс" in t:
        return "mlops"
    return None


def _normalize_role(raw: str | None) -> str | None:
    if not raw:
        return None
    v = raw.strip().lower().replace("ё", "е")
    if v == ROLE_UNSPECIFIED:
        return ROLE_UNSPECIFIED
    if v in ("не знаю", "хз", "не уверен", "не уверена", "не знаю."):
        return ROLE_UNSPECIFIED
    if any(p in v for p in _UNSURE_ROLE_PHRASES):
        return ROLE_UNSPECIFIED
    if v in ROLE_TITLES_RU:
        return v
    # частичное извлечение сущности job_role (например только «scientist»)
    if v == "scientist":
        return "ds"
    aliases = {
        "project manager": "pm",
        "data analyst": "da",
        "data engineer": "de",
        "data scientist": "ds",
        "mlops engineer": "mlops",
    }
    return aliases.get(v) or _infer_role_from_free_text(v)


def _parse_years(text: str | None) -> float:
    if not text:
        return 0.0
    t = text.lower().replace("ё", "е")
    if YEARS_STUB_CANONICAL.lower() in t or _is_years_stub_phrase(t):
        return 0.0
    if "месяц" in t:
        m = re.search(r"(\d+)", t)
        if m:
            return max(0.08, int(m.group(1)) / 12.0)
    if "день" in t or "дней" in t or "дня" in t:
        m = re.search(r"(\d+)", t)
        if m:
            return max(0.03, int(m.group(1)) / 365.0)
    m = re.search(r"(\d+([.,]\d+)?)", t)
    if m:
        return float(m.group(1).replace(",", "."))
    if "полгода" in t or "пол года" in t:
        return 0.5
    # родительный падеж («около трёх лет» → после ё→е: «трех»)
    spoken_gen = [
        ("десяти", 10.0),
        ("девяти", 9.0),
        ("восьми", 8.0),
        ("семи", 7.0),
        ("шести", 6.0),
        ("пяти", 5.0),
        ("четырех", 4.0),
        ("трех", 3.0),
        ("двух", 2.0),
    ]
    for word, val in spoken_gen:
        if word in t:
            return val
    spoken = [
        ("десять", 10.0),
        ("девять", 9.0),
        ("восемь", 8.0),
        ("семь", 7.0),
        ("шесть", 6.0),
        ("пять", 5.0),
        ("четыре", 4.0),
        ("три", 3.0),
        ("две", 2.0),
        ("два", 2.0),
        ("один", 1.0),
        ("одна", 1.0),
    ]
    for word, val in spoken:
        if word in t:
            return val
    return 0.0


def _is_years_stub_phrase(t_lower: str) -> bool:
    """Заглушки/формулировки «без стажа». Не путать с «50 лет» / «20 лет» (подстрока «0 лет»)."""
    t = t_lower.lower().replace("ё", "е")
    if re.search(r"(?<![\d.,])0\s*лет\b", t):
        return True
    if "ноль лет" in t:
        return True
    phrases = (
        "нет опыта",
        "без опыта",
        "опыта нет",
        "нету опыта",
        "у меня нет опыта",
        "нет у меня опыта",
        "не было опыта",
        "никакого опыта",
        "опыта вообще нет",
        "не работал",
        "не работала",
    )
    return any(p in t for p in phrases)


def _is_skills_stub_phrase(t_lower: str) -> bool:
    return any(
        x in t_lower
        for x in (
            "нет навыков",
            "без навыков",
            "навыков нет",
            "нет релевантных навыков",
            "не было навыков",
        )
    )


def _years_answer_is_nonsense(raw: str) -> bool:
    tl = raw.lower().replace("ё", "е")
    if "анекдот" in tl or "чилл" in tl or "чили" in tl:
        return True
    if "минут" in tl and "год" not in tl and "месяц" not in tl and "лет" not in tl:
        return True
    return False


def _skills_answer_is_nonsense(raw: str) -> bool:
    tl = raw.lower().replace("ё", "е")
    if _is_skills_stub_phrase(tl):
        return False
    if "анекдот" in tl and "навык" not in tl:
        return True
    if any(x in tl for x in ("пельмен", "кастрюл", "морковк", "драник", "нарезк")):
        return True
    return False


def _looks_like_compensation_not_skills(tl: str) -> bool:
    """Отсекаем ответы про зарплату/формат на шаге навыков."""
    if tl.strip() in ("обсуждаемо", "обсуждаем", "зарплата обсуждаемо", "не важно", "без разницы"):
        return True
    if "зарплат" in tl and not any(x in tl for x in ("python", "sql", "модел", "ml", "data")):
        return True
    return False


def _years_phrase_in_skills_step(tl: str) -> bool:
    """Фразы про стаж не должны попадать в слот навыков."""
    if _is_years_stub_phrase(tl):
        return True
    if "нет опыта" in tl and "навык" not in tl and "стек" not in tl and "python" not in tl:
        return True
    return False


def _keyword_hits(role: str, skills: str) -> int:
    if role not in ROLE_KEYWORDS:
        return 0
    s = skills.lower()
    return sum(1 for kw in ROLE_KEYWORDS[role] if kw in s)


def _infer_best_role_from_skills(skills: str) -> tuple[str | None, Dict[str, int]]:
    """Если роль не указана — считаем совпадения по всем ролям; при ничьей или нулях возвращаем None."""
    if not skills.strip():
        return None, {}
    hits = {r: _keyword_hits(r, skills) for r in ROLE_KEYWORDS}
    best_v = max(hits.values()) if hits else 0
    if best_v == 0:
        return None, hits
    winners = [r for r, v in hits.items() if v == best_v]
    if len(winners) != 1:
        return None, hits
    return winners[0], hits


INTERVIEW_SLOTS: tuple[str, ...] = (
    "target_role",
    "years_experience",
    "skills_summary",
    "expectations",
)

# Защита от ложного intent cancel_interview: отменяем только по явной фразе в тексте.
CANCEL_TEXT_RE = re.compile(
    r"(?is)(?:^|[\s.,;:!?«»\"'\(\[\{])"
    r"(?:отмена\s+интервью|отменить\s+интервью|отмена\s+опроса|отмена\s+скрининга|"
    r"прервать\s+опрос|прекратить\s+интервью|стоп\s+опрос|завершить\s+анкету|"
    r"хватит\s+вопросов)"
    r"(?:$|[\s.,;:!?»\"'\)\]\}])"
)


def _reset_interview_slots() -> List[Any]:
    events: List[Any] = [
        ActiveLoop(None),
        SlotSet("screening_form_started", False),
        SlotSet("intro_done", False),
    ]
    for s in INTERVIEW_SLOTS:
        events.append(SlotSet(s, None))
    return events


class ActionAbortInterviewGoodbye(Action):
    """Снимает форму и прощается."""

    def name(self) -> Text:
        return "action_abort_interview_goodbye"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        dispatcher.utter_message(response="utter_goodbye")
        return _reset_interview_slots()


class ActionAbortInterviewCancel(Action):
    """Явная отмена опроса без прощания (только если в тексте есть явная отмена)."""

    def name(self) -> Text:
        return "action_abort_interview_cancel"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        text = (tracker.latest_message.get("text") or "").strip()
        if not CANCEL_TEXT_RE.search(text):
            return ActionClarifyDuringInterview().run(dispatcher, tracker, domain)
        dispatcher.utter_message(response="utter_interview_cancelled")
        return _reset_interview_slots()


class ActionClarifyDuringInterview(Action):
    """Один ответ: остаёмся в форме, подсказка про переформулировку и заглушки."""

    def name(self) -> Text:
        return "action_clarify_during_interview"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        rs = tracker.get_slot("requested_slot") or ""
        if rs == "target_role":
            tail = (
                "Укажите роль: PM, DA, DE, DS или MLOps. Если не выбрали — «не знаю», тогда роль оценим по навыкам. "
                "Прервать: «отмена интервью»."
            )
        elif rs == "years_experience":
            tail = "Нужен стаж в годах/месяцах или фраза «нет опыта по роли». Прервать: «отмена интервью»."
        elif rs == "skills_summary":
            tail = "Нужен краткий стек и задачи или «нет навыков». Прервать: «отмена интервью»."
        elif rs == "expectations":
            tail = "Нужны формат (офис/удалёнка) и зарплата или «обсуждаемо». Прервать: «отмена интервью»."
        else:
            tail = (
                "Ответьте на текущий вопрос или используйте заглушки «нет опыта по роли», «нет навыков», «обсуждаемо». "
                "Прервать: «отмена интервью»."
            )
        by_slot = {
            "target_role": "Сейчас шаг «роль». ",
            "years_experience": "Сейчас шаг «опыт». ",
            "skills_summary": "Сейчас шаг «навыки». ",
            "expectations": "Сейчас шаг «ожидания». ",
        }
        lead = by_slot.get(rs, "Сейчас идёт анкета — ответьте на шаг формы. ")
        dispatcher.utter_message(text=lead + tail)
        return []


class ValidateInterviewForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_interview_form"

    def validate_target_role(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        if slot_value is None:
            return {}
        role = _normalize_role(str(slot_value))
        if role:
            return {"target_role": role}
        return {"target_role": None}

    def validate_years_experience(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        if slot_value is None:
            return {}
        raw = str(slot_value).strip()
        tl = raw.lower().replace("ё", "е")
        if _is_years_stub_phrase(tl):
            return {"years_experience": YEARS_STUB_CANONICAL}
        if _years_answer_is_nonsense(raw):
            return {"years_experience": None}
        if _parse_years(raw) <= 0.0 and not any(
            w in tl for w in ("нет", "без", "ноль")
        ):
            return {"years_experience": None}
        return {"years_experience": raw}

    def validate_skills_summary(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        if slot_value is None:
            return {}
        raw = str(slot_value).strip()
        tl = raw.lower().replace("ё", "е")
        if _is_skills_stub_phrase(tl):
            return {"skills_summary": SKILLS_STUB_CANONICAL}
        if _years_phrase_in_skills_step(tl) or _looks_like_compensation_not_skills(tl):
            return {"skills_summary": None}
        if len(raw.strip()) < 8:
            return {"skills_summary": None}
        if _skills_answer_is_nonsense(raw):
            return {"skills_summary": None}
        return {"skills_summary": raw}

    def validate_expectations(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        if slot_value is None:
            return {}
        raw = str(slot_value).strip()
        tl = raw.lower().replace("ё", "е")
        if len(raw) < 3:
            return {"expectations": None}
        if "чилл" in tl or ("анекдот" in tl and "работ" not in tl):
            return {"expectations": None}
        if "просто хочу" in tl and "работ" not in tl and "офис" not in tl and "удал" not in tl:
            return {"expectations": None}
        return {"expectations": raw}


class ActionEvaluateCandidate(Action):
    """Оценивает соответствие заявленной роли по опыту и тексту навыков."""

    def name(self) -> Text:
        return "action_evaluate_candidate"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        raw_role = tracker.get_slot("target_role")
        years_text = tracker.get_slot("years_experience") or ""
        skills = tracker.get_slot("skills_summary") or ""
        expectations = tracker.get_slot("expectations") or ""

        claimed = _normalize_role(str(raw_role)) if raw_role is not None else None
        years_lower = years_text.lower()
        skills_lower = skills.lower()
        is_years_stub = YEARS_STUB_CANONICAL in years_text or _is_years_stub_phrase(years_lower)
        is_skills_stub = SKILLS_STUB_CANONICAL in skills or _is_skills_stub_phrase(skills_lower)

        years = _parse_years(years_text)

        reasons: List[str] = []
        inferred_from_skills = False
        effective_role: str | None = None

        if claimed == ROLE_UNSPECIFIED:
            inferred, _hits_map = _infer_best_role_from_skills(skills)
            if inferred:
                effective_role = inferred
                inferred_from_skills = True
            else:
                if not skills.strip() or (not is_skills_stub and len(skills.strip()) < 8):
                    reasons.append(
                        "роль не указана явно — чтобы сопоставить вас с одной из пяти позиций по навыкам, "
                        "нужен более развёрнутый ответ про стек и задачи"
                    )
                elif _hits_map and max(_hits_map.values()) == 0:
                    reasons.append(
                        "роль не указана явно; по тексту навыков не удалось отнести ни к одной из пяти ролей команды"
                    )
                else:
                    reasons.append(
                        "роль не указана явно; по навыкам нет однозначного лидера среди пяти ролей "
                        "(похоже на несколько профилей сразу или ответ слишком общий)"
                    )
        else:
            effective_role = claimed
            if not effective_role:
                reasons.append("не удалось однозначно сопоставить роль с одной из пяти позиций команды")

        year_ok = years >= 0.5 or is_years_stub
        if not year_ok:
            reasons.append("релевантный стаж выглядит слишком коротким для самостоятельной работы в роли")

        skill_len_ok = len(skills.strip()) >= 8 or is_skills_stub
        if not skill_len_ok:
            reasons.append("описание навыков слишком общее — не видно конкретного стека и задач")

        if inferred_from_skills:
            keyword_ok = True
        elif effective_role:
            keyword_hits = _keyword_hits(effective_role, skills)
            keyword_ok = keyword_hits >= 1 or is_skills_stub
            if not keyword_ok:
                reasons.append(
                    f"по тексту навыков слабо прослеживается типичный профиль для роли "
                    f"«{ROLE_TITLES_RU[effective_role]}»; при смешанном стеке (например, модели и прод) "
                    f"уточнит рекрутёр"
                )
        else:
            keyword_ok = False

        if is_years_stub:
            reasons.append("по опыту указана заглушка вместо конкретного стажа")
        if is_skills_stub:
            reasons.append("по навыкам указана заглушка вместо стека и задач")

        both_stubs = is_years_stub and is_skills_stub
        fit = (
            effective_role is not None
            and year_ok
            and skill_len_ok
            and keyword_ok
            and not both_stubs
        )

        role_title = ROLE_TITLES_RU[effective_role] if effective_role else None

        if both_stubs:
            fit = False
            reasons = [
                "одновременно выбраны заглушки по опыту и по навыкам — для скрининга нужен "
                "хотя бы один содержательный ответ (стаж или стек/задачи)"
            ]

        restart_hint = (
            "\n\nСнова пройти скрининг: «хочу пройти интервью» или /restart в rasa shell."
        )

        if fit and role_title:
            unsure_lead = ""
            if inferred_from_skills:
                unsure_lead = (
                    "Роль вы не указали — совпадение с позицией по **навыкам** (эвристика). "
                    "Если неверно, начните заново и назовите роль явно.\n\n"
                )
            exp_line = expectations.strip() or "—"
            msg = (
                f"Итог: предварительно вы **подходите** на роль **{role_title}**.\n"
                f"{unsure_lead}"
                f"Опыт по ответу: {_format_years_ru(years)}.\n"
                f"Ожидания: {exp_line}\n\n"
                "Рекрутёр может связаться для следующего этапа. "
                "Оценка автоматическая и ориентировочная."
                f"{restart_hint}"
            )
        else:
            role_line = (
                f"для роли «{role_title}»"
                if role_title
                else "для заявленной позиции в ML-команде"
            )
            uniq: List[str] = []
            for r in reasons:
                if r not in uniq:
                    uniq.append(r)
            if not uniq:
                uniq.append("недостаточно данных для положительного заключения")
            msg = (
                "Итог: по ответам **не проходите** порог первичного соответствия "
                f"{role_line}.\n\n"
                "Почему:\n• " + "\n• ".join(uniq)
                + "\n\n"
                "Напишите конкретнее или используйте заглушки только там, где правда нечего добавить."
                f"{restart_hint}"
            )

        dispatcher.utter_message(text=msg)

        return [
            SlotSet("target_role", None),
            SlotSet("years_experience", None),
            SlotSet("skills_summary", None),
            SlotSet("expectations", None),
            SlotSet("screening_form_started", False),
            SlotSet("intro_done", False),
        ]
