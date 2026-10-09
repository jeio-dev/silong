import json
import unittest

from silong import rules


def fields(**overrides):
    """A valid, empty field object with `overrides` applied."""
    base = {
        "location": None,
        "in_center": None,
        "on_behalf": None,
        "headcount": None,
        "vulnerable": [],
        "injured": False,
        "injury_note": None,
        "needs": [],
        "summary": "",
    }
    base.update(overrides)
    return base


class ClassifyTest(unittest.TestCase):
    def assertClassified(self, value, urgency, reason):
        self.assertEqual(rules.classify(rules.validate_fields(value)), (urgency, reason))

    def test_rescue_outside_center(self):
        self.assertClassified(
            fields(needs=["rescue"], in_center=False, on_behalf=True),
            "critical",
            "Critical: rescue needed outside center",
        )

    def test_outside_center_rescue_beats_injury_and_medical(self):
        self.assertClassified(
            fields(needs=["medical", "rescue"], in_center=False, injured=True),
            "critical",
            "Critical: rescue needed outside center",
        )

    def test_rescue_with_unknown_or_inside_center_uses_general_rule(self):
        for in_center in (None, True):
            with self.subTest(in_center=in_center):
                self.assertClassified(
                    fields(needs=["rescue"], in_center=in_center),
                    "critical",
                    "Critical: rescue needed",
                )

    def test_medical_need(self):
        self.assertClassified(
            fields(needs=["medical", "food"], vulnerable=["senior"]),
            "critical",
            "Critical: medical need",
        )

    def test_injury_reported(self):
        self.assertClassified(
            fields(injured=True, injury_note="sugat sa binti", needs=["water"]),
            "critical",
            "Critical: injury reported",
        )

    def test_vulnerable_with_supply_need(self):
        self.assertClassified(
            fields(vulnerable=["infant"], needs=["milk_formula"]),
            "high",
            "High: infant + milk_formula",
        )

    def test_high_reason_lists_every_vulnerable_tag_and_matching_supply(self):
        self.assertClassified(
            fields(vulnerable=["infant", "senior"], needs=["hygiene", "water", "medicine"]),
            "high",
            "High: senior, infant + medicine, water",
        )

    def test_vulnerable_without_supply_need_is_medium(self):
        self.assertClassified(
            fields(vulnerable=["pregnant"], needs=["hygiene"]),
            "medium",
            "Medium: needs hygiene",
        )

    def test_vulnerable_without_needs_is_low(self):
        self.assertClassified(fields(vulnerable=["pwd"]), "low", "Low: no need stated")

    def test_other_stated_need_is_medium(self):
        self.assertClassified(
            fields(needs=["other", "food"]), "medium", "Medium: needs food, other"
        )

    def test_no_need_is_low(self):
        self.assertClassified(fields(), "low", "Low: no need stated")


