"""Verify daily image discovery, attribution and graceful text-only fallbacks."""

from copy import deepcopy
from datetime import date
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import app
import dish_images as images


def result():
    """Represent a small Commons response, including its HTML author credit."""
    return {
        "title": "File:Paella mixta.jpg", "index": 1,
        "imageinfo": [{
            "mime": "image/jpeg", "thumburl": "https://thumb.wikimedia.org/paella.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Paella_mixta.jpg",
            "extmetadata": {
                "Artist": {"value": '<a href="/wiki/User:Cook">Cook &amp; Camera</a>'},
                "Credit": {"value": "Own work"},
                "ImageDescription": {"value": "Paella mixta, a rice dish"},
                "Categories": {"value": "Paella dishes"},
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
            },
        }],
    }


class SelectionTests(unittest.TestCase):
    """Require relevant photos with safe URLs and usable license information."""

    def test_spanish_query_keeps_food_and_diet_words(self):
        terms = images.search_terms("Paella Vegetariana (exclusivo para personas con celiaquía)")
        self.assertEqual(terms, ["paella", "vegetariana"])
        self.assertEqual(images.search_terms("Cazón En Adobo"), ["cazon", "adobo"])

    def test_credit_and_license_are_preserved_as_plain_text(self):
        photo = images.select_image([result()], ["paella", "mixta"])
        self.assertEqual(photo.artist, "Cook & Camera")
        self.assertEqual(photo.license_name, "CC BY-SA 4.0")
        self.assertEqual(photo.credit, "Own work")

    def test_unknown_licenses_and_unsafe_urls_are_omitted(self):
        for field, bad in (("thumburl", "https://evil.example/photo.jpg"),
                           ("descriptionurl", "javascript:alert(1)"), ("mime", "image/svg+xml")):
            with self.subTest(field=field):
                page = result()
                page["imageinfo"][0][field] = bad
                self.assertIsNone(images.select_image([page], ["paella", "mixta"]))
        for field, bad in (("Artist", ""), ("LicenseUrl", "https://creativecommons.org/licenses/by-nc/4.0/"),
                           ("Restrictions", "personality rights"), ("LicenseShortName", "")):
            with self.subTest(field=field):
                page = result()
                page["imageinfo"][0]["extmetadata"][field] = {"value": bad}
                self.assertIsNone(images.select_image([page], ["paella", "mixta"]))

    def test_relevance_and_dietary_terms_are_required(self):
        self.assertIsNone(images.select_image([result()], ["paella", "vegetariana"]))
        self.assertIsNone(images.select_image([result()], ["pollo", "asado"]))

    def test_fish_species_photo_is_not_a_dish(self):
        page = result()
        page["title"] = "File:Pez espada.jpg"
        page["imageinfo"][0]["extmetadata"]["ImageDescription"] = {"value": "Pez espada swimming"}
        page["imageinfo"][0]["extmetadata"]["Categories"] = {"value": "Fish"}
        self.assertIsNone(images.select_image([page], ["pez", "espada"]))

    def test_bad_candidate_does_not_hide_good_candidate(self):
        self.assertIsNotNone(images.select_image([{}, {"imageinfo": [{}]}, result()], ["paella", "mixta"]))

    def test_filename_match_ranks_above_description_only(self):
        other = deepcopy(result())
        other["title"] = "File:Restaurant.jpg"
        photo = images.select_image([other, result()], ["paella", "mixta"])
        self.assertEqual(photo.title, "Paella mixta.jpg")

    def test_api_request_is_bounded_and_includes_credit_metadata(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"query": {"pages": [result()]}}).encode()
        with patch("dish_images.urlopen") as fetch:
            fetch.return_value.__enter__.return_value = response
            self.assertEqual(len(images.search_commons(["paella", "mixta"], 5)), 1)
        request = fetch.call_args.args[0]
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(params["gsrnamespace"], ["6"])
        self.assertIn("LicenseUrl", params["iiextmetadatafilter"][0])
        self.assertEqual(fetch.call_args.kwargs["timeout"], 5)
        response.read.assert_called_once_with(1_000_001)


