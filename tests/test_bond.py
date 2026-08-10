import pytest
from finquint.fixed_income import Bond, generate_cashflows, price_bond

def test_cashflows():
    b=Bond(100, .05, 2, 2)
    assert generate_cashflows(b)==[2.5,2.5,2.5,102.5]

def test_par_bond():
    b=Bond(100,.05,5,2)
    assert price_bond(b,.05)==pytest.approx(100)
