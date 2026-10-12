"""Тесты провайдера getmatch на заглушках вместо сети."""

from __future__ import annotations

import unittest

from autoapply.config import CONFIG_PATH, PROFILE_PATH, SearchSpec, load_config
from autoapply.models import Status
from autoapply.providers.getmatch_provider import (
    GetmatchApiError,
    GetmatchProvider,
    _flatten_params,
)

# Форма ответа /api/offers, снятая с живого API.
OFFER = {
    "id": 36133,
    "offer_type": "vacancy",
    "is_active": True,
    "position": "Backend-разработчик (Django)",
    "company": {"name": "X5 Медиа"},
    "url": "/vacancies/36133-backend-razrabotchik",
    "salary_display_from": 250000,
    "salary_display_to": 450000,
    "salary_currency": "RUB",
    "salary_hidden": False,
    "seniority": "middle",
    "seniorities": ["middle", "senior"],
    "skills_objects": [
        {"name": "Python", "slug": "python"},
        {"name": "PostgreSQL", "slug": "postgresql"},
    ],
    "location_items": [{"label": "Москва", "format": "remote"}],
    "offer_description": "Короткий тизер вакансии.",
    "published_at": "2026-09-01T10:00:00+03:00",
    "cover_letter_required": True,
}

# Рекрутинговое событие — не вакансия, откликаться не на что.
ONE_DAY_OFFER = {**OFFER, "id": 29964, "offer_type": "one_day_offer_v3"}

DETAIL = {
    **OFFER,
    "description": "<p>Развиваем рекламную платформу.</p><ul><li>Python</li><li>REST API</li></ul>",
    "stack_description": "Стек: Django, PostgreSQL, Docker",
    "required_years_of_experience": 3,
}


def spec(**params) -> SearchSpec:
    return SearchSpec(name="test", provider="getmatch", params=params, max_pages=1)


class StubClient:
    def __init__(self, offers=None, total=None):
        self.offers_list = offers if offers is not None else [OFFER]
        self.total = total if total is not None else len(self.offers_list)
        self.calls: list[list[tuple[str, str]]] = []
        self.applied: list[tuple[str, str]] = []
        self.error: GetmatchApiError | None = None

    def offers(self, params):
        self.calls.append(params)
        return {"offers": self.offers_list, "meta": {"total": self.total}}

    def offer(self, offer_id):
        return DETAIL

    def apply(self, offer_id, letter):
        if self.error:
            raise self.error
        self.applied.append((offer_id, letter))
        return {"id": "app-1"}


class GetmatchTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(CONFIG_PATH, PROFILE_PATH)

    def provider(self, client=None, **settings) -> GetmatchProvider:
        provider = GetmatchProvider(self.config)
        provider.client = client or StubClient()
        for key, value in settings.items():
            setattr(provider, key, value)
        return provider


class TestParamFlattening(unittest.TestCase):
    def test_multi_value_repeats_key(self):
        self.assertEqual(
            _flatten_params({"se": ["junior", "middle"]}),
            [("se", "junior"), ("se", "middle")],
        )

    def test_scalar_and_none(self):
        self.assertEqual(_flatten_params({"l": "remote"}), [("l", "remote")])
        self.assertEqual(_flatten_params({"l": None}), [])


class TestSearch(GetmatchTestCase):
    def test_defaults_applied(self):
        client = StubClient()
        list(self.provider(client).search(spec(l="remote")))
        sent = dict(client.calls[0])
        self.assertEqual(sent["sa"], "any")
        self.assertEqual(sent["pa"], "all")
        self.assertEqual(sent["l"], "remote")

    def test_recruiting_events_filtered_out(self):
        # Выдача смешивает вакансии и однодневные ивенты.
        client = StubClient(offers=[OFFER, ONE_DAY_OFFER], total=2)
        found = list(self.provider(client).search(spec()))
        self.assertEqual([v.external_id for v in found], ["36133"])

    def test_inactive_offers_filtered_out(self):
        client = StubClient(offers=[{**OFFER, "is_active": False}], total=1)
        self.assertEqual(list(self.provider(client).search(spec())), [])

    def test_pagination_advances_offset_until_total(self):
        client = StubClient(offers=[OFFER] * 30, total=60)
        prov = self.provider(client)
        list(prov.search(SearchSpec(name="t", provider="getmatch", params={}, max_pages=5)))
        offsets = [dict(c)["offset"] for c in client.calls]
        self.assertEqual(offsets, ["0", "30"])

    def test_empty_page_stops(self):
        client = StubClient(offers=[], total=100)
        self.assertEqual(list(self.provider(client).search(spec())), [])
        self.assertEqual(len(client.calls), 1)

    def test_config_params_are_recognised(self):
        for search in self.config.searches_for("getmatch"):
            with self.subTest(search=search.name):
                self.assertEqual(self.provider().unknown_params(search), [])


