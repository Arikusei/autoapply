"""Тесты провайдера Habr Career на заглушках вместо сети."""

from __future__ import annotations

import json
import unittest

from autoapply.config import CONFIG_PATH, PROFILE_PATH, SearchSpec, load_config
from autoapply.models import Status
from autoapply.providers.habr_provider import (
    HabrApiError,
    HabrProvider,
    _flatten_params,
)

# Форма ответа /api/frontend/vacancies, снятая с живого API.
LIST_ITEM = {
    "id": 1000168585,
    "href": "/vacancies/1000168585",
    "title": "Python-разработчик",
    "remoteWork": True,
    "employment": "full_time",
    "qualification": "Middle",
    "company": {"title": "IBS", "alias_name": "ibs"},
    "salary": {"from": 150000, "to": 250000, "currency": "rur"},
    "skills": [{"title": "Python"}, {"title": "PostgreSQL"}],
    "locations": [{"title": "Москва"}],
    "divisions": [{"title": "Бэкенд разработка"}],
    "publishedDate": {"date": "2026-09-01T10:00:00+03:00"},
}

JOB_POSTING_PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Python-разработчик (автоматизация)",
 "description":"Навыки: Python, n8n. Квалификация: Junior. <p>Ищем разработчика на Python.</p><ul><li>REST API</li><li>PostgreSQL</li></ul>",
 "hiringOrganization":{"@type":"Organization","name":"IBS"},
 "employmentType":"FULL_TIME","jobLocationType":"TELECOMMUTE",
 "datePosted":"2026-09-01"}
