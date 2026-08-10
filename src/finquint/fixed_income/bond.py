from dataclasses import dataclass

@dataclass(frozen=True)
class Bond:
    face_value: float
    coupon_rate: float
    maturity_years: float
    frequency: int = 2

    def __post_init__(self):
        if self.face_value <= 0 or self.maturity_years <= 0 or self.frequency <= 0:
            raise ValueError("face value, maturity and frequency must be positive")
        if not float(self.maturity_years * self.frequency).is_integer():
            raise ValueError("maturity * frequency must be a whole number")

    @property
    def coupon_payment(self):
        return self.face_value * self.coupon_rate / self.frequency

    @property
    def periods(self):
        return int(self.maturity_years * self.frequency)
