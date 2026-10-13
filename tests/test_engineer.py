"""The engineer loop against a scripted fake API client (no network, no cost)."""

import json
from types import SimpleNamespace

import anthropic
import pytest

from race_engineer import repo
from race_engineer.agent.engineer import Engineer, EngineerConfig, EngineerOffline
from race_engineer.agent.tools import TOOLS
from race_engineer.setup_diff import parse_assignment


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, input, id=None):
    return SimpleNamespace(type="tool_use", id=id or f"tu_{name}", name=name, input=input)


def reply(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class FakeClient:
    """Returns scripted responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Snapshot messages: the engineer keeps appending to the same list.
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedIO:
    def __init__(self, answers=(), confirms=()):
        self.answers = list(answers)
        self.confirms = list(confirms)
        self.said, self.notices, self.proposals, self.questions = [], [], [], []

    def say(self, t):
        self.said.append(t)

    def notice(self, t):
        self.notices.append(t)

    def ask(self, prompt):
        self.questions.append(prompt)
        return self.answers.pop(0) if self.answers else None

    def confirm(self, prompt):
        self.questions.append(prompt)
        return self.confirms.pop(0)

    def show_proposal(self, title, lines):
        self.proposals.append(lines)


@pytest.fixture
def push(conn, weekend):
    bike, track, session = weekend
    repo.add_laps(conn, session.id, [121_955])
    feedback = repo.add_feedback(conn, session.id, "front feels slippery mid corner T3, T7")
    return bike, track, session, feedback


def make(conn, push, responses, io):
    bike, _, session, feedback = push
    client = FakeClient(responses)
    engineer = Engineer(conn, client, io, bike_id=bike.id, session_id=session.id, feedback_id=feedback.id,
                        config=EngineerConfig(model="claude-opus-5-5", effort="high"))
    return engineer, client


def tool_results(request):
    """tool_result blocks in the last message of a request, by tool_use_id."""
    return {b["tool_use_id"]: b for b in request["messages"][-1]["content"]}


def test_diagnose_ask_and_apply_change(conn, push):
    bike, _, session, feedback = push
    io = ScriptedIO(answers=["it floats right after I release the brake", "q"], confirms=[True])
    engineer, client = make(conn, push, [
        reply(tool("get_session_context", {}), stop="tool_use"),
        reply(text("Does the front float as you release the brake?")),
        reply(text("Classic rebound. Two clicks in."),
              tool("propose_changes", {
                  "changes": [{"param": "fork.rebound", "step": -2}],
                  "symptom": "front_push_mid",
                  "hypothesis": "fork rebound too fast, front unloads at brake release",
                  "reasoning": "floats right at brake release",
                  "watch_for": "front holds its line; may feel slower over T1 bumps"}),
              stop="tool_use"),
        reply(text("Go and try it in FP2.")),
    ], io)
    engineer.run(engineer.opening_message())

    # Opening message carries the complaint and the keyword tags.
    first = client.requests[0]["messages"][0]["content"]
    assert "front feels slippery mid corner T3, T7" in first and "symptom=front_push_mid" in first

    # The context tool returned the real setup.
    context = json.loads(tool_results(client.requests[1])["tu_get_session_context"]["content"])
    assert context["current_setup"]["values"][0]["param"] == "fork.compression"
    assert context["session"]["best_lap"] == "2:01.955"
    assert context["session"]["feedback"][0]["symptom"] == "front_push_mid"

    # The rider's answer went back as a user turn.
    assert client.requests[2]["messages"][-1] == {"role": "user", "content": "it floats right after I release the brake"}

    # The proposal was shown, accepted and recorded as an agent change.
    assert io.proposals[0][0] == "fork.rebound 10 -> 8 (-2)"
    result = json.loads(tool_results(client.requests[3])["tu_propose_changes"]["content"])
    assert result["status"] == "applied"
    change = repo.get_change(conn, result["change_id"])
    assert (change.proposed_by, change.symptom, change.session_id) == ("agent", "front_push_mid", session.id)
    assert repo.get_bike(conn, bike.id).current_setup_id == change.after_setup_id

    assert io.said[-1] == "Go and try it in FP2."
    saved = conn.execute("SELECT * FROM engineer_conversation").fetchone()
    assert saved["feedback_id"] == feedback.id and len(json.loads(saved["messages"])) == 8


def test_request_settings(conn, push):
    engineer, client = make(conn, push, [reply(text("Hi."))], ScriptedIO())
    engineer.run("hello")
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"] == {"effort": "high"}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["tools"] is TOOLS
    assert "thinking" not in request and "temperature" not in request


def test_out_of_range_proposal_goes_back_to_the_model_not_the_rider(conn, push):
    bike = push[0]
    repo.set_param_range(conn, bike.id, "fork.rebound", 0, 11)
    io = ScriptedIO()
    engineer, client = make(conn, push, [
        reply(tool("propose_changes", {"changes": [{"param": "fork.rebound", "step": 2}], "hypothesis": "h",
                                       "reasoning": "r", "watch_for": "w"}), stop="tool_use"),
        reply(text("Ok, something else then.")),
    ], io)
    engineer.run("go")
    result = tool_results(client.requests[1])["tu_propose_changes"]
    assert result["is_error"] and "outside the allowed range 0 to 11" in result["content"]
    assert io.proposals == [] and repo.list_changes(conn, bike_id=bike.id) == []


@pytest.mark.parametrize("changes, message", [
    ([{"param": "fork.rebound", "step": -1}, {"param": "fork.compression", "step": 1},
      {"param": "tire.front_cold", "step": -0.1}], "at most 2"),
    ([{"param": "fork.rebound"}], "either step or value"),
    ([{"param": "fork.rebound", "step": -1}, {"param": "fork.rebound", "step": -1}], "only once"),
    ([{"param": "fork.height", "step": 2}], "no current value"),
    ([{"param": "fork.rebund", "step": 1}], "fork.rebound"),
])
def test_invalid_proposals_are_errors(conn, push, changes, message):
    engineer, client = make(conn, push, [
        reply(tool("propose_changes", {"changes": changes, "hypothesis": "h", "reasoning": "r", "watch_for": "w"}),
              stop="tool_use"),
        reply(text("ok")),
    ], ScriptedIO())
    engineer.run("go")
    result = tool_results(client.requests[1])["tu_propose_changes"]
    assert result["is_error"] and message in result["content"]


def test_absolute_values_including_negative(conn, push):
    bike = push[0]
    repo.change_setup(conn, bike.id, [parse_assignment("shock.ride_height=0")])
    engineer, _ = make(conn, push, [
        reply(tool("propose_changes", {"changes": [{"param": "shock.ride_height", "value": -2},
                                                   {"param": "tire.front_cold", "value": 2.0}],
                                       "hypothesis": "h", "reasoning": "r", "watch_for": "w"}), stop="tool_use"),
        reply(text("ok")),
    ], ScriptedIO(confirms=[True]))
    engineer.run("go")
    params = repo.get_setup(conn, repo.get_bike(conn, bike.id).current_setup_id).params
    assert params["shock.ride_height"] == -2 and params["tire.front_cold"] == 2


def test_declined_proposal(conn, push):
    bike = push[0]
    io = ScriptedIO(answers=["I'd rather try pressure first"], confirms=[False])
    engineer, client = make(conn, push, [
        reply(tool("propose_changes", {"changes": [{"param": "fork.rebound", "step": -2}], "hypothesis": "h",
                                       "reasoning": "r", "watch_for": "w"}), stop="tool_use"),
        reply(text("Fair enough.")),
    ], io)
    engineer.run("go")
    result = json.loads(tool_results(client.requests[1])["tu_propose_changes"]["content"])
    assert result == {"status": "declined", "rider_comment": "I'd rather try pressure first"}
    assert repo.list_changes(conn, bike_id=bike.id) == []


def test_record_outcome_tool(conn, push):
    bike, track, fp1, _ = push
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id,
                               symptom="front_push_mid")
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2")
    repo.add_laps(conn, fp2.id, [121_388])
    client = FakeClient([
        reply(tool("record_outcome", {"change_id": change.id, "verdict": "better", "symptom_resolved": True,
                                      "notes": "front planted"}), stop="tool_use"),
        reply(text("Logged.")),
    ])
    engineer = Engineer(conn, client, ScriptedIO(confirms=[True]), bike_id=bike.id, session_id=fp2.id)
    engineer.run("FP2 done")
    result = json.loads(tool_results(client.requests[1])["tu_record_outcome"]["content"])
    assert result["status"] == "recorded" and result["best_lap_change"] == "-0.567"
    outcome = repo.list_outcomes(conn, change.id)[0]
    assert (outcome.session_id, outcome.symptom_resolved, outcome.lap_delta_ms) == (fp2.id, True, -567)


def test_update_feedback_tags_tool(conn, push):
    feedback = push[3]
    engineer, client = make(conn, push, [
        reply(tool("update_feedback_tags", {"feedback_id": feedback.id, "symptom": "front_push_entry",
                                            "phase": "entry"}, id="a"),
              tool("update_feedback_tags", {"feedback_id": feedback.id, "symptom": "nonsense"}, id="b"),
              stop="tool_use"),
        reply(text("ok")),
    ], ScriptedIO())
    engineer.run("go")
    results = tool_results(client.requests[1])
    assert len(client.requests[1]["messages"][-1]["content"]) == 2  # both results in one message
    assert "is_error" not in results["a"] and results["b"]["is_error"]
    assert repo.get_feedback(conn, feedback.id).symptom == "front_push_entry"


def test_search_and_effects_tools(conn, push):
    bike, track, fp1, _ = push
    change = repo.change_setup(conn, bike.id, [parse_assignment("fork.rebound=-2")], session_id=fp1.id,
                               symptom="front_push_mid", hypothesis="hold the front down")
    repo.add_outcome(conn, change.id, "better")
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2")
    client = FakeClient([
        reply(tool("search_history", {"query": "front slippery", "same_track": True}, id="s"),
              tool("get_learned_effects", {"symptom": "front_push_mid"}, id="e"),
              tool("lookup_symptom_guide", {"description": "front tucks at turn in"}, id="g"),
              tool("get_setup_history", {}, id="h"), stop="tool_use"),
        reply(text("ok")),
    ])
    Engineer(conn, client, ScriptedIO(), bike_id=bike.id, session_id=fp2.id).run("go")
    results = {k: json.loads(v["content"]) for k, v in tool_results(client.requests[1]).items()}
    assert any("slippery" in h["text"] for h in results["s"])
    assert results["e"][0]["param"] == "fork.rebound" and results["e"][0]["better"] == 1
    assert results["g"][0]["id"] == "front_push_entry"
    assert results["h"][0]["results"][0]["verdict"] == "better"


def test_refusal_is_not_added_to_history(conn, push):
    io = ScriptedIO(answers=["something else", "q"])
    engineer, client = make(conn, push, [
        reply(text(""), stop="refusal"),
        reply(text("Sure.")),
    ], io)
    engineer.run("go")
    assert "declined" in io.notices[0]
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "user"]


def test_truncated_tool_call_is_not_run(conn, push):
    bike = push[0]
    io = ScriptedIO()
    engineer, _ = make(conn, push, [
        reply(tool("propose_changes", {"changes": [{"param": "fork.rebound", "step": -2}]}), stop="max_tokens"),
    ], io)
    engineer.run("go")
    assert "cut off" in io.notices[0]
    assert repo.list_changes(conn, bike_id=bike.id) == []


def test_connection_loss_raises_offline_and_keeps_transcript(conn, push):
    engineer, _ = make(conn, push, [
        reply(tool("get_session_context", {}), stop="tool_use"),
        anthropic.APIConnectionError(request=None),
    ], ScriptedIO())
    with pytest.raises(EngineerOffline):
        engineer.run(engineer.opening_message())
    saved = json.loads(conn.execute("SELECT messages FROM engineer_conversation").fetchone()["messages"])
    assert [m["role"] for m in saved] == ["user", "assistant", "user"]


def test_missing_credentials_is_a_clear_error(conn, push):
    from race_engineer.agent.engineer import EngineerUnavailable
    engineer, _ = make(conn, push, [TypeError('"Could not resolve authentication method. Expected one of api_key"')],
                       ScriptedIO())
    with pytest.raises(EngineerUnavailable, match="ANTHROPIC_API_KEY"):
        engineer.run("go")
    # Nothing saved, so the feedback still counts as undiscussed.
    assert conn.execute("SELECT COUNT(*) FROM engineer_conversation").fetchone()[0] == 0
    assert repo.latest_undiscussed_feedback(conn, push[2].id).id == push[3].id


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        assert t["description"] and t["input_schema"]["type"] == "object"
        assert t["input_schema"]["additionalProperties"] is False


def test_record_hot_pressures_tool(conn, push):
    _, _, session, _ = push  # front cold 2.1
    conn.execute("UPDATE session SET tire_front = 'SC1' WHERE id = ?", (session.id,))
    repo.set_tire_target(conn, "SC1", "front", 2.3, 2.5)
    io = ScriptedIO()
    engineer, client = make(conn, push, [
        reply(tool("record_hot_pressures", {"front": "28psi"}, id="bad"), stop="tool_use"),
        reply(tool("record_hot_pressures", {"front": "36.5psi"}, id="good"), stop="tool_use"),
        reply(tool("get_session_context", {}, id="ctx"), stop="tool_use"),
        reply(text("ok")),
    ], io)
    engineer.run("go")
    bad = tool_results(client.requests[1])["bad"]
    assert bad["is_error"] and "not above the 2.1 bar cold" in bad["content"]
    good = json.loads(tool_results(client.requests[2])["good"]["content"])
    assert good["tire_pressures_bar"]["front"] == {"cold": 2.1, "hot": 2.52, "hot_target": [2.3, 2.5]}
    assert io.notices == [f"Saved hot pressures for session #{session.id}: front 2.52 bar."]
    context = json.loads(tool_results(client.requests[3])["ctx"]["content"])
    assert context["session"]["tire_pressures_bar"]["front"]["hot"] == 2.52


def test_log_rider_change_tool(conn, push):
    bike, track, fp1, _ = push
    fp2 = repo.start_session(conn, bike.id, track.id, type="FP2", date=fp1.date)
    client = FakeClient([
        reply(tool("log_rider_change", {"changes": [{"param": "tire.front_cold", "value": "32psi"}],
                                        "already_on_bike_this_session": True, "why": "felt vague"}, id="a"),
              stop="tool_use"),
        reply(tool("get_setup_history", {}, id="h"), stop="tool_use"),
        reply(text("ok")),
    ])
    io = ScriptedIO(confirms=[True])
    Engineer(conn, client, io, bike_id=bike.id, session_id=fp2.id).run("go")
    assert io.questions[0] == "Record your change tire.front_cold 2.1 -> 2.21 (+0.11) (already on the bike this session)?"
    result = json.loads(tool_results(client.requests[1])["a"]["content"])
    assert result["status"] == "recorded" and result["made_after_session_id"] == fp1.id
    change = repo.get_change(conn, result["change_id"])
    assert change.proposed_by == "rider" and repo.get_session(conn, fp2.id).setup_id == change.after_setup_id
    history = json.loads(tool_results(client.requests[2])["h"]["content"])
    assert history[0]["made_after_session"] == {"session_id": fp1.id, "date": fp1.date, "type": "FP1"}


def test_log_rider_change_declined(conn, push):
    bike = push[0]
    engineer, client = make(conn, push, [
        reply(tool("log_rider_change", {"changes": [{"param": "fork.rebound", "step": -1}],
                                        "already_on_bike_this_session": False}, id="a"), stop="tool_use"),
        reply(text("ok")),
    ], ScriptedIO(confirms=[False]))
    engineer.run("go")
    assert json.loads(tool_results(client.requests[1])["a"]["content"])["status"] == "not recorded"
    assert repo.list_changes(conn, bike_id=bike.id) == []