</script></head><body></body></html>"""


def spec(**params) -> SearchSpec:
    return SearchSpec(name="test", provider="habr", params=params, max_pages=1)


class StubClient:
    def __init__(self, pages: int = 1, page_html: str = JOB_POSTING_PAGE):
        self.search_calls: list[list[tuple[str, str]]] = []
        self.pages = pages
        self.page_html = page_html
        self.responded: list[tuple[str, str]] = []
        self.error: HabrApiError | None = None

    def search(self, params):
        self.search_calls.append(params)
        return {
            "list": [LIST_ITEM],
            "meta": {"totalPages": self.pages, "currentPage": 1},
        }

    def vacancy_page(self, vacancy_id):
        return self.page_html

    def csrf_token(self):
        return "csrf-token"

    def respond(self, vacancy_id, letter, token):
        if self.error:
            raise self.error
        self.responded.append((vacancy_id, letter))
        return {"id": "resp-1"}


class HabrTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH, PROFILE_PATH)

    def provider(self, client=None, **settings) -> HabrProvider:
        provider = HabrProvider(self.config)
        provider.client = client or StubClient()
        for key, value in settings.items():
            setattr(provider, key, value)
        return provider


class TestParamFlattening(unittest.TestCase):
    """Многозначные фильтры Habr работают только в форме name[]=a&name[]=b."""

    def test_multi_value_uses_bracket_syntax(self):
        flat = _flatten_params({"qid": [3, 4]})
        self.assertEqual(flat, [("qid[]", "3"), ("qid[]", "4")])

    def test_single_value_without_brackets(self):
        self.assertEqual(_flatten_params({"q": "python"}), [("q", "python")])

    def test_seniority_words_translated_to_ids(self):
        # В config.yaml уровни удобнее писать словами.
        flat = _flatten_params({"qid": ["intern", "junior", "middle"]})
        self.assertEqual([v for _, v in flat], ["1", "3", "4"])

    def test_booleans_lowercased(self):
        self.assertEqual(_flatten_params({"remote": True}), [("remote", "true")])
        self.assertEqual(_flatten_params({"remote": False}), [("remote", "false")])

    def test_none_dropped(self):
        self.assertEqual(_flatten_params({"q": None}), [])

    def test_skills_and_locations_use_brackets(self):
        flat = _flatten_params({"skills": [446], "locations": ["c_736"]})
        self.assertEqual(
            flat, [("skills[]", "446"), ("locations[]", "c_736")]
        )


class TestSearch(HabrTestCase):
    def test_defaults_and_pagination(self):
        client = StubClient()
        list(self.provider(client).search(spec(q="python")))
        sent = dict(client.search_calls[0])
        self.assertEqual(sent["q"], "python")
        self.assertEqual(sent["per_page"], "50")
        self.assertEqual(sent["sort"], "date")

    def test_pagination_stops_at_last_page(self):
        client = StubClient(pages=3)
        prov = self.provider(client)
        list(prov.search(SearchSpec(name="t", provider="habr", params={}, max_pages=10)))
        self.assertEqual([dict(c)["page"] for c in client.search_calls], ["1", "2", "3"])

    def test_config_params_are_recognised(self):
        for search in self.config.searches_for("habr"):
            with self.subTest(search=search.name):
                self.assertEqual(self.provider().unknown_params(search), [])


class TestVacancyMapping(HabrTestCase):
    def test_list_item_mapped(self):
        vacancy = self.provider()._to_vacancy(LIST_ITEM)
        self.assertEqual(vacancy.provider, "habr")
        self.assertEqual(vacancy.external_id, "1000168585")
        self.assertEqual(vacancy.company, "IBS")
        self.assertEqual(vacancy.url, "https://career.habr.com/vacancies/1000168585")
        self.assertEqual(vacancy.seniority, "middle")
        self.assertEqual(vacancy.key_skills, ["Python", "PostgreSQL"])
        self.assertEqual(vacancy.salary_currency, "RUR")
        self.assertTrue(vacancy.remote)

    def test_qualification_levels_mapped(self):
        prov = self.provider()
        for raw, expected in {
            "Intern": "intern",
            "Junior": "junior",
            "Middle": "middle",
            "Senior": "senior",
            "Lead": "lead",
        }.items():
            with self.subTest(qualification=raw):
                item = {**LIST_ITEM, "qualification": raw}
                self.assertEqual(prov._to_vacancy(item).seniority, expected)

    def test_missing_fields_do_not_crash(self):
        vacancy = self.provider()._to_vacancy({"id": 5})
        self.assertEqual(vacancy.company, "")
        self.assertEqual(vacancy.url, "https://career.habr.com/vacancies/5")
        self.assertEqual(vacancy.salary_text, "—")
        self.assertIsNone(vacancy.seniority)

    def test_details_taken_from_json_ld(self):
        prov = self.provider()
        vacancy = prov.fetch_details(prov._to_vacancy({**LIST_ITEM, "qualification": None}))
        self.assertIn("Ищем разработчика на Python", vacancy.description)
        self.assertNotIn("<", vacancy.description)
        self.assertEqual(vacancy.title, "Python-разработчик (автоматизация)")
        self.assertTrue(vacancy.remote)
        # Уровень Habr дублирует в начале описания — оттуда и берём,
        # если в списке его не было.
        self.assertEqual(vacancy.seniority, "junior")

    def test_list_skills_win_over_description(self):
        # Навыки из выдачи структурированы, а из описания — разобраны эвристикой.
        prov = self.provider()
        vacancy = prov.fetch_details(prov._to_vacancy(LIST_ITEM))
        self.assertEqual(vacancy.key_skills, ["Python", "PostgreSQL"])

    def test_skills_parsed_from_description_when_list_empty(self):
        prov = self.provider()
        vacancy = prov.fetch_details(prov._to_vacancy({**LIST_ITEM, "skills": []}))
        self.assertEqual(vacancy.key_skills, ["Python", "n8n"])

    def test_details_survive_page_without_json_ld(self):
        prov = self.provider(StubClient(page_html="<html>без разметки</html>"))
        original = prov._to_vacancy(LIST_ITEM)
        vacancy = prov.fetch_details(original)
        self.assertEqual(vacancy.title, "Python-разработчик")

    def test_details_survive_http_error(self):
        client = StubClient()

        def boom(vacancy_id):
            raise HabrApiError(500, "server error")

        client.vacancy_page = boom
        prov = self.provider(client)
        vacancy = prov.fetch_details(prov._to_vacancy(LIST_ITEM))
        self.assertEqual(vacancy.title, "Python-разработчик")


class TestApply(HabrTestCase):
    def test_manual_when_apply_disabled(self):
        # По умолчанию автоотклик выключен: письмо готово, отклик руками.
        prov = self.provider(apply_enabled=False)
        result = prov.apply(prov._to_vacancy(LIST_ITEM), "письмо")
        self.assertIs(result.status, Status.MANUAL)
        self.assertIn("career.habr.com", result.detail)

    def test_manual_when_cookies_missing(self):
        prov = self.provider(apply_enabled=True)
        prov._cookies = {}
        result = prov.apply(prov._to_vacancy(LIST_ITEM), "письмо")
        self.assertIs(result.status, Status.MANUAL)

    def test_sends_when_enabled_and_authorized(self):
        client = StubClient()
        prov = self.provider(client, apply_enabled=True)
        prov._cookies = {"remember_user_token": "token"}
        result = prov.apply(prov._to_vacancy(LIST_ITEM), "текст письма")
        self.assertIs(result.status, Status.SENT)
        self.assertEqual(client.responded, [("1000168585", "текст письма")])
        self.assertEqual(result.negotiation_id, "resp-1")

    def test_can_apply_requires_both_flag_and_cookie(self):
        prov = self.provider(apply_enabled=True)
        prov._cookies = {}
        self.assertFalse(prov.can_apply)
        prov._cookies = {"remember_user_token": "t"}
        self.assertTrue(prov.can_apply)
        prov.apply_enabled = False
        self.assertFalse(prov.can_apply)

    def test_hint_explains_what_to_do(self):
        self.assertIn("apply_enabled", self.provider(apply_enabled=False).authorization_hint())


class TestErrorClassification(HabrTestCase):
    def classify(self, error: HabrApiError):
        return self.provider()._classify_error(error)

    def test_captcha_stops_the_run(self):
        # Капча значит, что бота распознали: продолжать бессмысленно.
        result = self.classify(
            HabrApiError(403, {"message": "Необходимо заполнить капчу",
                               "error": {"type": "captcha"}})
        )
        self.assertTrue(result.limit_reached)
        self.assertIs(result.status, Status.QUEUED)

    def test_expired_cookies_stop_the_run(self):
        result = self.classify(HabrApiError(401, {"message": "unauthorized"}))
        self.assertTrue(result.limit_reached)
        self.assertIn("cookie", result.detail)

    def test_already_applied(self):
        result = self.classify(HabrApiError(422, {"message": "Вы уже откликнулись"}))
        self.assertIs(result.status, Status.ALREADY)

    def test_missing_vacancy_unavailable(self):
        self.assertIs(self.classify(HabrApiError(404, None)).status, Status.UNAVAILABLE)

    def test_unknown_is_retryable(self):
        self.assertIs(self.classify(HabrApiError(500, "oops")).status, Status.FAILED)


if __name__ == "__main__":
    unittest.main()
