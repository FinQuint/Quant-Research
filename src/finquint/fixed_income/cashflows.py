from .bond import Bond


def cash_flows(bond: Bond) -> list[tuple[float, float]]:
    flows = []
    for period in range(1, bond.periods + 1):
        amount = bond.coupon_payment
        if period == bond.periods:
            amount += bond.face_value
        flows.append((period / bond.frequency, amount))
    return flows

def generate_cashflows(bond: Bond) -> list[float]:
    """Return payment amounts using the original Phase 1 API.

    Kept for backward compatibility. New code can use ``cash_flows``
    when both payment times and amounts are required.
    """
    return [amount for _, amount in cash_flows(bond)]