class ValidateFieldsTest(unittest.TestCase):
    def assertInvalid(self, value, message_part, **kwargs):
        with self.assertRaises(ValueError) as caught:
            rules.validate_fields(value, **kwargs)
        self.assertIn(message_part, str(caught.exception))

    def test_unknown_flags_and_values_stay_null(self):
        self.assertEqual(rules.validate_fields(fields()), fields())

    def test_valid_fields_are_kept(self):
        value = fields(
            location="Brgy. San Roque, Purok 2",
            in_center=False,
            on_behalf=True,
            headcount=0,
            vulnerable=["senior"],
            injured=True,
            injury_note="Sugat sa ulo",
            needs=["rescue"],
            summary="Family of four trapped on roof, senior injured.",
        )
        self.assertEqual(rules.validate_fields(value), value)

    def test_strings_are_stripped_and_blank_becomes_null(self):
        result = rules.validate_fields(
            fields(location="  Room 3 ", injury_note="   ", summary="  Needs water.  ")
        )
        self.assertEqual(result["location"], "Room 3")
        self.assertIsNone(result["injury_note"])
        self.assertEqual(result["summary"], "Needs water.")

    def test_blank_whitespace_and_null_summary_become_empty_string(self):
        for summary in ("", "  \n ", None):
            with self.subTest(summary=summary):
                self.assertEqual(rules.validate_fields(fields(summary=summary))["summary"], "")

    def test_lists_are_deduplicated_in_canonical_order(self):
        result = rules.validate_fields(
            fields(vulnerable=["child", "senior", "child"], needs=["water", "rescue", "water"])
        )
        self.assertEqual(result["vulnerable"], ["senior", "child"])
        self.assertEqual(result["needs"], ["rescue", "water"])

    def test_input_is_not_modified(self):
        value = fields(needs=["water", "water"], location=" Room 1 ")
        rules.validate_fields(value)
        self.assertEqual(value["needs"], ["water", "water"])
        self.assertEqual(value["location"], " Room 1 ")

    def test_missing_and_extra_keys_are_rejected(self):
        value = fields()
        del value["on_behalf"]
        self.assertInvalid(value, "missing field(s): on_behalf")
        self.assertInvalid(fields(urgency="low"), "unknown field(s): urgency")

    def test_non_object_is_rejected(self):
        for value in (None, [], "fields"):
            with self.subTest(value=value):
                self.assertInvalid(value, "fields must be an object")

    def test_wrong_types_are_rejected(self):
        cases = [
            (fields(location=3), "location"),
            (fields(in_center="yes"), "in_center"),
            (fields(on_behalf=0), "on_behalf"),
            (fields(headcount="3"), "headcount must be an integer"),
            (fields(headcount=2.5), "headcount must be an integer"),
            (fields(headcount=True), "headcount must be an integer"),
            (fields(vulnerable="infant"), "vulnerable must be a list"),
            (fields(injured=None), "injured must be true or false"),
            (fields(injury_note=["sugat"]), "injury_note"),
            (fields(needs=None), "needs must be a list"),
            (fields(summary=5), "summary must be text"),
        ]
        for value, message_part in cases:
            with self.subTest(message=message_part):
                self.assertInvalid(value, message_part)

    def test_unknown_enum_values_are_rejected(self):
        self.assertInvalid(fields(vulnerable=["elderly"]), "vulnerable contains 'elderly'")
        self.assertInvalid(fields(needs=["shelter"]), "needs contains 'shelter'")

    def test_negative_headcount_is_rejected(self):
        self.assertInvalid(fields(headcount=-1), "headcount must not be negative")

    def test_staff_summary_over_twenty_words_is_rejected(self):
        twenty = " ".join(f"w{i}" for i in range(20))
        self.assertEqual(rules.validate_fields(fields(summary=twenty))["summary"], twenty)
        self.assertInvalid(fields(summary=twenty + " extra"), "at most 20 words")

    def test_model_summary_is_truncated_to_first_twenty_words(self):
        words = [f"w{i}" for i in range(25)]
        result = rules.validate_fields(fields(summary=" ".join(words)), truncate_summary=True)
        self.assertEqual(result["summary"], " ".join(words[:20]))


def row(row_id, created_at, *, urgency=None, headcount=None, resolved=0):
    """A stored report row as `db.list_reports` returns it (synthetic)."""
    stored = None if urgency is None else json.dumps(fields(headcount=headcount))
    return {
        "id": row_id,
        "created_at": f"2026-10-09T08:{created_at:02d}:00.000000+00:00",
        "urgency": urgency,
        "fields_json": stored,
        "resolved": resolved,
    }


class OrderTest(unittest.TestCase):
    def ids(self, rows):
        return [r["id"] for r in rules.order_reports(rows)]

    def test_classified_by_urgency_then_oldest_receipt(self):
        rows = [
            row(1, 1, urgency="low"),
            row(2, 2, urgency="medium"),
            row(3, 3, urgency="critical"),
            row(4, 4, urgency="high"),
            row(5, 0, urgency="critical"),
        ]
        self.assertEqual(self.ids(rows), [5, 3, 4, 2, 1])

    def test_same_urgency_and_receipt_larger_headcount_first_unknown_last(self):
        rows = [
            row(1, 5, urgency="high", headcount=None),
            row(2, 5, urgency="high", headcount=2),
            row(3, 5, urgency="high", headcount=7),
            row(4, 5, urgency="high", headcount=0),
            row(5, 5, urgency="high", headcount=None),
        ]
        self.assertEqual(self.ids(rows), [3, 2, 4, 1, 5])

    def test_unclassified_after_classified_oldest_first(self):
        rows = [
            row(1, 3),
            row(2, 9, urgency="low"),
            row(3, 1),
            row(4, 5, urgency="critical"),
        ]
        self.assertEqual(self.ids(rows), [4, 2, 3, 1])

    def test_resolved_last_newest_receipt_first(self):
        rows = [
            row(1, 1, urgency="critical", resolved=1),
            row(2, 2, resolved=1),
            row(3, 3, urgency="low"),
            row(4, 4),
            row(5, 5, urgency="medium", resolved=1),
        ]
        self.assertEqual(self.ids(rows), [3, 4, 5, 2, 1])

    def test_receipt_order_uses_time_not_text_across_offsets(self):
        early = {**row(1, 0, urgency="low"), "created_at": "2026-10-09T09:00:00+08:00"}
        late = {**row(2, 30, urgency="low")}
        self.assertEqual(self.ids([late, early]), [1, 2])


if __name__ == "__main__":
    unittest.main()
