import pytest

from race_engineer import guide, ranges, setup_diff


def test_symptom_guide_loads_and_is_valid():
    symptoms = guide.load_symptoms()
    assert len(symptoms) >= 15
    for s in symptoms.values():
        assert s.causes, f"{s.id} has no causes"
        assert s.follow_up, f"{s.id} has no follow-up questions"
        assert s.keywords, f"{s.id} has no keywords"
        for cause in s.causes:
            for a in cause.adjustments:
                assert a.step != 0 and a.effect, f"{s.id}: {a.param} needs a step and an effect"


def test_default_ranges_cover_every_parameter():
    assert set(ranges.default_ranges()) == set(setup_diff.PARAMETERS)


def test_guide_rejects_unknown_parameters():
    raw = [{"id": "x", "name": "X", "end": "front", "phases": ["mid"], "keywords": ["x"],
            "causes": [{"description": "d", "likelihood": "high", "adjustments": [{"param": "fork.rebund", "step": 1}]}]}]
    with pytest.raises(guide.GuideError, match="fork.rebound"):
        guide.parse_symptoms(raw)


def test_guide_rejects_fractional_clicks():
    raw = [{"id": "x", "name": "X", "keywords": ["x"],
            "causes": [{"description": "d", "likelihood": "low", "adjustments": [{"param": "fork.rebound", "step": 0.5}]}]}]
    with pytest.raises(guide.GuideError, match="whole number"):
        guide.parse_symptoms(raw)


@pytest.mark.parametrize("text, expected", [
    ("front feels slippery mid corner T3, T7", "front_push_mid"),
    ("front tucks on the brakes at turn in", "front_push_entry"),  # two phases, both covered
    ("rear spins when I pick up the throttle", "rear_spin_exit"),
    ("bike runs wide on exit", "runs_wide_exit"),
    ("bouncing off the limiter before the braking marker", "gearing_short"),
    ("headshake over the crest", "headshake"),
    ("weather was nice", None),
])
def test_best_symptom(text, expected):
    assert guide.best_symptom(text) == expected


def test_match_respects_end():
    ids = [m.symptom.id for m in guide.match("rear chatter mid corner")]
    assert ids[0] == "rear_chatter"
    assert "front_chatter" not in ids


def test_range_violations_only_checks_given_keys():
    limits = {"fork.rebound": (0, 20), "tire.front_cold": (1.8, 2.5)}
    params = {"fork.rebound": 25, "tire.front_cold": 2.6}
    assert len(ranges.violations(limits, params)) == 2
    assert ranges.violations(limits, params, ["tire.front_cold"]) == [
        "tire.front_cold = 2.6 is outside the allowed range 1.8 to 2.5 bar"
    ]
