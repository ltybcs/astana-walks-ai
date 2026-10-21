"""Pure alternative selection: explicit negatives and nonamenity constraints stay hard."""
from copy import deepcopy
from dataclasses import replace
import unittest

from app.amenity_fallback import AMENITY_KEYS, relax_amenities, unconfirmed_amenities
from app.retrieval import matched_goal_indices, source_goal_evidence
from app.semantic_intent import deterministic_plan, validate_intent
from tests.test_goal_scoped_requirements import goal, request, requirement, place


class AmenityFallbackTests(unittest.TestCase):
    def test_copy_changes_only_required_conveniences_and_never_original(self):
        query="Я с ребёнком, хочу кафе с розеткой, Wi-Fi и доступом для коляски"
        original=deterministic_plan(query)
        before=deepcopy(original.as_dict())
        alternate=relax_amenities(original)
        self.assertIsNot(alternate,original)
        self.assertEqual(original.as_dict(),before)
        self.assertEqual(alternate.goals,original.goals)
        self.assertEqual(alternate.excluded,original.excluded)
        self.assertEqual(alternate.context,original.context)
        self.assertEqual(alternate.query_sha256,original.query_sha256)
        self.assertEqual(alternate.goal_policy,original.goal_policy)
        self.assertEqual(alternate.start_quote,original.start_quote)
        self.assertEqual(alternate.required_keys,("accessible",))
        for old,new in zip(original.requirements,alternate.requirements):
            self.assertEqual((old.quote,old.key,old.goal_index),(new.quote,new.key,new.goal_index))
            self.assertEqual(new.required,False if old.key in AMENITY_KEYS else old.required)

    def test_only_missing_or_explicit_unknown_is_an_alternative_not_yes_or_no(self):
        plan=deterministic_plan("Хочу кафе с розеткой")
        for features in ({},{"socket":"unknown"}):
            poi=place("cafe",features=features);before=deepcopy(poi)
            self.assertEqual(unconfirmed_amenities(poi,plan),("socket",))
            self.assertEqual(poi,before)
            self.assertFalse(source_goal_evidence(poi,plan))
        for value in ("yes",True,"no",False,"maybe",None):
            with self.subTest(value=value):
                self.assertIsNone(unconfirmed_amenities(place("cafe",features={"socket":value}),plan))

    def test_all_unknowns_reported_but_one_negative_disqualifies_that_branch(self):
        plan=deterministic_plan("Хочу только тихое кафе с розеткой и Wi-Fi")
        self.assertEqual(unconfirmed_amenities(place("cafe"),plan),("socket","wifi","quiet"))
        for key in AMENITY_KEYS:
            self.assertIsNone(unconfirmed_amenities(place("cafe",features={key:"no"}),plan))
        self.assertEqual(unconfirmed_amenities(place("cafe",features={"socket":"yes","wifi":"unknown","quiet":"yes"}),plan),("wifi",))

    def test_nonrequired_preferences_do_not_create_an_alternate(self):
        for query in ("Хочу кафе","Хочу тихое кафе","Хочу кафе без розеток и Wi-Fi"):
            with self.subTest(query=query):
                plan=deterministic_plan(query)
                self.assertIsNone(relax_amenities(plan))
                self.assertIsNone(unconfirmed_amenities(place("cafe"),plan))

    def test_same_branch_owns_its_unknown_amenity_even_when_another_feature_is_no(self):
        query="Хочу кафе с розеткой или парк с Wi-Fi"
        plan=validate_intent(request(goal("кафе","cafe"),goal("парк","park"),requirements=[
            requirement("розеткой","socket",0),requirement("Wi-Fi","wifi",1)]),query)
        self.assertEqual(unconfirmed_amenities(place("cafe",features={"wifi":"no"}),plan),("socket",))
        self.assertEqual(unconfirmed_amenities(place("park",features={"socket":"no"}),plan),("wifi",))
        self.assertIsNone(unconfirmed_amenities(place("cafe",features={"socket":"no"}),plan))
        self.assertIsNone(unconfirmed_amenities(place("museum"),plan))
        self.assertEqual([r.goal_index for r in relax_amenities(plan).requirements],[0,1])

    def test_strictly_satisfied_alternative_never_receives_an_unknown_badge(self):
        plan=deterministic_plan("Хочу кафе с розеткой или парк")
        self.assertIsNone(unconfirmed_amenities(place("park"),plan))
        self.assertIsNone(unconfirmed_amenities(place("cafe",features={"socket":"yes"}),plan))
        both=place("cafe",source_categories=["cafe","park"])
        self.assertIsNone(unconfirmed_amenities(both,plan))
        self.assertEqual(matched_goal_indices(both,relax_amenities(plan)),(0,1))

    def test_all_matched_branches_checked_not_just_first_with_known_negative(self):
        plan=validate_intent(request(goal("кафе","cafe"),goal("парк","park"),requirements=[
            requirement("розеткой","socket",0),requirement("Wi-Fi","wifi",1)]),
            "Хочу кафе с розеткой или парк с Wi-Fi")
        dual=place("cafe",source_categories=["cafe","park"],features={"socket":"no"})
        self.assertEqual(matched_goal_indices(dual,relax_amenities(plan)),(0,1))
        self.assertEqual(unconfirmed_amenities(dual,plan),("wifi",))

    def test_primary_and_type_never_replaced_by_another_category(self):
        plan=validate_intent(request(goal("парк","park"),goal("кафе","cafe"),policy="primary",
            requirements=[requirement("Wi-Fi","wifi")]),"Главная цель парк, ещё интересно кафе. Нужен Wi-Fi")
        self.assertEqual(unconfirmed_amenities(place("park"),plan),("wifi",))
        self.assertIsNone(unconfirmed_amenities(place("cafe"),plan))
        club=deterministic_plan("Хочу компьютерный клуб с Wi-Fi")
        self.assertIsNone(unconfirmed_amenities(place("cafe"),club))

    def test_accessibility_and_extra_service_proof_stay_required(self):
        plan=deterministic_plan("Хочу кафе с Wi-Fi и входом для коляски")
        self.assertIsNone(unconfirmed_amenities(place("cafe"),plan))
        self.assertEqual(unconfirmed_amenities(place("cafe",features={"wheelchair":"yes"}),plan),("wifi",))
        for phrase,key in (("интерактивы","activity"),("оборудование","equipment"),
                           ("товар в наличии","inventory"),("цена","price")):
            query="Хочу кафе с Wi-Fi. Обязательно "+phrase
            extra=validate_intent(request(goal("кафе","cafe"),policy="primary",requirements=[
                requirement("Wi-Fi","wifi"),requirement(phrase,key)]),query)
            with self.subTest(key=key):
                self.assertIn(key,relax_amenities(extra).required_keys)
                self.assertIsNone(unconfirmed_amenities(place("cafe"),extra))

    def test_service_in_other_or_branch_does_not_spread_but_it_still_needs_proof(self):
        query="Хочу детские площадки или музей с интерактивами. Нужен Wi-Fi"
        plan=validate_intent(request(goal("детские площадки","playground"),
            goal("музей с интерактивами","museum","specific_activity"),requirements=[requirement("Wi-Fi","wifi")]),query)
        self.assertEqual(unconfirmed_amenities(place("playground"),plan),("wifi",))
        self.assertIsNone(unconfirmed_amenities(place("museum"),plan))

    def test_excluded_adult_and_restricted_source_records_do_not_become_alternates(self):
        query="Я с ребёнком, хочу кафе или бар. Нужен Wi-Fi"
        child=validate_intent(request(goal("кафе","cafe"),goal("бар","bar"),requirements=[requirement("Wi-Fi","wifi")]),query)
        self.assertIsNone(unconfirmed_amenities(place("bar"),child))
        self.assertIsNone(unconfirmed_amenities(place("cafe",source_categories=["cafe","nightclub"]),child))
        ordinary=deterministic_plan("Хочу кафе с Wi-Fi")
        for kwargs in ({"source_tags":{"access":"private"}},{"access_evidence":"no"},{"source_tags":[]}):
            self.assertIsNone(unconfirmed_amenities(place("cafe",**kwargs),ordinary))
        excluded=deterministic_plan("Хочу погулять с Wi-Fi, не хочу кафе")
        self.assertIsNone(unconfirmed_amenities(place("cafe"),excluded))

    def test_malformed_record_and_unknown_types_cannot_supply_an_alternate(self):
        plan=deterministic_plan("Хочу кафе с Wi-Fi")
        for bad in (None,{},place("unknown"),{**place("cafe"),"category":[]},
                    {**place("cafe"),"features":[]},{**place("cafe"),"features":{"wifi":"unavailable"}}):
            self.assertIsNone(unconfirmed_amenities(bad,plan))
        with self.assertRaises(TypeError):relax_amenities({})


if __name__=="__main__":unittest.main()
