"""Тесты провайдера hh.ru на заглушках вместо сети."""

from __future__ import annotations

import unittest

from autoapply.config import CONFIG_PATH, PROFILE_PATH, SearchSpec, load_config
from autoapply.models import Status
from autoapply.providers.hh_client import HHApiError, HHClient
from autoapply.providers.hh_provider import HHProvider

SEARCH_ITEM = {
    "id": "123456",
    "name": "Python-разработчик",
    "employer": {"name": "ООО Пример"},
    "alternate_url": "https://hh.ru/vacancy/123456",
    "area": {"id": 90, "name": "Томск"},
    "salary": {"from": 80000, "to": None, "currency": "RUR"},
    "experience": {"id": "between1And3", "name": "1–3 года"},
    "schedule": {"id": "remote", "name": "Удалённо"},
    "employment": {"id": "full", "name": "Полная занятость"},
    "published_at": "2026-09-01T10:00:00+0300",
    "snippet": {
        "requirement": "Знание <highlighttext>Python</highlighttext> и SQL",
        "responsibility": "Автоматизация процессов &amp; интеграции",
    },
    "has_test": False,
    "response_letter_required": True,
}


def spec(**params) -> SearchSpec:
    return SearchSpec(name="test", provider="hh", params=params, max_pages=1)


class StubClient:
    def __init__(self, pages: int = 1):
        self.calls: list[dict] = []
        self.pages = pages

    def search_vacancies(self, params: dict) -> dict:
        self.calls.append(params)
        return {"items": [SEARCH_ITEM], "pages": self.pages}


class HHTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH, PROFILE_PATH)

    def provider(self, client=None) -> HHProvider:
        provider = HHProvider(self.config)
        provider.client = client or StubClient()
        return provider


class TestSearchParams(HHTestCase):
    def test_params_forwarded_as_is(self):
        client = StubClient()
        prov = self.provider(client)
        list(
            prov.search(
                spec(
                    text="Python",
                    area=1255,
                    professional_role=[96, 10],
                    experience=["noExperience"],
                    schedule=["remote"],
                    period=7,
                )
            )
        )
        sent = client.calls[0]
        self.assertEqual(sent["text"], "Python")
        self.assertEqual(sent["area"], 1255)
        self.assertEqual(sent["professional_role"], [96, 10])
        self.assertEqual(sent["period"], 7)
        self.assertEqual(sent["per_page"], 50)
        self.assertEqual(sent["order_by"], "publication_time")

    def test_bool_only_with_salary_converted(self):
        # hh.ru ожидает строку "true", питоновский True он не понимает.
        client = StubClient()
        list(self.provider(client).search(spec(only_with_salary=True)))
        self.assertEqual(client.calls[0]["only_with_salary"], "true")

    def test_none_values_dropped(self):
        client = StubClient()
        list(self.provider(client).search(spec(text="Python", area=None)))
        self.assertNotIn("area", client.calls[0])

    def test_pagination_stops_at_last_page(self):
        client = StubClient(pages=3)
        prov = self.provider(client)
        list(prov.search(SearchSpec(name="t", provider="hh", params={}, max_pages=10)))
        self.assertEqual([c["page"] for c in client.calls], [0, 1, 2])

    def test_max_pages_caps_pagination(self):
        client = StubClient(pages=100)
        prov = self.provider(client)
        list(prov.search(SearchSpec(name="t", provider="hh", params={}, max_pages=2)))
        self.assertEqual(len(client.calls), 2)

    def test_config_params_are_recognised(self):
        for search in self.config.searches_for("hh"):
            with self.subTest(search=search.name):
                self.assertEqual(self.provider().unknown_params(search), [])


class TestVacancyMapping(HHTestCase):
    def test_search_item_mapped(self):
        vacancy = self.provider()._to_vacancy(SEARCH_ITEM)
        self.assertEqual(vacancy.provider, "hh")
        self.assertEqual(vacancy.external_id, "123456")
        self.assertEqual(vacancy.title, "Python-разработчик")
        self.assertEqual(vacancy.area_name, "Томск")
        self.assertEqual(vacancy.salary_from, 80000)
        self.assertTrue(vacancy.remote)
        self.assertTrue(vacancy.letter_required)
        self.assertIsNotNone(vacancy.published_at)

    def test_experience_mapped_to_common_seniority(self):
        cases = {
            "noExperience": "junior",
            "between1And3": "middle",
            "between3And6": "senior",
            "moreThan6": "lead",
        }
        prov = self.provider()
        for raw, expected in cases.items():
            with self.subTest(experience=raw):
                item = {**SEARCH_ITEM, "experience": {"id": raw}}
                self.assertEqual(prov._to_vacancy(item).seniority, expected)

    def test_snippet_html_stripped(self):
        vacancy = self.provider()._to_vacancy(SEARCH_ITEM)
        self.assertNotIn("<", vacancy.description)
        self.assertIn("Python", vacancy.description)
        self.assertIn("&", vacancy.description)

    def test_missing_fields_do_not_crash(self):
        vacancy = self.provider()._to_vacancy({"id": "1", "name": "Тест"})
        self.assertEqual(vacancy.company, "")
        self.assertEqual(vacancy.url, "https://hh.ru/vacancy/1")
        self.assertEqual(vacancy.salary_text, "—")
        self.assertIsNone(vacancy.seniority)


