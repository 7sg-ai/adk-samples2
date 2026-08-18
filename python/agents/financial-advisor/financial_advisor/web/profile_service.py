"""Authenticated Salesforce Account profile service."""

from __future__ import annotations

import os
from typing import Protocol

from financial_advisor.client_profile import (
    ClientProfile,
    SalesforceMcpClientProfileGateway,
)


class ProfileService(Protocol):
    async def get_profile(
        self, *, account_id: str, access_token: str
    ) -> ClientProfile: ...


class SalesforceProfileService:
    def __init__(self, mcp_url: str) -> None:
        self._mcp_url = mcp_url

    async def get_profile(
        self, *, account_id: str, access_token: str
    ) -> ClientProfile:
        return await SalesforceMcpClientProfileGateway(
            mcp_url=self._mcp_url,
            access_token=access_token,
            risk_profile_field=os.getenv(
                "SFDC_RISK_PROFILE_FIELD", "RiskProfile_c"
            ),
            investment_goals_field=os.getenv(
                "SFDC_INVESTMENT_GOALS_FIELD", "InvestmentGoals_c"
            ),
        ).get_account_profile(account_id)