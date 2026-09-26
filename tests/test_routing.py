from factory.agents.agents import FailureTracker, TaskContext, pick_builder, pick_tier
from factory.config import load_profile

PROFILE = load_profile(mode="fullstack")


def ctx(domain=(), risk=(), failures=0, budget=10**6):
    return TaskContext(set(domain), set(risk), failures, budget)


def test_generalist_by_default():
    assert pick_builder(ctx(domain={"security"}), PROFILE) == "builder"


def test_specialist_on_risk_tag():
    assert pick_builder(ctx(domain={"security"}, risk={"auth"}), PROFILE) == "specialist:security"


def test_specialist_on_repeated_failure():
    assert pick_builder(ctx(domain={"database"}, failures=2), PROFILE) == "specialist:database"


def test_specialists_off_in_simple_mode():
    assert pick_builder(ctx(domain={"security"}, risk={"auth"}), load_profile(mode="simple")) == "builder"


def test_no_specialist_when_budget_low():
    assert pick_builder(ctx(domain={"security"}, risk={"auth"}, budget=100), PROFILE) == "builder"


def test_tier_escalates():
    assert pick_tier("builder", 0, PROFILE) == "cheap"
    assert pick_tier("builder", 3, PROFILE) == "strong"
    assert pick_tier("specialist:security", 0, PROFILE) == "strong"
    assert pick_tier("specialist:database", 0, PROFILE) == "mid"


def test_failure_tracker():
    t = FailureTracker()
    assert t.record("T1", ["a", "b"]) == 1
    assert t.record("T1", ["a"]) == 2
    assert t.max_repeat("T1") == 2 and t.max_repeat("T2") == 0