class TestApplyFormatNegotiation(unittest.TestCase):
    """Схема ручки отклика не опубликована, поэтому клиент подбирает формат."""

    def make_client(self, behaviour) -> HHClient:
        client = HHClient.__new__(HHClient)
        client._apply_format = None
        client.calls = []

        def fake_request(method, path, *, params=None, data=None, json_body=None, **kw):
            client.calls.append({"data": data, "json": json_body})
            return behaviour(data, json_body)

        client.request = fake_request
        return client

    def test_falls_back_to_json_when_form_rejected(self):
        def behaviour(data, json_body):
            if data is not None:
                raise HHApiError(400, {"errors": [{"value": "bad_argument"}]})
            return {"id": "777"}

        client = self.make_client(behaviour)
        self.assertEqual(client.apply("1", "abc", "письмо")["id"], "777")
        self.assertEqual(client._apply_format, "json")
        self.assertEqual(len(client.calls), 2)

    def test_successful_format_is_reused(self):
        client = self.make_client(lambda data, json_body: {"id": "1"})
        client.apply("1", "abc", "письмо")
        client.apply("2", "abc", "письмо")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client._apply_format, "form")

    def test_semantic_errors_not_retried_with_other_formats(self):
        def behaviour(data, json_body):
            raise HHApiError(403, {"errors": [{"value": "already_applied"}]})

        client = self.make_client(behaviour)
        with self.assertRaises(HHApiError) as ctx:
            client.apply("1", "abc", "письмо")
        self.assertTrue(ctx.exception.has("already_applied"))
        self.assertEqual(len(client.calls), 1)

    def test_limit_exceeded_not_retried(self):
        def behaviour(data, json_body):
            raise HHApiError(400, {"errors": [{"value": "limit_exceeded"}]})

        client = self.make_client(behaviour)
        with self.assertRaises(HHApiError):
            client.apply("1", "abc", "письмо")
        self.assertEqual(len(client.calls), 1)

    def test_letter_passed_in_payload(self):
        client = self.make_client(lambda data, json_body: {"id": "1"})
        client.apply("55", "res-1", "Здравствуйте!")
        payload = client.calls[0]["data"]
        self.assertEqual(payload["vacancy_id"], "55")
        self.assertEqual(payload["resume_id"], "res-1")
        self.assertEqual(payload["message"], "Здравствуйте!")


class TestErrorClassification(HHTestCase):
    def classify(self, value: str, status_code: int = 403):
        prov = self.provider()
        return prov._classify_error(
            HHApiError(status_code, {"errors": [{"value": value, "type": "negotiations"}]})
        )

    def test_already_applied_is_terminal(self):
        self.assertIs(self.classify("already_applied").status, Status.ALREADY)

    def test_limit_exceeded_stops_run_and_keeps_item_queued(self):
        result = self.classify("limit_exceeded", 400)
        self.assertTrue(result.limit_reached)
        self.assertIs(result.status, Status.QUEUED)

    def test_test_required_unavailable(self):
        self.assertIs(self.classify("test_required").status, Status.UNAVAILABLE)

    def test_archived_vacancy_unavailable(self):
        self.assertIs(self.classify("invalid_vacancy").status, Status.UNAVAILABLE)

    def test_bad_resume_is_retryable(self):
        self.assertIs(self.classify("resume_not_found").status, Status.FAILED)

    def test_unknown_error_is_retryable(self):
        self.assertIs(self.classify("something_new").status, Status.FAILED)


class TestErrorBodyParsing(unittest.TestCase):
    def test_values_and_types_collected(self):
        err = HHApiError(403, {"errors": [{"type": "negotiations", "value": "already_applied"}]})
        self.assertTrue(err.has("already_applied"))
        self.assertTrue(err.has("negotiations"))

    def test_oauth_style_body(self):
        err = HHApiError(403, {"error": "token_expired", "error_description": "expired"})
        self.assertTrue(err.has("token_expired"))

    def test_non_dict_body_is_safe(self):
        self.assertEqual(HHApiError(500, "Bad Gateway").error_types, [])
        self.assertEqual(HHApiError(500, None).error_types, [])


if __name__ == "__main__":
    unittest.main()
