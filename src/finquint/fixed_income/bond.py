from dataclasses import dataclass


@dataclass(frozen=True)
class Bond:
    face_value: float
    coupon_rate: float
    maturity_years: float
    frequency: int = 2

    def __post_init__(self) -> None:
        if self.face_value <= 0:
            raise ValueError("face_value must be positive")
        if self.coupon_rate < 0:
            raise ValueError("coupon_rate cannot be negative")
        if self.maturity_years <= 0:
            raise ValueError("maturity_years must be positive")
        if self.frequency <= 0:
            raise ValueError("frequency must be positive")
        periods = self.maturity_years * self.frequency
        if abs(periods - round(periods)) > 1e-10:
            raise ValueError("maturity_years must contain a whole number of coupon periods")

    @property
    def periods(self) -> int:
        return round(self.maturity_years * self.frequency)

    @property
    def coupon_payment(self) -> float:
        return self.face_value * self.coupon_rate / self.frequency

