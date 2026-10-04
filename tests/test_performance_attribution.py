import pandas as pd
import pytest

from finquint.performance import aggregate_attribution, gross_to_net_attribution


def test_group_contributions_reconcile_to_portfolio_return():
    contributions = pd.DataFrame({
        "timestamp": [1, 1, 2, 2],
        "group": ["rates", "credit", "rates", "credit"],
        "contribution": [0.006, 0.004, -0.002, 0.012],
    })
    report = aggregate_attribution(contributions, [0.01, 0.01])
    assert report.reconciled
    assert report.residual == pytest.approx(0)
    assert report.contributions.set_index("group").loc["credit", "total_contribution"] == pytest.approx(0.016)


def test_attribution_exposes_reconciliation_residual():
    report = aggregate_attribution(
        pd.DataFrame({"group": ["rates"], "contribution": [0.01]}), [0.012]
    )
    assert not report.reconciled
    assert report.residual == pytest.approx(0.002)


def test_gross_to_net_bridge_reconciles_each_period():
    result = gross_to_net_attribution(
        [0.01, 0.02], [0.001, 0.002], [0.0002, 0.0002], [0.0, 0.0005]
    )
    expected = result["gross_return"] - result["transaction_cost"] - result["financing_cost"] - result["borrow_cost"]
    assert result["net_return"].tolist() == pytest.approx(expected.tolist())
    assert (result["net_return"] <= result["gross_return"]).all()


def test_cost_bridge_rejects_negative_costs_and_length_mismatch():
    with pytest.raises(ValueError, match="nonnegative"):
        gross_to_net_attribution([0.01, 0.02], [0.001, -0.001])
    with pytest.raises(ValueError, match="equal length"):
        gross_to_net_attribution([0.01, 0.02], [0.001])
