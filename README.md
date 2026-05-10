# HR-бот для скрининга кандидатов в ML-команду (Rasa 3.x)

Учебный проект по заданию 1: первичное интервью по пяти ролям (Project Manager, Data Analyst, Data Engineer, Data Scientist, MLOps Engineer), форма сбора слотов и **custom action** с итоговым заключением «подходит / не подходит».

Репозиторий на GitHub: [esheshka/rasa_hr_project](https://github.com/esheshka/rasa_hr_project). После `git clone` все команды ниже выполняйте из корня клона (каталог `rasa_hr_project`).

**Архитектура ответа «один user-turn → одна реплика бота»:** в `config.yml` отключены **TEDPolicy** и **MemoizationPolicy** — остаётся **RulePolicy** (и форма), чтобы не было конкурирующих предсказателей за один вход и чтобы не «терялся» `active_loop` из‑за усечённой story в Memoization. Длинное приветствие собрано в **один** шаблон `utter_greet_full` и один вызов `dispatcher.utter_message` в `action_greet_user`. Пустой ответ после формы по-прежнему означает, что не запущен `rasa run actions`.

## Окружение

Рекомендуется Python **3.10** (как в материалах курса). В каталоге проекта уже может существовать виртуальное окружение `.venv`:

```bash
cd rasa_hr_project   # или другой каталог, если клонировали под другим именем
python3.10 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

## Запуск

В **двух** терминалах с активированным `.venv`:

```bash
cd rasa_hr_project
source .venv/bin/activate
rasa run actions
```

```bash
cd rasa_hr_project
source .venv/bin/activate
rasa train
rasa shell
```

Полезные команды:

```bash
rasa data validate
rasa shell nlu
```

## Автоматическая проверка (REST)

После `rasa train` поднимите сервер и action server, затем в третьем терминале:

```bash
cd rasa_hr_project
source .venv/bin/activate
rasa run actions --port 5055   # терминал 1
rasa run --enable-api --cors "*" -p 5006   # терминал 2
python scripts/e2e_dialog.py   # терминал 3, код выхода 0 = все сценарии OK
```

Скрипт проверяет в том числе: привет + `hi`, fallback, список ролей, полный happy-path, **ветка «не знаю роль» с автоподбором по навыкам**, отказ по слабым навыкам, прощание, OOS, **прерывание формы** при анекдоте, длинное сообщение.

## Сценарий диалога

1. Пользователь здоровается или спрашивает роли (`ask_roles`).
2. Запуск интервью: фразы вроде «хочу пройти интервью» (`start_interview`) — активируется форма `interview_form`.
3. Бот последовательно собирает слоты: целевая роль, стаж, навыки, ожидания.
4. После заполнения вызывается `action_evaluate_candidate`: эвристика по стажу и ключевым словам для выбранной роли (или **автоподбор одной роли по навыкам**, если на шаге роли указано «не знаю» / «любая роль» и по тексту навыков получается **однозначный** лидер среди пяти профилей).

Во время формы фразы **вне области** или **прощание** снимают форму и дают соответствующий ответ (см. `action_abort_interview_*` в `actions/actions.py`).

После завершения скрининга (итог или прерывание) слот **`intro_done`** сбрасывается — следующее «Здравствуйте» снова покажет полное приветствие.

Для сброса состояния в `rasa shell`: `/restart`.

Если после последнего ответа формы **пустой ответ** в UI — почти всегда не запущен `rasa run actions` или недоступен порт 5055 из `endpoints.yml`.

## Структура

| Файл | Назначение |
|------|------------|
| `domain.yml` | Интенты, сущность `job_role`, слоты, форма, ответы |
| `data/nlu.yml` | Примеры фраз, lookup и синонимы ролей |
| `data/rules.yml` | Приветствие, список ролей, старт и завершение формы |
| `data/stories.yml` | Минимальный пример для `FormValidationAction` (обучение без TED/Memoization — см. `config.yml`) |
| `actions/actions.py` | Итог интервью и прерывание формы |
| `scripts/e2e_dialog.py` | Автопрогон краевых сценариев через REST |
