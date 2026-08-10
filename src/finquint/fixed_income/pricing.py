from finquint.analytics.numerical import present_value
from .cashflows import generate_cashflows

def price_bond(bond, annual_yield):
    return present_value(generate_cashflows(bond), annual_yield / bond.frequency)
