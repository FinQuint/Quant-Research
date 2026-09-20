import pytest

from finquint.models import CIRModel, VasicekModel, calibrate_cir, calibrate_vasicek


def test_vasicek_zero_coupon_and_reproducible_exact_paths():
    model = VasicekModel(0.7, 0.04, 0.015)
    assert model.zero_coupon_price(0.03, 0) == 1
    assert 0 < model.zero_coupon_price(0.03, 5) < 1
    first = model.simulate(0.03, 12, 1 / 12, paths=3, seed=42)
    assert first == model.simulate(0.03, 12, 1 / 12, paths=3, seed=42)
    assert len(first) == 3 and len(first[0]) == 13


def test_cir_prices_and_full_truncation_paths_are_nonnegative():
    model = CIRModel(1.2, 0.04, 0.18)
    assert model.feller_satisfied
    assert model.zero_coupon_price(0.03, 0) == 1
    assert 0 < model.zero_coupon_price(0.03, 10) < 1
    paths = model.simulate(0.03, 200, 1 / 252, paths=4, seed=9)
    assert min(min(path) for path in paths) >= 0


def test_vasicek_calibration_recovers_simulated_dynamics():
    source = VasicekModel(0.8, 0.035, 0.012)
    observations = source.simulate(0.03, 6000, 1 / 252, seed=17)[0]
    fitted = calibrate_vasicek(observations, 1 / 252)
    # Mean-reversion speed is noisy and upward-biased in finite daily samples.
    assert fitted.mean_reversion == pytest.approx(source.mean_reversion, rel=0.9)
    assert fitted.long_run_rate == pytest.approx(source.long_run_rate, abs=0.008)
    assert fitted.volatility == pytest.approx(source.volatility, rel=0.08)


def test_cir_calibration_returns_valid_model():
    source = CIRModel(1.1, 0.045, 0.12)
    observations = source.simulate(0.04, 8000, 1 / 252, seed=31)[0]
    fitted = calibrate_cir(observations, 1 / 252)
    assert fitted.mean_reversion > 0
    assert fitted.long_run_rate == pytest.approx(source.long_run_rate, abs=0.015)
    assert fitted.volatility == pytest.approx(source.volatility, rel=0.12)


def test_model_and_simulation_validation():
    with pytest.raises(ValueError):
        VasicekModel(0, 0.03, 0.01)
    with pytest.raises(ValueError):
        CIRModel(1, -0.01, 0.1)
    with pytest.raises(ValueError):
        CIRModel(1, 0.03, 0.1).simulate(-0.01, 10, 0.1)
    with pytest.raises(ValueError):
        calibrate_vasicek((0.03, 0.03, 0.03), 1)
