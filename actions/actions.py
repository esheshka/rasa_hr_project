import logging
import os
import re
from typing import Any, Dict, List, Text

import yaml
from rasa_sdk import Action, Tracker
from rasa_sdk.events import ActiveLoop, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormValidationAction

from .screening_criteria import QUIZ_QUESTIONS, extract_answer_choice

logger = logging.getLogger(__name__)


def _load_screening_config() -> Dict[str, Any]:
    """Читает секцию screening из config.yml рядом с корнем проекта."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base, "config.yml")
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("screening", {})
    except Exception as exc:
        logger.warning("Не удалось прочитать config.yml: %s", exc)
        return {}


_SCREENING_CFG = _load_screening_config()
MIN_CORRECT_ANSWERS: int = int(_SCREENING_CFG.get("min_correct_answers", 3))
MIN_YEARS_EXPERIENCE: float = float(_SCREENING_CFG.get("min_years_experience", 0))

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

QUIZ_SLOTS: tuple[str, ...] = (
    "quiz_role",
    "quiz_q1_answer",
    "quiz_q2_answer",
    "quiz_q3_answer",
    "quiz_q4_answer",
    "quiz_q5_answer",
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
    for s in QUIZ_SLOTS:
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
        if rs and rs.startswith("quiz_q"):
            q_num = rs.replace("quiz_q", "").replace("_answer", "")
            tail = f"Сейчас шаг квиза — вопрос {q_num}. Введите цифру 1, 2, 3 или 4. Прервать: «отмена интервью»."
            dispatcher.utter_message(text=tail)
            return []
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
    """Определяет роль, выводит сводку и активирует quiz_form."""

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

        claimed = _normalize_role(str(raw_role)) if raw_role is not None else None
        effective_role: str | None = None

        if claimed == ROLE_UNSPECIFIED:
            inferred, _ = _infer_best_role_from_skills(skills)
            effective_role = inferred
        else:
            effective_role = claimed

        if effective_role not in ROLE_TITLES_RU:
            dispatcher.utter_message(
                text=(
                    "Не удалось однозначно определить роль для квиза.\n"
                    "Повторите скрининг, указав роль явно: PM, DA, DE, DS или MLOps.\n\n"
                    "Начать заново: «хочу пройти интервью»."
                )
            )
            reset = [SlotSet(s, None) for s in INTERVIEW_SLOTS]
            reset += [SlotSet("screening_form_started", False), SlotSet("intro_done", False)]
            reset += [SlotSet(s, None) for s in QUIZ_SLOTS]
            return reset

        role_title = ROLE_TITLES_RU[effective_role]
        years = _parse_years(years_text)

        dispatcher.utter_message(
            text=(
                f"Базовая информация принята.\n"
                f"Роль: {role_title} | Опыт: {_format_years_ru(years)}\n\n"
                f"Сейчас 5 технических вопросов по роли «{role_title}».\n"
                f"Отвечайте цифрой: 1, 2, 3 или 4.\n"
                f"Прервать: «отмена интервью»."
            )
        )

        events: List[Any] = [SlotSet(s, None) for s in INTERVIEW_SLOTS]
        events += [
            SlotSet("screening_form_started", False),
            SlotSet("quiz_role", effective_role),
            SlotSet("quiz_q1_answer", None),
            SlotSet("quiz_q2_answer", None),
            SlotSet("quiz_q3_answer", None),
            SlotSet("quiz_q4_answer", None),
            SlotSet("quiz_q5_answer", None),
            ActiveLoop("quiz_form"),
        ]
        return events


class ActionAskQuizQ1Answer(Action):
    def name(self) -> Text:
        return "action_ask_quiz_q1_answer"

    def run(self, dispatcher, tracker, domain):
        role = tracker.get_slot("quiz_role") or ""
        qs = QUIZ_QUESTIONS.get(role, [])
        if qs:
            dispatcher.utter_message(text=qs[0]["text"])
        return []


class ActionAskQuizQ2Answer(Action):
    def name(self) -> Text:
        return "action_ask_quiz_q2_answer"

    def run(self, dispatcher, tracker, domain):
        role = tracker.get_slot("quiz_role") or ""
        qs = QUIZ_QUESTIONS.get(role, [])
        if len(qs) > 1:
            dispatcher.utter_message(text=qs[1]["text"])
        return []


class ActionAskQuizQ3Answer(Action):
    def name(self) -> Text:
        return "action_ask_quiz_q3_answer"

    def run(self, dispatcher, tracker, domain):
        role = tracker.get_slot("quiz_role") or ""
        qs = QUIZ_QUESTIONS.get(role, [])
        if len(qs) > 2:
            dispatcher.utter_message(text=qs[2]["text"])
        return []


class ActionAskQuizQ4Answer(Action):
    def name(self) -> Text:
        return "action_ask_quiz_q4_answer"

    def run(self, dispatcher, tracker, domain):
        role = tracker.get_slot("quiz_role") or ""
        qs = QUIZ_QUESTIONS.get(role, [])
        if len(qs) > 3:
            dispatcher.utter_message(text=qs[3]["text"])
        return []


class ActionAskQuizQ5Answer(Action):
    def name(self) -> Text:
        return "action_ask_quiz_q5_answer"

    def run(self, dispatcher, tracker, domain):
        role = tracker.get_slot("quiz_role") or ""
        qs = QUIZ_QUESTIONS.get(role, [])
        if len(qs) > 4:
            dispatcher.utter_message(text=qs[4]["text"])
        return []


class ValidateQuizForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_quiz_form"

    def validate_quiz_q1_answer(self, slot_value, dispatcher, tracker, domain):
        if slot_value is None:
            return {}
        choice = extract_answer_choice(str(slot_value))
        return {"quiz_q1_answer": choice}

    def validate_quiz_q2_answer(self, slot_value, dispatcher, tracker, domain):
        if slot_value is None:
            return {}
        choice = extract_answer_choice(str(slot_value))
        return {"quiz_q2_answer": choice}

    def validate_quiz_q3_answer(self, slot_value, dispatcher, tracker, domain):
        if slot_value is None:
            return {}
        choice = extract_answer_choice(str(slot_value))
        return {"quiz_q3_answer": choice}

    def validate_quiz_q4_answer(self, slot_value, dispatcher, tracker, domain):
        if slot_value is None:
            return {}
        choice = extract_answer_choice(str(slot_value))
        return {"quiz_q4_answer": choice}

    def validate_quiz_q5_answer(self, slot_value, dispatcher, tracker, domain):
        if slot_value is None:
            return {}
        choice = extract_answer_choice(str(slot_value))
        return {"quiz_q5_answer": choice}


class ActionEvaluateQuiz(Action):
    """Подсчитывает правильные ответы и выносит вердикт по порогам из config.yml."""

    def name(self) -> Text:
        return "action_evaluate_quiz"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        role = tracker.get_slot("quiz_role") or ""
        answers = [tracker.get_slot(f"quiz_q{i}_answer") or "" for i in range(1, 6)]
        questions = QUIZ_QUESTIONS.get(role, [])

        correct_count = 0
        result_lines: List[str] = []
        for i, (ans, q) in enumerate(zip(answers, questions)):
            is_correct = (ans.strip() == q["correct"])
            if is_correct:
                correct_count += 1
            marker = "[+]" if is_correct else "[-]"
            result_lines.append(
                f"{marker} Вопрос {i + 1}: ваш ответ — {ans or '?'}, правильный — {q['correct']}"
            )

        total = len(questions)
        role_title = ROLE_TITLES_RU.get(role, role)
        quiz_passed = correct_count >= MIN_CORRECT_ANSWERS

        years_text = tracker.get_slot("years_experience") or ""
        years = _parse_years(years_text)
        exp_passed = (MIN_YEARS_EXPERIENCE <= 0) or (years >= MIN_YEARS_EXPERIENCE)

        passed = quiz_passed and exp_passed
        restart_hint = "\n\nПовторить скрининг: «хочу пройти интервью»."

        if passed:
            msg = (
                f"Итог: вы ПРОШЛИ скрининг на роль «{role_title}»!\n"
                f"Правильных ответов: {correct_count}/{total}\n\n"
                + "\n".join(result_lines)
                + "\n\nРекрутёр свяжется с вами для следующего этапа. Оценка ориентировочная."
                + restart_hint
            )
        else:
            reasons: List[str] = []
            if not quiz_passed:
                reasons.append(f"правильных ответов {correct_count}/{total} (нужно минимум {MIN_CORRECT_ANSWERS})")
            if not exp_passed:
                reasons.append(
                    f"опыт {_format_years_ru(years)} (нужно минимум {_format_years_ru(MIN_YEARS_EXPERIENCE)})"
                )
            msg = (
                f"Итог: вы НЕ ПРОШЛИ порог скрининга на роль «{role_title}».\n"
                f"Причина: {'; '.join(reasons)}.\n\n"
                + "\n".join(result_lines)
                + "\n\nПопробуйте ещё раз или подготовьтесь по материалам роли."
                + restart_hint
            )

        dispatcher.utter_message(text=msg)

        return [
            SlotSet("quiz_role", None),
            SlotSet("quiz_q1_answer", None),
            SlotSet("quiz_q2_answer", None),
            SlotSet("quiz_q3_answer", None),
            SlotSet("quiz_q4_answer", None),
            SlotSet("quiz_q5_answer", None),
            SlotSet("intro_done", False),
        ]
