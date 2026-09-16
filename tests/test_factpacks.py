import json
import unittest

from sekaisync.factpacks import build_fact_pack, build_fact_pack_at
from sekaisync.models import Entity


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

    def _entity(self, name="UnreleasedCharacter", start=None):
        facts = {"unit": "SecretUnit"}
        if start is not None:
            facts["startAt"] = start
        return Entity(
            id="character:future",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": name, "ja": name},
            facts=facts,
            source="master_db:demo",
            demo=True,
        )

    def _payload(self, entity, **kwargs):
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

    def test_as_of_without_region_is_not_promised_region_correct(self):
        """Astra P05/B6: region-sensitivity is NOT solved here.

        ``build_fact_pack_at`` has no region parameter, so a caller cannot ask
        "what was public in EN at time T".  This test pins that limitation
        explicitly rather than letting the function look region-aware.
        """
        import inspect

        params = inspect.signature(build_fact_pack_at).parameters
        self.assertNotIn(
            "region",
            params,
            "region support appeared — update this test and re-check that "
            "per-region public times are actually modelled (Astra P03/B6)",
        )


if __name__ == "__main__":
    unittest.main()
