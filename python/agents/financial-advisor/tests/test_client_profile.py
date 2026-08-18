# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for privacy-preserving Salesforce client profile loading."""

from dataclasses import dataclass
from typing import Any

import pytest

from financial_advisor import client_profile


@dataclass
class FakeToolContext:
    state: dict[str, Any]


@pytest.mark.asyncio
async def test_direct_launch_requests_manual_non_pii_profile() -> None:
    context = FakeToolContext(state={"access_mode": "anonymous"})

    result = await client_profile.load_current_client_profile(
        context  # type: ignore[arg-type]
    )

    assert result["status"] == "manual_input_required"
    assert result["missing_fields"] == ["risk_profile", "investment_goals"]
    assert context.state[client_profile.PROFILE_SOURCE_STATE_KEY] == "manual"
    assert client_profile.ACCOUNT_ID_STATE_KEY not in str(result)


@pytest.mark.asyncio
async def test_salesforce_launch_loads_only_profile_fields() -> None:
    account_id = "001000000000001AAA"
    context = FakeToolContext(
        state={
            client_profile.ACCESS_MODE_STATE_KEY: "salesforce",
            client_profile.ACCOUNT_ID_STATE_KEY: account_id,
            client_profile.PROFILE_SOURCE_STATE_KEY: "salesforce",
            client_profile.RISK_PROFILE_STATE_KEY: "Moderate",
            client_profile.INVESTMENT_GOALS_STATE_KEY: (
                "Long-term retirement growth"
            ),
        }
    )

    result = await client_profile.load_current_client_profile(
        context  # type: ignore[arg-type]
    )

    assert result == {
        "status": "success",
        "profile_source": "salesforce",
        "risk_profile": "Moderate",
        "investment_goals": "Long-term retirement growth",
        "missing_fields": [],
    }
    assert account_id not in str(result)
    assert context.state[client_profile.RISK_PROFILE_STATE_KEY] == "Moderate"
    assert (
        context.state[client_profile.INVESTMENT_GOALS_STATE_KEY]
        == "Long-term retirement growth"
    )


@pytest.mark.asyncio
async def test_partial_salesforce_profile_requests_only_missing_value() -> None:
    context = FakeToolContext(
        state={
            client_profile.ACCESS_MODE_STATE_KEY: "salesforce",
            client_profile.ACCOUNT_ID_STATE_KEY: "001000000000001AAA",
            client_profile.PROFILE_SOURCE_STATE_KEY: "salesforce",
            client_profile.RISK_PROFILE_STATE_KEY: "Conservative",
            client_profile.INVESTMENT_GOALS_STATE_KEY: None,
        }
    )

    result = await client_profile.load_current_client_profile(
        context  # type: ignore[arg-type]
    )

    assert result["status"] == "partial"
    assert result["missing_fields"] == ["investment_goals"]


@pytest.mark.asyncio
async def test_invalid_account_id_fails_closed_without_calling_gateway() -> None:
    context = FakeToolContext(
        state={
            client_profile.ACCESS_MODE_STATE_KEY: "anonymous",
            client_profile.ACCOUNT_ID_STATE_KEY: "not-an-account-id' OR 1=1",
        }
    )

    result = await client_profile.load_current_client_profile(
        context  # type: ignore[arg-type]
    )

    assert result["status"] == "manual_input_required"
    assert "not-an-account" not in str(result)


def test_query_is_fixed_to_account_and_allowlisted_fields() -> None:
    query = client_profile._build_account_profile_query(
        "001000000000001AAA",
        risk_profile_field="RiskProfile_c",
        investment_goals_field="InvestmentGoals_c",
    )

    assert query == (
        "SELECT RiskProfile_c, InvestmentGoals_c FROM Account "
        "WHERE Id = '001000000000001AAA' LIMIT 1"
    )
    assert "Name" not in query
    assert "Email" not in query
    assert "Phone" not in query


@pytest.mark.parametrize(
    "account_id",
    ["", "001-short", "001000000000001'", "001000000000001AAA OR 1=1"],
)
def test_query_rejects_invalid_account_ids(account_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid Salesforce Account ID"):
        client_profile._build_account_profile_query(
            account_id,
            risk_profile_field="RiskProfile_c",
            investment_goals_field="InvestmentGoals_c",
        )