class TranslationTests(unittest.TestCase):
    """Validate translation responses before using them as search terms."""

    def test_request_translates_only_the_dish_name(self):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "responseStatus": 200, "quotaFinished": False,
            "responseData": {"translatedText": "<b>Vegetarian</b> paella"},
        }).encode()
        with patch("dish_images.urlopen") as fetch:
            fetch.return_value.__enter__.return_value = response
            translated = images.translate_dish("Paella Vegetariana (exclusivo para personas con celiaquía)", 3)
        self.assertEqual(translated, "Vegetarian paella")
        params = parse_qs(urlsplit(fetch.call_args.args[0].full_url).query)
        self.assertEqual(params, {"q": ["Paella Vegetariana"], "langpair": ["es|en"]})
        self.assertEqual(fetch.call_args.kwargs["timeout"], 3)
        response.read.assert_called_once_with(100_001)

    def test_invalid_or_exhausted_responses_are_rejected(self):
        payloads = [
            {"responseStatus": 429, "responseData": {"translatedText": "Quota exceeded"}},
            {"responseStatus": 200, "quotaFinished": True, "responseData": {"translatedText": "Quota exceeded"}},
            {"responseStatus": 200, "responseData": {"translatedText": ""}},
            {"responseStatus": 200, "responseData": {"translatedText": None}},
            {"responseStatus": 200, "responseData": {"translatedText": "x" * 501}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), patch("dish_images.urlopen") as fetch:
                fetch.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with self.assertRaises(ValueError):
                    images.translate_dish("Paella", 5)

    def test_oversized_request_and_response_are_rejected(self):
        with patch("dish_images.urlopen") as fetch:
            with self.assertRaises(ValueError):
                images.translate_dish("á" * 251, 5)
            fetch.assert_not_called()
            fetch.return_value.__enter__.return_value.read.return_value = b"x" * 100_001
            with self.assertRaises(ValueError):
                images.translate_dish("Paella", 5)


class DiscoveryTests(unittest.TestCase):
    """Search fresh per batch, sharing matches across menus and recipients."""

    def setUp(self):
        self.menus = {"1": [("primero", "Paella Mixta"), ("segundo", "Paella Mixta"),
                            ("acompanamiento", "Ensalada"), ("postre", "Melón")]}

    def test_unique_starters_and_mains_only_and_fresh_next_batch(self):
        with patch("dish_images.search_commons", return_value=[result()]) as search, \
                patch("dish_images.translate_dish") as translate:
            self.assertEqual(list(images.find_images(self.menus)), ["Paella Mixta"])
            search.assert_called_once()
            images.find_images(self.menus)
            self.assertEqual(search.call_count, 2)
            translate.assert_not_called()

    def test_house_name_fallback_retains_dietary_labels(self):
        menus = {"1": [("primero", "Paella Con Setas Al Buen Gusto Vegetariana")]}
        with patch("dish_images.search_commons", return_value=[]) as search, \
                patch("dish_images.translate_dish", return_value="Vegetarian paella with mushrooms house style"):
            self.assertEqual(images.find_images(menus), {})
        queries = [call.args[0] for call in search.call_args_list]
        self.assertEqual(len(queries), 4)
        self.assertTrue(all("vegetariana" in query or "vegetarian" in query for query in queries))
        self.assertEqual(queries[1], ["vegetarian", "paella", "mushrooms", "house", "style"])

    def test_english_match_after_spanish_miss_is_shared_across_menus(self):
        menus = {"1": [("primero", "Pollo Asado")], "2": [("segundo", "Pollo Asado")]}
        page = result()
        page["title"] = "File:Roasted chicken.jpg"
        with patch("dish_images.search_commons", side_effect=[[], [page]]) as search, \
                patch("dish_images.translate_dish", return_value="Roasted chicken") as translate:
            found = images.find_images(menus)
        self.assertEqual(found["Pollo Asado"].title, "Roasted chicken.jpg")
        self.assertEqual([call.args[0] for call in search.call_args_list], [["pollo", "asado"], ["roasted", "chicken"]])
        translate.assert_called_once()

    def test_rejected_spanish_candidates_also_trigger_translation(self):
        page = result()
        page["imageinfo"][0]["extmetadata"]["LicenseUrl"]["value"] = ""
        with patch("dish_images.search_commons", return_value=[page]), \
                patch("dish_images.translate_dish", return_value="Mixed paella") as translate:
            self.assertEqual(images.find_images(self.menus), {})
        translate.assert_called_once()

    def test_translation_failure_keeps_spanish_fallback_and_next_dish(self):
        menus = {"1": [("primero", "Paella Mixta Especial"), ("segundo", "Paella Mixta")]}
        with patch("dish_images.search_commons", side_effect=[[], [result()], [result()]]) as search, \
                patch("dish_images.translate_dish", side_effect=TimeoutError()):
            self.assertEqual(len(images.find_images(menus)), 2)
        self.assertEqual([call.args[0] for call in search.call_args_list],
                         [["paella", "mixta", "especial"], ["paella", "mixta"], ["paella", "mixta"]])

    def test_translation_keeps_missing_dietary_qualifier_and_avoids_duplicate_queries(self):
        with patch("dish_images.translate_dish", return_value="Paella"), \
                patch("dish_images.time.monotonic", return_value=0):
            queries = list(images.image_queries("Paella Vegetariana", 30))
        self.assertEqual(queries, [["paella", "vegetariana"], ["paella", "vegetarian"]])
        with patch("dish_images.translate_dish", return_value="Paella Mixta"), \
                patch("dish_images.time.monotonic", return_value=0):
            self.assertEqual(list(images.image_queries("Paella Mixta", 30)), [["paella", "mixta"]])

    def test_short_english_query_does_not_drop_dish_type(self):
        with patch("dish_images.translate_dish", return_value="Red bean burger house style"), \
                patch("dish_images.time.monotonic", return_value=0):
            queries = list(images.image_queries("Hamburguesa de alubias rojas", 30))
        self.assertEqual(queries[-1], ["red", "bean", "burger"])
        bean_paste = result()
        bean_paste["title"] = "File:Red bean paste.jpg"
        self.assertIsNone(images.select_image([bean_paste], queries[-1]))

    def test_translation_and_english_search_share_time_budget(self):
        with patch("dish_images.time.monotonic", side_effect=[0, 0, 31]), \
                patch("dish_images.search_commons", return_value=[]) as search, \
                patch("dish_images.translate_dish") as translate:
            self.assertEqual(images.find_images(self.menus), {})
            translate.assert_not_called()
            search.assert_called_once()
        with patch("dish_images.time.monotonic", side_effect=[0, 0, 28, 31]), \
                patch("dish_images.search_commons", return_value=[]) as search, \
                patch("dish_images.translate_dish", return_value="Mixed paella") as translate:
            self.assertEqual(images.find_images(self.menus), {})
            translate.assert_called_once_with("Paella Mixta", 2)
            search.assert_called_once()

    def test_unavailable_service_does_not_block_newsletter(self):
        for error in (TimeoutError(), ValueError(), HTTPError(images.API, 429, "Rate limited", {}, None)):
            with self.subTest(error=type(error)), patch("dish_images.search_commons", side_effect=error) as search:
                self.assertEqual(images.find_images(self.menus), {})
                search.assert_called_once()

    def test_time_budget_stops_further_requests(self):
        with patch("dish_images.time.monotonic", side_effect=[0, 31]), patch("dish_images.search_commons") as search:
            self.assertEqual(images.find_images(self.menus), {})
            search.assert_not_called()

    def test_bad_environment_switch_is_rejected(self):
        with patch.dict(os.environ, {"IMAGE_SEARCH_ENABLED": "false"}):
            self.assertFalse(images.enabled_from_env())
        with patch.dict(os.environ, {"IMAGE_SEARCH_ENABLED": "maybe"}):
            with self.assertRaises(ValueError):
                images.enabled_from_env()


class RenderingTests(unittest.TestCase):
    """Only HTML starters and mains gain photos; dish names and text stay readable."""

    def test_html_photos_have_credits_and_plain_text_stays_unchanged(self):
        menus = {"1": [("primero", "Paella Mixta"), ("acompanamiento", "Paella Mixta")],
                 "2": [("primero", "Paella Mixta")]}
        photo = images.select_image([result()], ["paella", "mixta"])
        baseline = app.newsletter(date(2026, 9, 24), menus, "main")
        subject, plain, html = app.newsletter(date(2026, 9, 24), menus, "main", {"Paella Mixta": photo})
        self.assertEqual((subject, plain), baseline[:2])
        self.assertEqual(html.count("<img "), 2)
        self.assertNotIn("Imágenes orientativas", baseline[2])
        self.assertEqual(html.count("Imágenes orientativas"), 1)
        self.assertEqual(html.count("Cook &amp; Camera"), 1)
        footer = html[html.index("Imágenes orientativas"):]
        self.assertIn(photo.license_url, footer)
        self.assertIn(photo.source_url, footer)
        self.assertGreater(html.index("Imágenes orientativas"), html.rindex("<img "))
        self.assertGreater(html.index("Imágenes orientativas"), html.index("Full menu, allergens and updates"))


if __name__ == "__main__":
    unittest.main()
