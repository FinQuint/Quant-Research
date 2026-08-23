from .bond import Bond


def cash_flows(bond: Bond) -> list[tuple[float, float]]:
    flows = []
    for period in range(1, bond.periods + 1):
        amount = bond.coupon_payment
        if period == bond.periods:
            amount += bond.face_value
        flows.append((period / bond.frequency, amount))
    return flows

