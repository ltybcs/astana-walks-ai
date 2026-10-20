"""Known explicit endpoints are checked without any inference or network."""
import unittest

from app.google_budget import GoogleError
from app.route_request import validate_walking_request


class PointToPointTests(unittest.TestCase):
    def assert_code(self, query, minutes, code):
        with self.assertRaises(GoogleError) as caught:
            validate_walking_request(query, minutes)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.status, 422)
        return caught.exception

    def test_expo_to_airport_in_ten_minutes_is_rejected_without_route_estimate(self):
        error = self.assert_code("Хочу пешком от Экспо до аэропорта за 10 минут", [10], "route_does_not_fit")
        self.assertIn("10 мин", error.message)
        self.assertIn("дополнительные остановки не подбирались", error.message)
        self.assertNotIn("рейс", error.message)
        self.assertNotIn("км", error.message)

    def test_nearby_endpoints_are_not_silently_changed_into_a_circle(self):
        for query in ("Пешком от EXPO до Mega за час", "Walk from EXPO to the airport"):
            with self.subTest(query=query):
                self.assert_code(query, [60], "unsupported_point_to_point")

    def test_largest_ready_budget_is_the_only_rejection_bound(self):
        query = "Хочу от Экспо до аэропорта"
        self.assert_code(query, [10], "route_does_not_fit")
        self.assert_code(query, [10, 60], "unsupported_point_to_point")

    def test_airport_visit_and_temporal_before_flight_are_not_endpoint_pairs(self):
        for query in (
            "Я турист около аэропорта, до рейса два часа, хочу кофе",
            "У меня 10 минут до рейса, я у Экспо",
            "Отдохну 10 минут до рейса у аэропорта",
            "Хочу кафе от 10 минут до двух часов рядом с Mega",
        ):
            with self.subTest(query=query):
                self.assertIsNone(validate_walking_request(query, [120]))

    def test_same_public_anchor_is_left_to_existing_circular_flow(self):
        for query in ("От Меги до Mega", "from EXPO to expo", "из Байтерека до Бәйтерека"):
            with self.subTest(query=query):
                self.assertIsNone(validate_walking_request(query, [60]))

    def test_explicitly_negated_route_does_not_override_the_actual_request(self):
        for query in (
            "Не хочу идти от Экспо до аэропорта, хочу кофе возле Mega",
            "Не от Экспо до аэропорта, а прогулку возле Mega",
            "От Экспо до аэропорта не пойду, хочу парк",
            "I don't want from EXPO to airport, I want coffee",
        ):
            with self.subTest(query=query):
                self.assertIsNone(validate_walking_request(query, [10]))

    def test_uncertainty_and_without_transport_still_request_the_route(self):
        for query in (
            "Не знаю как пройти от Экспо до аэропорта",
            "Хочу без машины пройти от Экспо до аэропорта",
            "I want to walk without a car from EXPO to airport",
        ):
            with self.subTest(query=query):
                self.assert_code(query, [10], "route_does_not_fit")

    def test_prefixes_and_incomplete_pairs_do_not_become_public_endpoints(self):
        for query in (
            "от Экспоцентра до аэропорта", "от Мегапарка до аэропорта",
            "от Байтерековского до аэропорта", "от EXPO до аэропортового отеля",
            "от EXPO до", "от неизвестного отеля до Mega",
        ):
            with self.subTest(query=query):
                self.assertIsNone(validate_walking_request(query, [10]))

    def test_multiple_positive_pairs_are_rejected_without_choosing_the_first(self):
        self.assert_code("От Экспо до Mega или от Байтерека до аэропорта", [10], "unsupported_point_to_point")

    def test_all_known_public_anchor_spellings_remain_detectable(self):
        for name in ("Президентского парка", "аэропорта", "Центрального парка", "Абу Даби",
                     "Mega", "Байтерека", "набережной Есиля"):
            with self.subTest(name=name):
                self.assert_code(f"От {name} до EXPO", [480], "unsupported_point_to_point")


if __name__ == "__main__":
    unittest.main()
