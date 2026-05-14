from __future__ import annotations

from dataclasses import dataclass


class SafetyBoundaryError(RuntimeError):
    """Raised when a forbidden phase-1 capability is enabled."""


@dataclass(frozen=True)
class SafetyFlags:
    allow_real_trading: bool = False
    allow_private_keys: bool = False
    allow_withdrawals: bool = False
    allow_browser_automation: bool = False

    def enforce_phase_one(self) -> None:
        violations = []
        if self.allow_real_trading:
            violations.append("real trading")
        if self.allow_private_keys:
            violations.append("private key handling")
        if self.allow_withdrawals:
            violations.append("withdrawals")
        if self.allow_browser_automation:
            violations.append("browser automation")

        if violations:
            raise SafetyBoundaryError(
                "Phase 1 forbids: " + ", ".join(violations)
            )

