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

"""Privacy-preserving client profile access for the financial agent.

The authenticated web gateway performs Salesforce access before creating the
ADK session. The model receives only the two allowlisted profile values. In
anonymous mode this tool requests manual non-PII profile input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from google.adk.tools import ToolContext
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ACCOUNT_ID_STATE_KEY = "sfdc_account_id"
ACCESS_MODE_STATE_KEY = "access_mode"
SALESFORCE_ACCESS_MODE = "salesforce"
RISK_PROFILE_STATE_KEY = "client_risk_profile"
INVESTMENT_GOALS_STATE_KEY = "client_investment_goals"
PROFILE_SOURCE_STATE_KEY = "client_profile_source"

DEFAULT_MCP_URL = (
    "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads"
)
DEFAULT_RISK_PROFILE_FIELD = "RiskProfile_c"
DEFAULT_INVESTMENT_GOALS_FIELD = "InvestmentGoals_c"
SALESFORCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


@dataclass(frozen=True)
class ClientProfile:
    """The non-PII Account fields approved for strategy personalization."""

    risk_profile: str | None
    investment_goals: str | None


class ClientProfileGateway(Protocol):
    """Boundary for retrieving an allowlisted client profile."""

    async def get_account_profile(self, account_id: str) -> ClientProfile:
        """Retrieve the approved profile fields for one Salesforce Account."""


class SalesforceMcpClientProfileGateway:
    """Calls Salesforce's read-only hosted MCP server with a fixed SOQL query."""

    def __init__(
        self,
        *,
        mcp_url: str,
        access_token: str,
        risk_profile_field: str = DEFAULT_RISK_PROFILE_FIELD,
        investment_goals_field: str = DEFAULT_INVESTMENT_GOALS_FIELD,
    ) -> None:
        self._mcp_url = mcp_url
        self._access_token = access_token
        self._risk_profile_field = _validate_field_api_name(risk_profile_field)
        self._investment_goals_field = _validate_field_api_name(
            investment_goals_field
        )

    async def get_account_profile(self, account_id: str) -> ClientProfile:
        """Retrieve only risk profile and investment goals from Account."""
        query = _build_account_profile_query(
            account_id,
            risk_profile_field=self._risk_profile_field,
            investment_goals_field=self._investment_goals_field,
        )
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json, text/event-stream",
        }

        async with streamablehttp_client(
            self._mcp_url, headers=headers
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "soqlQuery", arguments={"query": query}
                )

        if result.isError:
            raise RuntimeError("Salesforce MCP rejected the profile query")

        payload = _extract_mcp_payload(result)
        record = _find_first_record(payload)
        if record is None:
            raise LookupError("No accessible Salesforce Account profile found")

        return ClientProfile(
            risk_profile=_optional_string(record.get(self._risk_profile_field)),
            investment_goals=_optional_string(
                record.get(self._investment_goals_field)
            ),
        )


async def load_current_client_profile(tool_context: ToolContext) -> dict[str, Any]:
    """Load the selected client's non-PII investment profile.

    Salesforce profile values must be injected by the authenticated gateway.
    Direct and anonymous launches ask for risk profile and investment goals.

    Returns:
        A status for the coordinator. The Account ID is deliberately omitted.
    """
    state = tool_context.state
    if state.get(ACCESS_MODE_STATE_KEY) != SALESFORCE_ACCESS_MODE:
        state[PROFILE_SOURCE_STATE_KEY] = "manual"
        return {
            "status": "manual_input_required",
            "missing_fields": ["risk_profile", "investment_goals"],
            "message": (
                "This session has no authorized Salesforce context. Ask the "
                "user for their risk profile and investment goals."
            ),
        }

    if state.get(PROFILE_SOURCE_STATE_KEY) == "salesforce":
        missing_fields = []
        if not state.get(RISK_PROFILE_STATE_KEY):
            missing_fields.append("risk_profile")
        if not state.get(INVESTMENT_GOALS_STATE_KEY):
            missing_fields.append("investment_goals")
        return {
            "status": "success" if not missing_fields else "partial",
            "profile_source": "salesforce",
            "risk_profile": state.get(RISK_PROFILE_STATE_KEY),
            "investment_goals": state.get(INVESTMENT_GOALS_STATE_KEY),
            "missing_fields": missing_fields,
        }

    return {
        "status": "profile_unavailable",
        "missing_fields": ["risk_profile", "investment_goals"],
        "message": (
            "The authenticated gateway did not provide an approved client "
            "profile. Do not attempt Salesforce access from the agent."
        ),
    }


def _validate_account_id(account_id: str) -> None:
    if not SALESFORCE_ID_PATTERN.fullmatch(account_id):
        raise ValueError("Invalid Salesforce Account ID")


def _validate_field_api_name(field_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field_name):
        raise ValueError("Invalid Salesforce field API name")
    return field_name


def _build_account_profile_query(
    account_id: str,
    *,
    risk_profile_field: str,
    investment_goals_field: str,
) -> str:
    """Build the only SOQL shape this integration permits."""
    _validate_account_id(account_id)
    risk_field = _validate_field_api_name(risk_profile_field)
    goals_field = _validate_field_api_name(investment_goals_field)
    return (
        f"SELECT {risk_field}, {goals_field} "
        "FROM Account "
        f"WHERE Id = '{account_id}' LIMIT 1"
    )


def _extract_mcp_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    for content_item in getattr(result, "content", []):
        text = getattr(content_item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise RuntimeError("Salesforce MCP returned an unsupported response")


def _find_first_record(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        records = payload.get("records")
        if isinstance(records, list) and records and isinstance(records[0], Mapping):
            return records[0]
        for value in payload.values():
            record = _find_first_record(value)
            if record is not None:
                return record
    elif isinstance(payload, list):
        for value in payload:
            record = _find_first_record(value)
            if record is not None:
                return record
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None