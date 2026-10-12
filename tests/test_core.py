"""Тесты ядра: конфигурация, поиск слов, скоринг, письма, хранилище.

Запуск: python -m unittest discover -s tests -t .
Сеть не используется.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from autoapply.config import CONFIG_PATH, PROFILE_PATH, ConfigError, load_config
from autoapply.letters import LetterWriter, _clean, _profile_summary
from autoapply.matching import find_substrings, find_words
from autoapply.models import Score, Status, Vacancy, lowest_seniority
from autoapply.providers.parsing import extract_json_ld, html_to_text, parse_datetime
from autoapply.scoring import hard_filter, score_vacancy
from autoapply.storage import Storage


def make_vacancy(**overrides) -> Vacancy:
    defaults = dict(
        provider="hh",
        external_id="1",
        title="Python-разработчик",
        company="ООО Пример",
        url="https://hh.ru/vacancy/1",
        description="Нужен Python, SQL, работа с REST API и автоматизация процессов.",
        key_skills=["Python", "PostgreSQL"],
        area_name="Томск",
        seniority="middle",
        remote=True,
        salary_from=80000,
        salary_currency="RUR",
    )
    defaults.update(overrides)
    return Vacancy(**defaults)


class TestConfig(unittest.TestCase):
    """Реальные config.yaml и profile.yaml должны загружаться и быть связными."""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH, PROFILE_PATH)

    def test_providers_enabled(self):
        self.assertIn("hh", self.config.providers)

    def test_every_provider_has_searches(self):
        for name in self.config.providers:
            with self.subTest(provider=name):
                self.assertTrue(
                    self.config.searches_for(name),
                    f"у площадки {name} нет ни одного запроса",
                )

    def test_search_params_are_provider_native(self):
        for spec in self.config.searches:
            self.assertTrue(spec.name)
            self.assertIsInstance(spec.params, dict)
            self.assertIn(spec.provider, self.config.providers)

    def test_search_params_are_known_to_provider(self):
        # Опечатка в имени фильтра не вызывает ошибку у API — она молча меняет
        # выдачу. Проверяем, что в конфиге нет незнакомых ключей.
        from autoapply.providers import get_provider

        for spec in self.config.searches:
            with self.subTest(search=spec.name):
                provider = get_provider(self.config, spec.provider)
                self.assertEqual(provider.unknown_params(spec), [])

    def test_scoring_terms_are_lowercase(self):
        # Скоринг сравнивает с текстом в нижнем регистре.
        for group in (
            self.config.scoring.weights,
            self.config.scoring.blocklist,
            self.config.scoring.reject_title_patterns,
            self.config.scoring.allow_seniority,
        ):
            for term in group:
                self.assertEqual(term, term.lower())

    def test_allow_seniority_uses_common_scale(self):
        from autoapply.models import SENIORITY_ORDER

        for level in self.config.scoring.allow_seniority:
            self.assertIn(level, SENIORITY_ORDER)

    def test_profile_has_content_for_prompt(self):
        summary = _profile_summary(self.config.profile)
        self.assertIn("Python", summary)
        self.assertIn("never_claim", summary)

    def test_missing_providers_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("scoring: {}\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path, PROFILE_PATH)
            self.assertIn("площадка", str(ctx.exception))

    def test_provider_without_searches_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("providers: [hh]\nhh: {}\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path, PROFILE_PATH)


class TestMatching(unittest.TestCase):
    """Стоп-слова ищутся по границам слова, основы — по подстроке."""

    def test_word_match_finds_term_in_brackets(self):
        # Ровно этот случай ломал отбор: «Java» в «FullStack QA (Java)».
        self.assertEqual(find_words("fullstack qa (java)", ["java"]), ["java"])

    def test_word_match_does_not_touch_longer_word(self):
        self.assertEqual(find_words("знание javascript и react", ["java"]), [])

    def test_word_match_handles_plus_and_hash(self):
        self.assertEqual(find_words("опыт c++ обязателен", ["c++"]), ["c++"])
        self.assertEqual(find_words("разработка на c#", ["c#"]), ["c#"])

    def test_word_match_handles_cyrillic(self):
        self.assertEqual(find_words("разработчик 1с", ["1с"]), ["1с"])
        self.assertEqual(find_words("1с:предприятие", ["1с"]), ["1с"])

    def test_word_match_is_case_insensitive(self):
        self.assertEqual(find_words("Senior Developer", ["senior"]), ["senior"])

    def test_substring_match_keeps_stems_working(self):
        found = find_substrings("автоматизации процессов и работа с данными", 
                                ["автоматизац", "данн"])
        self.assertEqual(sorted(found), ["автоматизац", "данн"])

    def test_empty_inputs(self):
        self.assertEqual(find_words("", ["java"]), [])
        self.assertEqual(find_words("java", []), [])
        self.assertEqual(find_substrings("", ["x"]), [])


class TestScoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_config(CONFIG_PATH, PROFILE_PATH).scoring

    def test_good_match_accepted(self):
        score = score_vacancy(make_vacancy(), self.spec)
        self.assertTrue(score.accepted, score.reasons)
        self.assertGreaterEqual(score.value, self.spec.threshold)
        self.assertIn("python", score.matched)

    def test_java_role_rejected_by_title(self):
        score = score_vacancy(make_vacancy(title="FullStack QA (Java)"), self.spec)
        self.assertFalse(score.accepted)
        self.assertIn("java", score.reasons[0])

    def test_javascript_in_description_does_not_reject(self):
        # JavaScript в описании — не повод отклонять python-вакансию.
        score = score_vacancy(
            make_vacancy(description="Python, SQL, немного JavaScript на фронте"),
            self.spec,
        )
        self.assertTrue(score.accepted, score.reasons)

    def test_senior_title_rejected(self):
        score = score_vacancy(make_vacancy(title="Senior Python Developer"), self.spec)
        self.assertFalse(score.accepted)
        self.assertIn("уровень не подходит", score.reasons[0])

    def test_seniority_above_allowed_rejected(self):
        score = score_vacancy(make_vacancy(seniority="senior"), self.spec)
        self.assertFalse(score.accepted)
        self.assertIn("уровень", score.reasons[0])

    def test_unknown_seniority_does_not_reject(self):
        # Если площадка не сообщила уровень, вакансию решает балл, а не догадки.
        score = score_vacancy(make_vacancy(seniority=None), self.spec)
        self.assertTrue(score.accepted, score.reasons)

    def test_vacancy_with_test_rejected(self):
        score = score_vacancy(make_vacancy(has_test=True), self.spec)
        self.assertFalse(score.accepted)
        self.assertIn("тест", score.reasons[0])

    def test_irrelevant_vacancy_rejected(self):
        score = score_vacancy(
            make_vacancy(
                title="Бариста",
                description="Приготовление напитков, работа за стойкой.",
                key_skills=[],
            ),
            self.spec,
        )
        self.assertFalse(score.accepted)

    def test_bonuses_applied(self):
        remote_home = score_vacancy(make_vacancy(), self.spec).value
        office_far = score_vacancy(
            make_vacancy(remote=False, area_name="Москва"), self.spec
        ).value
        expected = self.spec.bonuses["remote"] + self.spec.bonuses["area_match"]
        self.assertEqual(remote_home - office_far, expected)

    def test_home_area_matched_by_name(self):
        # У площадок разные id регионов, поэтому сравнение идёт по названию.
        for area in ("Томск", "Северск (Томская область)", "томская область"):
            with self.subTest(area=area):
                score = score_vacancy(make_vacancy(area_name=area), self.spec)
                self.assertIn(
                    "домашний регион",
                    " ".join(score.reasons),
                    f"регион '{area}' не распознан как домашний",
                )

    def test_hard_filter_agrees_with_score(self):
        rejected = make_vacancy(title="Тимлид разработки")
        self.assertIsNotNone(hard_filter(rejected, self.spec))
        self.assertFalse(score_vacancy(rejected, self.spec).accepted)
        self.assertIsNone(hard_filter(make_vacancy(), self.spec))


class TestSeniority(unittest.TestCase):
    def test_lowest_of_several_levels(self):
        # Вакансия, открытая и для junior, и для senior, доступна junior-кандидату.
        self.assertEqual(lowest_seniority(["middle", "senior", "junior"]), "junior")

    def test_single_level(self):
        self.assertEqual(lowest_seniority(["senior"]), "senior")

    def test_unknown_levels_ignored(self):
        self.assertIsNone(lowest_seniority(["c_level_unknown", "странное"]))
        self.assertEqual(lowest_seniority(["чужое", "middle"]), "middle")

    def test_empty(self):
        self.assertIsNone(lowest_seniority([]))


class TestLetters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH, PROFILE_PATH)

    def test_template_renders_with_vacancy_fields(self):
        writer = LetterWriter(self.config)
        letter = writer.render_template(make_vacancy(title="Инженер данных"))
        self.assertIn("Инженер данных", letter)
        self.assertIn("ООО Пример", letter)
        self.assertLessEqual(len(letter), self.config.letters.max_chars)

    def test_falls_back_to_template_without_api_key(self):
        config = load_config(CONFIG_PATH, PROFILE_PATH)
        object.__setattr__(config.secrets, "openai_api_key", None)
        writer = LetterWriter(config)
        letter, source = writer.write(make_vacancy())
        self.assertEqual(source, "template")
        self.assertTrue(letter.strip())
        self.assertIsNotNone(writer.llm_disabled_reason)

    def test_clean_strips_markdown_and_trims_on_boundary(self):
        self.assertNotIn("*", _clean("**Здравствуйте!** Текст письма.", 500))
        trimmed = _clean("Первое предложение. " * 50, 100)
        self.assertLessEqual(len(trimmed), 101)
        self.assertTrue(trimmed.endswith((".", "…")))


class TestParsing(unittest.TestCase):
    def test_block_tags_become_line_breaks(self):
        text = html_to_text("<p>Обязанности:</p><ul><li>Python</li><li>SQL</li></ul>")
        self.assertIn("Обязанности:", text)
        self.assertNotIn("<", text)
        self.assertIn("\n", text)

    def test_entities_unescaped(self):
        self.assertEqual(html_to_text("<p>Python &amp; SQL</p>"), "Python & SQL")

    def test_empty_input(self):
        self.assertEqual(html_to_text(None), "")
        self.assertEqual(html_to_text(""), "")

    def test_datetime_parsing_tolerates_garbage(self):
        self.assertIsNotNone(parse_datetime("2026-09-01T10:00:00+03:00"))
        self.assertIsNone(parse_datetime("не дата"))
        self.assertIsNone(parse_datetime(None))

    def test_json_ld_extraction(self):
        page = (
            '<html><script type="application/ld+json">'
            '{"@type": "WebSite", "name": "x"}</script>'
            '<script type="application/ld+json">'
            '{"@type": "JobPosting", "title": "Разработчик"}</script></html>'
        )
        posting = extract_json_ld(page, "JobPosting")
        self.assertIsNotNone(posting)
        self.assertEqual(posting["title"], "Разработчик")

    def test_json_ld_missing_or_broken(self):
        self.assertIsNone(extract_json_ld("<html></html>", "JobPosting"))
        broken = '<script type="application/ld+json">{not json}</script>'
        self.assertIsNone(extract_json_ld(broken, "JobPosting"))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "state.sqlite3")
        self.score = Score(value=10, accepted=True, reasons=["ок"], matched=["python"])

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_queue_roundtrip_preserves_letter(self):
        self.storage.save(make_vacancy(), self.score, Status.QUEUED, "Текст письма", "llm")
        queue = self.storage.queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].letter, "Текст письма")
        self.assertEqual(queue[0].letter_source, "llm")

    def test_save_is_idempotent_by_key(self):
        for _ in range(3):
            self.storage.save(make_vacancy(), self.score, Status.QUEUED, "письмо")
        self.assertEqual(len(self.storage.queue()), 1)

    def test_same_id_on_different_providers_is_not_a_duplicate(self):
        # Ключ в БД включает площадку, иначе вакансии с одинаковыми id
        # затирали бы друг друга.
        self.storage.save(
            make_vacancy(provider="hh", external_id="7"), self.score, Status.QUEUED, "x"
        )
        self.storage.save(
            make_vacancy(provider="habr", external_id="7"), self.score, Status.QUEUED, "x"
        )
        self.assertEqual(len(self.storage.queue()), 2)

    def test_queue_filtered_by_provider(self):
        self.storage.save(
            make_vacancy(provider="hh", external_id="1"), self.score, Status.QUEUED, "x"
        )
        self.storage.save(
            make_vacancy(provider="getmatch", external_id="2"),
            self.score,
            Status.QUEUED,
            "x",
        )
        self.assertEqual(len(self.storage.queue(providers=["hh"])), 1)
        self.assertEqual(len(self.storage.queue(providers=["hh", "getmatch"])), 2)
        self.assertEqual(self.storage.queue(providers=[]), [])

    def test_settled_statuses_block_reprocessing(self):
        for status in (
            Status.SENT,
            Status.ALREADY,
            Status.UNAVAILABLE,
            Status.SKIPPED,
            Status.MANUAL,
        ):
            with self.subTest(status=status):
                self.storage.save(
                    make_vacancy(external_id=status.value), self.score, status
                )
                self.assertTrue(self.storage.is_settled(f"hh:{status.value}"))

    def test_failed_is_not_settled_and_can_be_requeued(self):
        self.storage.save(make_vacancy(), self.score, Status.FAILED, "письмо")
        self.assertFalse(self.storage.is_settled("hh:1"))
        self.assertEqual(self.storage.requeue_failed(), 1)
        self.assertEqual(len(self.storage.queue()), 1)

    def test_unseen_vacancy_is_not_settled(self):
        self.assertFalse(self.storage.is_settled("hh:does-not-exist"))

    def test_sent_today_counts_only_sent(self):
        self.storage.save(make_vacancy(external_id="a"), self.score, Status.QUEUED, "x")
        self.storage.save(make_vacancy(external_id="b"), self.score, Status.QUEUED, "x")
        self.assertEqual(self.storage.sent_today(), 0)

        self.storage.mark("hh:a", Status.SENT, "ok", negotiation_id="555")
        self.assertEqual(self.storage.sent_today(), 1)
        self.assertEqual([i.external_id for i in self.storage.queue()], ["b"])

    def test_mark_sent_records_timestamp(self):
        self.storage.save(make_vacancy(), self.score, Status.QUEUED, "x")
        self.storage.mark("hh:1", Status.SENT, "ok")
        recent = self.storage.recent_sent()
        self.assertEqual(len(recent), 1)
        self.assertTrue((recent[0].detail or "").startswith(date.today().isoformat()))

    def test_queue_ordered_by_score_desc(self):
        self.storage.save(
            make_vacancy(external_id="low"), Score(3, True), Status.QUEUED, "x"
        )
        self.storage.save(
            make_vacancy(external_id="high"), Score(20, True), Status.QUEUED, "x"
        )
        self.assertEqual([i.external_id for i in self.storage.queue()], ["high", "low"])

    def test_counts_by_provider(self):
        self.storage.save(
            make_vacancy(provider="hh", external_id="a"), self.score, Status.QUEUED, "x"
        )
        self.storage.save(
            make_vacancy(provider="habr", external_id="b"), self.score, Status.MANUAL
        )
        counts = self.storage.counts_by_provider()
        self.assertEqual(counts.get(("hh", "queued")), 1)
        self.assertEqual(counts.get(("habr", "manual")), 1)


class TestVacancyModel(unittest.TestCase):
    def test_salary_formats(self):
        self.assertEqual(make_vacancy(salary_from=None, salary_to=None).salary_text, "—")
        self.assertIn("от", make_vacancy(salary_from=100000).salary_text)
        self.assertIn("–", make_vacancy(salary_from=100000, salary_to=200000).salary_text)

    def test_searchable_text_is_lowercase_and_includes_skills(self):
        text = make_vacancy().searchable_text()
        self.assertEqual(text, text.lower())
        self.assertIn("postgresql", text)

    def test_key_is_namespaced_by_provider(self):
        self.assertEqual(make_vacancy(provider="habr", external_id="42").key, "habr:42")


if __name__ == "__main__":
    unittest.main()
