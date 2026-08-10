def present_value(cashflows, periodic_rate):
    if periodic_rate <= -1:
        raise ValueError("periodic_rate must be greater than -1")
    return sum(cf / (1 + periodic_rate) ** t for t, cf in enumerate(cashflows, 1))
