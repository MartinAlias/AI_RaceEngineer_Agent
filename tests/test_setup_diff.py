import pytest

from race_engineer.setup_diff import (
    SetupError,
    apply_assignments,
    diff,
    format_delta,
    parse_assignment,
)


def test_parse_absolute_and_relative():
    a = parse_assignment("fork.rebound=12")
    assert (a.key, a.value, a.relative) == ("fork.rebound", 12, False)
    b = parse_assignment("tire.front_cold = -0.1")
    assert (b.key, b.value, b.relative) == ("tire.front_cold", -0.1, True)


def test_parse_normalizes_case():
    assert parse_assignment("Fork.Rebound=3").key == "fork.rebound"


def test_unknown_key_suggests_close_match():
    with pytest.raises(SetupError, match="fork.rebound"):
        parse_assignment("fork.rebund=3")


def test_clicks_must_be_whole_numbers():
    with pytest.raises(SetupError, match="whole number"):
        parse_assignment("fork.rebound=1.5")


def test_known_params_need_numbers_but_electronics_are_free():
    with pytest.raises(SetupError):
        parse_assignment("fork.rebound=soft")
    assert parse_assignment("electronics.map=B").value == "B"
    assert parse_assignment("electronics.tc=+1").relative


def test_apply_relative_and_absolute():
    params = {"fork.rebound": 10, "tire.front_cold": 2.1}
    new, deltas = apply_assignments(params, [parse_assignment("fork.rebound=+2"),
                                             parse_assignment("tire.front_cold=-0.1")])
    assert new == {"fork.rebound": 12, "tire.front_cold": 2.0}
    assert params == {"fork.rebound": 10, "tire.front_cold": 2.1}  # input untouched
    assert deltas == [
        {"param": "fork.rebound", "from": 10, "to": 12},
        {"param": "tire.front_cold", "from": 2.1, "to": 2.0},
    ]


def test_relative_change_needs_existing_value():
    with pytest.raises(SetupError, match="no current value"):
        apply_assignments({}, [parse_assignment("shock.rebound=+1")])


def test_diff_orders_by_group_and_includes_removed_keys():
    deltas = diff({"tire.rear_cold": 1.9, "fork.height": 5}, {"tire.rear_cold": 2.0})
    assert [d["param"] for d in deltas] == ["fork.height", "tire.rear_cold"]
    assert deltas[0]["to"] is None


def test_format_delta():
    assert format_delta({"param": "fork.rebound", "from": 10, "to": 12}) == "fork.rebound 10 -> 12 (+2)"
    assert format_delta({"param": "electronics.map", "from": "A", "to": "B"}) == "electronics.map A -> B"


@pytest.mark.parametrize("text, expected", [
    ("tire.front_cold=2.1", (2.1, False)),
    ("tire.front_cold=2.1bar", (2.1, False)),
    ("tire.front_cold=30psi", (2.07, False)),
    ("tire.front_cold=30", (2.07, False)),  # too high for bar: read as psi
    ("tire.rear_cold=-0.1", (-0.1, True)),
    ("tire.rear_cold=-1psi", (-0.07, True)),
])
def test_pressures_accept_psi(text, expected):
    a = parse_assignment(text)
    assert (a.value, a.relative) == expected


def test_pressure_errors():
    from race_engineer.setup_diff import parse_pressure
    with pytest.raises(SetupError, match="bar .* or psi"):
        parse_pressure("hot")
    with pytest.raises(SetupError, match="not a change"):
        parse_pressure("+0.1")


def test_hot_pressures_are_not_setup_parameters():
    with pytest.raises(SetupError, match="re pressure"):
        parse_assignment("tire.front_hot=2.4")
