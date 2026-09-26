from factory.config import deep_merge, load_profile
from factory.decisions import Decision, Reversibility, needs_approval

PROFILE = load_profile()


def make(**kw):
    base = dict(title="x", options=[], chosen="a", rationale="", reversibility=Reversibility.CHEAP)
    base.update(kw)
    return Decision(**base)


def test_cheap_implementation_is_autonomous():
    assert not needs_approval(make(), PROFILE)


def test_costly_needs_approval_unless_high_autonomy():
    d = make(reversibility=Reversibility.COSTLY)
    assert needs_approval(d, PROFILE)
    high = deep_merge(PROFILE, {"human": {"autonomy": "high"}})
    assert not needs_approval(d, high)


def test_scope_and_risk_always_ask():
    high = deep_merge(PROFILE, {"human": {"autonomy": "high"}})
    assert needs_approval(make(touches_spec_scope=True), high)
    assert needs_approval(make(risk_tags={"payments"}), high)


def test_architecture_category_asks():
    assert needs_approval(make(category="architecture"), PROFILE)