class TestVacancyMapping(GetmatchTestCase):
    def test_offer_mapped(self):
        vacancy = self.provider()._to_vacancy(OFFER)
        self.assertEqual(vacancy.provider, "getmatch")
        self.assertEqual(vacancy.external_id, "36133")
        self.assertEqual(vacancy.company, "X5 Медиа")
        self.assertEqual(
            vacancy.url, "https://getmatch.ru/vacancies/36133-backend-razrabotchik"
        )
        self.assertEqual(vacancy.salary_from, 250000)
        self.assertTrue(vacancy.remote)
        self.assertTrue(vacancy.letter_required)
        self.assertEqual(vacancy.key_skills, ["Python", "PostgreSQL"])

    def test_lowest_seniority_from_list(self):
        # Вакансия для middle и senior доступна middle-кандидату.
        self.assertEqual(self.provider()._to_vacancy(OFFER).seniority, "middle")

    def test_c_level_mapped_to_lead(self):
        offer = {**OFFER, "seniorities": ["c_level"]}
        self.assertEqual(self.provider()._to_vacancy(offer).seniority, "lead")

    def test_seniority_falls_back_to_scalar(self):
        offer = {**OFFER, "seniorities": None, "seniority": "junior"}
        self.assertEqual(self.provider()._to_vacancy(offer).seniority, "junior")

    def test_null_skill_slug_does_not_crash(self):
        # В живом API среди навыков встречается элемент с slug=None.
        offer = {
            **OFFER,
            "skills_objects": [
                {"name": "Python", "slug": "python"},
                {"name": None, "slug": None},
                None,
            ],
        }
        self.assertEqual(self.provider()._to_vacancy(offer).key_skills, ["Python"])

    def test_hidden_salary_not_shown(self):
        offer = {**OFFER, "salary_hidden": True}
        vacancy = self.provider()._to_vacancy(offer)
        self.assertIsNone(vacancy.salary_from)
        self.assertEqual(vacancy.salary_text, "—")

    def test_missing_fields_do_not_crash(self):
        vacancy = self.provider()._to_vacancy({"id": 9})
        self.assertEqual(vacancy.company, "")
        self.assertEqual(vacancy.url, "https://getmatch.ru/vacancies/9")
        self.assertIsNone(vacancy.seniority)
        self.assertFalse(vacancy.remote)

    def test_details_add_full_description(self):
        prov = self.provider()
        vacancy = prov.fetch_details(prov._to_vacancy(OFFER))
        self.assertIn("Развиваем рекламную платформу", vacancy.description)
        self.assertIn("Стек: Django", vacancy.description)
        self.assertNotIn("<", vacancy.description)

    def test_details_survive_http_error(self):
        client = StubClient()

        def boom(offer_id):
            raise GetmatchApiError(500, "server error")

        client.offer = boom
        prov = self.provider(client)
        vacancy = prov.fetch_details(prov._to_vacancy(OFFER))
        self.assertEqual(vacancy.title, "Backend-разработчик (Django)")


class TestApply(GetmatchTestCase):
    def test_manual_by_default(self):
        # У getmatch нет API отклика, поэтому по умолчанию — ручной отклик.
        prov = self.provider()
        result = prov.apply(prov._to_vacancy(OFFER), "письмо")
        self.assertIs(result.status, Status.MANUAL)
        self.assertIn("getmatch.ru", result.detail)

    def test_manual_without_session_cookie(self):
        prov = self.provider(apply_enabled=True)
        prov._cookies = {}
        self.assertIs(prov.apply(prov._to_vacancy(OFFER), "x").status, Status.MANUAL)

    def test_sends_when_enabled_and_authorized(self):
        client = StubClient()
        prov = self.provider(client, apply_enabled=True)
        prov._cookies = {"session": "abc"}
        result = prov.apply(prov._to_vacancy(OFFER), "текст")
        self.assertIs(result.status, Status.SENT)
        self.assertEqual(client.applied, [("36133", "текст")])

    def test_expired_session_stops_the_run(self):
        client = StubClient()
        client.error = GetmatchApiError(401, {"detail": "unauthorized"})
        prov = self.provider(client, apply_enabled=True)
        prov._cookies = {"session": "abc"}
        result = prov.apply(prov._to_vacancy(OFFER), "текст")
        self.assertTrue(result.limit_reached)
        self.assertIn("GETMATCH_SESSION", result.detail)

    def test_hint_explains_absence_of_api(self):
        self.assertIn("нет API", self.provider().authorization_hint())


if __name__ == "__main__":
    unittest.main()
