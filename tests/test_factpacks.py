import json
import unittest

from sekaisync.factpacks import build_fact_pack, build_fact_pack_at
from sekaisync.models import Entity, RegionFacts


class FactPackTest(unittest.TestCase):
    def test_compact_pack_is_smaller(self):
        entity = Entity(
            id="character:1",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": "Hoshino Ichika", "zh_tw": "星乃一歌"},
            facts={"unit": "Leo/need", "birthday": "3月7日", "height": "163cm"},
            source="master_db:demo",
            demo=True,
        )
        pack = build_fact_pack(entity, language="zh_tw")
        self.assertIn("星乃一歌", pack.text)
        self.assertLess(pack.fact_pack_tokens, pack.raw_json_tokens)


class FactPackAsOfIsolationTest(unittest.TestCase):
    """P05 — a not-yet-public entity must not appear anywhere in the payload.

    The defect (Astra measured ``future_payload_contains_unreleased_name:
    true``) was that the ``future`` branch still called ``build_fact_pack`` and
    returned the full text, so "here is what you must not know yet" shipped the
    very content it was meant to withhold.  Asserting on the whole serialized
    payload — not just the ``past`` key — is the point: a leak can reappear in
    any field, including a warning or summary.
    """

    AS_OF = 1_700_000_000_000  # 2023-11-14
    FUTURE_START = 1_800_000_000_000  # 2027-01-15

    def _entity(self, name="UnreleasedCharacter", start=None, kind="event"):
        facts = {"outline": "SecretUnit"}
        if start is not None:
            facts["startAt"] = start
        # "event" is the type whose public time is actually modelled
        # (``_TIME_FIELDS``) and which carries a story outline, so it exercises
        # both the as_of boundary and the body-language contract.  A type with
        # no declared public-time field stays undated by design.
        return Entity(
            id="event:future",
            type=kind,
            region="demo",
            regions=["demo"],
            names={"en": name, "ja": name},
            facts=facts,
            source="master_db:demo",
            demo=True,
        )

    def _payload(self, entity, **kwargs):
        kwargs.setdefault("region", "demo")
        return build_fact_pack_at(entity, language="en", **kwargs)

    def test_future_entity_leaks_nothing_into_serialized_payload(self):
        payload = self._payload(self._entity(start=self.FUTURE_START), as_of=self.AS_OF)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["state"], "future")
        self.assertNotIn(
            "UnreleasedCharacter",
            blob,
            "unreleased entity name appeared in the serialized payload",
        )
        self.assertNotIn("SecretUnit", blob)
        self.assertEqual(payload["future"]["text"], "")
        self.assertEqual(payload["past"]["text"], "")

    def test_future_entity_counts_what_it_withheld(self):
        """Withholding must still be reportable without leaking content."""
        payload = self._payload(self._entity(start=self.FUTURE_START), as_of=self.AS_OF)
        self.assertEqual(payload["content_status"], "withheld")
        self.assertEqual(payload["withheld"]["reason"], "entity_not_public_at_as_of")
        self.assertGreater(payload["withheld"]["fact_pack_tokens"], 0)

    def test_undated_entity_also_withholds(self):
        """No timestamp means "cannot prove it is public" — so withhold."""
        payload = self._payload(self._entity(), as_of=self.AS_OF)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["state"], "undated")
        self.assertNotIn("UnreleasedCharacter", blob)
        self.assertEqual(payload["withheld"]["reason"], "entity_undated")

    def test_past_entity_still_returns_content(self):
        """The safety fix must not empty the legitimate case."""
        payload = self._payload(self._entity(start=self.AS_OF - 1000), as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")
        self.assertEqual(payload["content_status"], "available")
        self.assertIn("UnreleasedCharacter", payload["past"]["text"])

    def test_as_of_boundary_is_inclusive(self):
        payload = self._payload(self._entity(start=self.AS_OF), as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")

    def test_both_as_of_forms_rejected(self):
        with self.assertRaises(ValueError):
            self._payload(
                self._entity(start=self.AS_OF),
                as_of=self.AS_OF,
                as_of_iso="2023-11-14T00:00:00+00:00",
            )

    def test_naive_iso_rejected_rather_than_assumed_utc(self):
        """Assuming UTC for a naive timestamp can flip the boundary by hours."""
        with self.assertRaises(ValueError):
            self._payload(self._entity(start=self.AS_OF), as_of_iso="2023-11-14T22:13:20")

    def test_offset_iso_accepted(self):
        # 2023-11-15T06:13:20+08:00 is the same instant as AS_OF (22:13:20Z),
        # so an entity one second earlier is in the past.
        payload = self._payload(
            self._entity(start=self.AS_OF - 1000),
            as_of_iso="2023-11-15T06:13:20+08:00",
        )
        self.assertEqual(payload["state"], "past")

    def test_plain_fact_pack_is_unaffected(self):
        """No as_of means the current snapshot — behaviour unchanged."""
        entity = self._entity(start=self.FUTURE_START)
        pack = build_fact_pack(entity, language="en")
        self.assertIn("UnreleasedCharacter", pack.text)

    def test_region_is_required_and_reported(self):
        """Region-sensitivity is modelled now (Astra P03/P05), so a caller must
        state the region instead of letting the function pick one, and the
        answer says whether the facts were region-scoped or fell back."""
        import inspect

        params = inspect.signature(build_fact_pack_at).parameters
        self.assertIn("region", params)
        self.assertIs(params["region"].default, inspect.Parameter.empty)

        payload = self._payload(self._entity(start=self.AS_OF - 1000), region="jp")
        self.assertEqual(payload["region"], "jp")
        # No per-region facts on this entity: the fallback is disclosed, not
        # presented as if it were jp-specific.
        self.assertEqual(payload["region_scope"], "entity")

    def test_region_facts_drive_the_as_of_decision(self):
        """Two regions can publish the same entity at different times: the
        region asked about decides past/future."""
        from sekaisync.models import RegionFacts
        entity = self._entity(start=self.AS_OF - 1000)
        entity.region_facts = {
            "jp": RegionFacts("jp", {"startAt": self.AS_OF - 1000}, "master_db:jp", None, {}),
            "en": RegionFacts("en", {"startAt": self.FUTURE_START}, "master_db:en", None, {}),
        }
        entity.facts = {"unit": "SecretUnit", "startAt": self.FUTURE_START}
        jp = self._payload(entity, region="jp")
        en = self._payload(entity, region="en")
        self.assertEqual((jp["state"], jp["region_scope"]), ("past", "region"))
        self.assertEqual((en["state"], en["region_scope"]), ("future", "region"))
        self.assertNotIn("UnreleasedCharacter", json.dumps(en, ensure_ascii=False))

    def test_unlisted_time_fields_do_not_make_an_entity_public(self):
        """Only the type's declared public-time fields count.

        The old scan accepted any ``*At`` key, so an end/updated timestamp made
        unpublished content look public.
        """
        entity = self._entity(start=None)
        entity.facts["closedAt"] = self.AS_OF - 1000
        entity.facts["updatedAt"] = self.AS_OF - 1000
        payload = self._payload(entity, region="demo", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "undated")
        self.assertNotIn("UnreleasedCharacter", json.dumps(payload, ensure_ascii=False))

    def test_body_language_follows_the_request(self):
        """An English request must not silently receive the Japanese body.

        The registry folds a story's per-language outline onto its *event*
        entity, so this is the shape the body contract actually runs on.
        """
        entity = self._entity(start=self.AS_OF - 1000)
        entity.region_facts = {"demo": RegionFacts("demo", {
            "startAt": self.AS_OF - 1000,
            "outline_ja": "日本語のあらすじ",
            "outline_en": "English outline",
        }, "master_db:demo", None, {})}
        payload = build_fact_pack_at(entity, language="en", region="demo", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")
        self.assertEqual(payload["effective_language"], "en")
        self.assertIn("English outline", payload["past"]["text"])
        self.assertNotIn("日本語のあらすじ", payload["past"]["text"])

    def test_missing_requested_body_is_reported_not_substituted_silently(self):
        entity = self._entity(start=self.AS_OF - 1000)
        entity.region_facts = {"demo": RegionFacts("demo", {
            "startAt": self.AS_OF - 1000, "outline_ja": "日本語のあらすじ",
        }, "master_db:demo", None, {})}
        payload = build_fact_pack_at(entity, language="en", region="demo", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")
        # The requested language is absent; the answer says which one was used
        # instead of pretending the request was satisfied.
        self.assertEqual(payload["effective_language"], "ja")
        self.assertEqual(payload["content_status"], "available")

    def test_no_body_at_all_is_reported_as_missing(self):
        entity = self._entity(start=self.AS_OF - 1000)
        entity.facts = {"startAt": self.AS_OF - 1000}
        payload = build_fact_pack_at(entity, language="en", region="demo", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")
        # No outline at all: "missing" states the absence instead of dressing
        # an empty field up as available content.
        self.assertEqual(payload["content_status"], "missing")
        self.assertIsNone(payload["effective_language"])


if __name__ == "__main__":
    unittest.main()
