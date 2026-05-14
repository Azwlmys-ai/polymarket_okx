import pytest

from src.safety import SafetyBoundaryError, SafetyFlags


def test_phase_one_defaults_are_safe():
    SafetyFlags().enforce_phase_one()


@pytest.mark.parametrize(
    "flag",
    [
        "allow_real_trading",
        "allow_private_keys",
        "allow_withdrawals",
        "allow_browser_automation",
    ],
)
def test_phase_one_rejects_forbidden_capabilities(flag):
    flags = SafetyFlags(**{flag: True})

    with pytest.raises(SafetyBoundaryError):
        flags.enforce_phase_one()

