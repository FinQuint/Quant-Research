def generate_cashflows(bond):
    cashflows = [bond.coupon_payment] * bond.periods
    cashflows[-1] += bond.face_value
    return cashflows
