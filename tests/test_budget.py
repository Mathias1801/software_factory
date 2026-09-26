import pytest

from factory.budget import BudgetExceeded, TokenLedger


def test_record_and_check():
    ledger = TokenLedger({"build": 1000}, warn_at=0.8)
    ledger.record("build", 300, 200)
    assert ledger.check("build") == "ok"
    assert ledger.check("build", 350) == "warn"
    with pytest.raises(BudgetExceeded):
        ledger.check("build", 600)


def test_unbudgeted_stage_is_ok():
    assert TokenLedger({}).check("anything", 10**9) == "ok"


def test_extend_and_roundtrip():
    ledger = TokenLedger({"qa": 100})
    ledger.record("qa", 50, 10)
    assert ledger.extend("qa", 1.5, at_least=200) == 200
    other = TokenLedger({"qa": 100})
    other.load(ledger.to_dict())
    assert other.budgets["qa"] == 200 and other.used["qa"] == 60
