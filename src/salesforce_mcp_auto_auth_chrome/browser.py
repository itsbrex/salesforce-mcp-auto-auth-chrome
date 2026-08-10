"""Typed read-only Salesforce operations executed inside the user's browser."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .browser_contracts import (
    validate_activity_payload,
    validate_pipeline_payload,
    validate_salesforce_id,
)
from .cookies import read_sid
from .instance import normalize_host

API_VERSION = "v60.0"

Runner = Callable[[Sequence[str], float], str]
SidReader = Callable[..., str | None]
IdentityReader = Callable[[str, str], str]


class BrowserBridgeError(RuntimeError):
    """Sanitized browser bridge failure with no command output or CRM values."""


class SalesforceBrowser:
    """Run fixed Salesforce reads in a managed background browser tab."""

    def __init__(
        self,
        instance_url: str,
        *,
        pin_browser: str | None,
        pin_profile: str | None,
        browsers: list[str] | None = None,
        profiles: list[str] | None = None,
        runner: Runner | None = None,
        sid_reader: SidReader = read_sid,
        identity_reader: IdentityReader | None = None,
        binary: str | None = None,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.pin_browser = pin_browser
        self.pin_profile = pin_profile
        self.browsers = browsers
        self.profiles = profiles
        self.runner = runner or _run_command
        self.sid_reader = sid_reader
        self.identity_reader = identity_reader or _read_user_id
        self.binary = _resolve_binary(binary)
        self.lightning_url = _lightning_url(self.instance_url)

    def get_account_pipeline(self, account_id: str) -> list[dict[str, Any]]:
        """Return fixed standard opportunity fields for one Account."""
        account_id = validate_salesforce_id(
            account_id, "001", field_name="account_id"
        )
        with self._managed_session() as (profile, session):
            script = _PIPELINE_JS.replace("__ACCOUNT_ID__", json.dumps(account_id))
            payload = self._eval(profile, session, script)
        return validate_pipeline_payload(payload)

    def get_account_activities(
        self, account_id: str, *, limit: int = 25
    ) -> dict[str, list[dict[str, str]]]:
        """Return open and historical activities from native related-list pages."""
        account_id = validate_salesforce_id(
            account_id, "001", field_name="account_id"
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25")

        with self._managed_session() as (profile, session):
            open_payload = self._related_list(
                profile, session, account_id, "OpenActivities", _OPEN_ACTIVITY_JS, limit
            )
            history_payload = self._related_list(
                profile,
                session,
                account_id,
                "ActivityHistories",
                _ACTIVITY_HISTORY_JS,
                limit,
            )
        return {
            "upcoming": validate_activity_payload(open_payload, "open"),
            "recent": validate_activity_payload(history_payload, "history"),
        }

    def get_accounts_context(
        self, account_ids: list[str], *, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Return pipeline and activities for up to ten Accounts in one tab."""
        if not isinstance(account_ids, list) or not 1 <= len(account_ids) <= 10:
            raise ValueError("account_ids must contain 1 through 10 Account IDs")
        parsed_ids = [
            validate_salesforce_id(value, "001", field_name="account_id")
            for value in account_ids
        ]
        if len(set(parsed_ids)) != len(parsed_ids):
            raise ValueError("account_ids must be unique")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be an integer from 1 through 25")

        with self._managed_session() as (profile, session):
            pipeline_by_account = self._account_pipeline_batch(
                profile, session, parsed_ids
            )
            contexts: list[dict[str, Any]] = []
            for account_id in parsed_ids:
                open_payload = self._related_list(
                    profile,
                    session,
                    account_id,
                    "OpenActivities",
                    _OPEN_ACTIVITY_JS,
                    limit,
                )
                history_payload = self._related_list(
                    profile,
                    session,
                    account_id,
                    "ActivityHistories",
                    _ACTIVITY_HISTORY_JS,
                    limit,
                )
                contexts.append(
                    {
                        "accountId": account_id,
                        "opportunities": pipeline_by_account[account_id],
                        "upcoming": validate_activity_payload(open_payload, "open"),
                        "recent": validate_activity_payload(
                            history_payload, "history"
                        ),
                    }
                )
        return contexts

    @contextmanager
    def _managed_session(self) -> Iterator[tuple[str, str]]:
        profile = self._resolve_opencli_profile()
        sid = self._current_sid()
        session = f"salesforce-mcp-{os.getpid()}-{secrets.token_hex(4)}"
        opened = False
        try:
            self._command(
                profile,
                session,
                ["open", f"{self.lightning_url}/lightning/page/home", "--window", "background"],
                timeout=45,
            )
            opened = True
            self._assert_identity(profile, session, sid)
            yield profile, session
        finally:
            if opened:
                with suppress(BrowserBridgeError):
                    self._command(profile, session, ["close"], timeout=10)

    def _resolve_opencli_profile(self) -> str:
        binary = self.binary
        if not binary:
            raise BrowserBridgeError("OpenCLI browser bridge is unavailable")
        output = self.runner([binary, "profile", "list"], 10)
        connected: list[tuple[str, str]] = []
        for line in output.splitlines():
            if "not connected" in line.casefold():
                continue
            match = re.match(r"^\s*(\S+)\s+(\S+)\s+.*\bconnected\b", line, re.I)
            if match:
                connected.append((match.group(1), match.group(2)))

        configured = os.environ.get("SALESFORCE_OPENCLI_PROFILE")
        if configured:
            matches = [item for item in connected if item[0] == configured]
        elif self.pin_browser:
            matches = [
                item for item in connected if item[1].casefold() == self.pin_browser.casefold()
            ]
            if not matches and len(connected) == 1:
                matches = connected
        else:
            matches = connected
        if len(matches) != 1:
            raise BrowserBridgeError(
                "Salesforce browser profile is unavailable or ambiguous"
            )
        return matches[0][0]

    def _current_sid(self) -> str:
        if self.pin_browser and self.pin_profile:
            sid = self.sid_reader(
                self.instance_url, [self.pin_browser], [self.pin_profile]
            )
        else:
            sid = self.sid_reader(self.instance_url, self.browsers, self.profiles)
        if not sid:
            raise BrowserBridgeError("Salesforce browser session is unavailable")
        return sid

    def _assert_identity(self, profile: str, session: str, sid: str) -> None:
        expected_user_id = self.identity_reader(self.instance_url, sid)
        payload = self._eval(profile, session, _IDENTITY_JS)
        if not isinstance(payload, dict):
            raise BrowserBridgeError("Salesforce browser identity check failed")
        host = payload.get("host")
        user_id = payload.get("userId")
        expected_host = urlparse(self.instance_url).hostname or ""
        if (
            not isinstance(host, str)
            or normalize_host(host) != normalize_host(expected_host)
            or user_id != expected_user_id
        ):
            raise BrowserBridgeError("Salesforce browser identity does not match")

    def _related_list(
        self,
        profile: str,
        session: str,
        account_id: str,
        related_list: str,
        script: str,
        limit: int,
    ) -> object:
        url = (
            f"{self.lightning_url}/lightning/r/Account/{account_id}/related/"
            f"{related_list}/view"
        )
        self._command(profile, session, ["open", url], timeout=45)
        expression = script.replace("__LIMIT__", str(limit))
        return self._eval(profile, session, expression)

    def _account_pipeline_batch(
        self, profile: str, session: str, account_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        script = _PIPELINE_BATCH_JS.replace(
            "__ACCOUNT_IDS__", json.dumps(account_ids, separators=(",", ":"))
        )
        payload = self._eval(profile, session, script)
        if not isinstance(payload, list) or len(payload) != len(account_ids):
            raise RuntimeError(
                "Salesforce browser contract drift: batch pipeline count changed"
            )
        pipelines: dict[str, list[dict[str, Any]]] = {}
        for expected_account_id, item in zip(account_ids, payload, strict=True):
            if (
                not isinstance(item, dict)
                or set(item) != {"accountId", "pipeline"}
                or item.get("accountId") != expected_account_id
            ):
                raise RuntimeError(
                    "Salesforce browser contract drift: batch pipeline shape changed"
                )
            records = validate_pipeline_payload(item["pipeline"])
            if any(record["accountId"] != expected_account_id for record in records):
                raise RuntimeError(
                    "Salesforce browser contract drift: batch account changed"
                )
            pipelines[expected_account_id] = records
        return pipelines

    def _eval(self, profile: str, session: str, script: str) -> object:
        return self._command(profile, session, ["eval", script], timeout=35)

    def _command(
        self,
        profile: str,
        session: str,
        args: list[str],
        *,
        timeout: float,
    ) -> object:
        command = [
            self._required_binary(),
            "--profile",
            profile,
            "browser",
            session,
            *args,
        ]
        output = self.runner(command, timeout)
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise BrowserBridgeError("OpenCLI browser bridge returned invalid data") from error

    def _required_binary(self) -> str:
        if not self.binary:
            raise BrowserBridgeError("OpenCLI browser bridge is unavailable")
        return self.binary


def _run_command(args: Sequence[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise BrowserBridgeError("OpenCLI browser bridge command failed") from error
    return completed.stdout


def _resolve_binary(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    configured = os.environ.get("SALESFORCE_OPENCLI_BIN")
    if configured:
        return configured
    user_binary = Path.home() / "bin" / "opencli"
    if user_binary.is_file() and os.access(user_binary, os.X_OK):
        return str(user_binary)
    discovered = shutil.which("opencli")
    if discovered:
        return discovered
    return None


def _read_user_id(instance_url: str, sid: str) -> str:
    request = urllib.request.Request(
        f"{instance_url}/services/oauth2/userinfo",
        headers={"Accept": "application/json", "Authorization": f"Bearer {sid}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, ValueError) as error:
        raise BrowserBridgeError("Salesforce identity validation failed") from error
    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    if not isinstance(user_id, str):
        raise BrowserBridgeError("Salesforce identity validation failed")
    return user_id


def _lightning_url(instance_url: str) -> str:
    parsed = urlparse(instance_url)
    host = parsed.hostname or ""
    suffix = ".my.salesforce.com"
    if not host.endswith(suffix):
        raise BrowserBridgeError("Salesforce instance host is unsupported")
    return f"https://{host[: -len(suffix)]}.lightning.force.com"


_IDENTITY_JS = f"""
(async()=>{{
  const response=await fetch('/services/data/{API_VERSION}/chatter/users/me',{{
    headers:{{Accept:'application/json'}},credentials:'same-origin'
  }});
  if(!response.ok)return {{error:'request_failed',status:response.status}};
  const payload=await response.json();
  return {{host:location.hostname,userId:payload.id||''}};
}})()
""".strip()

_PIPELINE_JS = f"""
(async()=>{{
  const accountId=__ACCOUNT_ID__;
  const fields=['Opportunity.Id','Opportunity.Name','Opportunity.StageName','Opportunity.CloseDate','Opportunity.Owner.Name','Opportunity.Amount','Opportunity.ExpectedRevenue','Opportunity.Probability','Opportunity.Type','Opportunity.AccountId'].join(',');
  const path='/services/data/{API_VERSION}/ui-api/related-list-records/'+accountId+'/Opportunities?fields='+encodeURIComponent(fields)+'&pageSize=200';
  const response=await fetch(path,{{
    headers:{{Accept:'application/json'}},credentials:'same-origin'
  }});
  if(!response.ok)return {{error:'request_failed',status:response.status}};
  const payload=await response.json();
  return {{
    sourceKeys:Object.keys(payload).sort(),
    done:payload.nextPageToken==null,totalSize:payload.count,
    records:(payload.records||[]).map(record=>({{
      id:record.id||record.fields?.Id?.value||'',
      accountId:record.fields?.AccountId?.value||'',
      name:record.fields?.Name?.displayValue||record.fields?.Name?.value||'',
      stage:record.fields?.StageName?.displayValue||record.fields?.StageName?.value||'',
      closeDate:record.fields?.CloseDate?.value||record.fields?.CloseDate?.displayValue||'',
      owner:record.fields?.Owner?.displayValue||'',
      amount:record.fields?.Amount?.value,
      expectedRevenue:record.fields?.ExpectedRevenue?.value,
      probability:record.fields?.Probability?.value,
      type:record.fields?.Type?.displayValue||record.fields?.Type?.value||''
    }}))
  }};
}})()
""".strip()

_PIPELINE_BATCH_JS = f"""
(async()=>{{
  const accountIds=__ACCOUNT_IDS__;
  const fields=['Opportunity.Id','Opportunity.Name','Opportunity.StageName','Opportunity.CloseDate','Opportunity.Owner.Name','Opportunity.Amount','Opportunity.ExpectedRevenue','Opportunity.Probability','Opportunity.Type','Opportunity.AccountId'].join(',');
  return await Promise.all(accountIds.map(async accountId=>{{
    const path='/services/data/{API_VERSION}/ui-api/related-list-records/'+accountId+'/Opportunities?fields='+encodeURIComponent(fields)+'&pageSize=200';
    const response=await fetch(path,{{
      headers:{{Accept:'application/json'}},credentials:'same-origin'
    }});
    if(!response.ok)return {{accountId,pipeline:{{error:'request_failed',status:response.status}}}};
    const payload=await response.json();
    return {{accountId,pipeline:{{
      sourceKeys:Object.keys(payload).sort(),
      done:payload.nextPageToken==null,totalSize:payload.count,
      records:(payload.records||[]).map(record=>({{
        id:record.id||record.fields?.Id?.value||'',
        accountId:record.fields?.AccountId?.value||'',
        name:record.fields?.Name?.displayValue||record.fields?.Name?.value||'',
        stage:record.fields?.StageName?.displayValue||record.fields?.StageName?.value||'',
        closeDate:record.fields?.CloseDate?.value||record.fields?.CloseDate?.displayValue||'',
        owner:record.fields?.Owner?.displayValue||'',
        amount:record.fields?.Amount?.value,
        expectedRevenue:record.fields?.ExpectedRevenue?.value,
        probability:record.fields?.Probability?.value,
        type:record.fields?.Type?.displayValue||record.fields?.Type?.value||''
      }}))
    }}}};
  }}));
}})()
""".strip()

_OPEN_ACTIVITY_JS = r"""
(async()=>{
  const listName='OpenActivities',limit=__LIMIT__,sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let table=null;
  for(let attempt=0;attempt<40;attempt++){
    table=[...document.querySelectorAll('table')].find(candidate=>{
      const text=[...candidate.querySelectorAll('thead th')].map(x=>(x.innerText||x.textContent||'').trim().split('\n')[0]);
      return text.includes('Subject')&&text.includes('Assigned To');
    })||null;
    if(table||/No items to display\./i.test(document.body?.innerText||''))break;
    await sleep(250);
  }
  if(!table)return {empty:/No items to display\./i.test(document.body?.innerText||''),headers:[],records:[]};
  const clean=value=>(value||'').trim();
  const headers=[...table.querySelectorAll('thead th')].map(x=>clean(x.innerText||x.textContent).split('\n')[0]);
  const index=label=>headers.findIndex(value=>value===label);
  const records=[...table.querySelectorAll('tbody tr')].slice(0,limit).map(row=>{
    const cells=[...row.querySelectorAll(':scope > th,:scope > td')].map(cell=>clean(cell.innerText||cell.textContent));
    const get=label=>{const i=index(label);return i>=0?cells[i]:''};
    return {subject:get('Subject').slice(0,240),status:get('Status').slice(0,80),due:get('Due Date').slice(0,40),assignee:get('Assigned To').slice(0,120)};
  }).filter(record=>record.subject);
  return {empty:records.length===0,headers,records,listName};
})()
""".strip()

_ACTIVITY_HISTORY_JS = r"""
(async()=>{
  const listName='ActivityHistories',limit=__LIMIT__,sleep=ms=>new Promise(r=>setTimeout(r,ms));
  let table=null;
  for(let attempt=0;attempt<40;attempt++){
    table=[...document.querySelectorAll('table')].find(candidate=>{
      const text=[...candidate.querySelectorAll('thead th')].map(x=>(x.innerText||x.textContent||'').trim().split('\n')[0]);
      return text.includes('Subject')&&text.includes('Assigned To');
    })||null;
    if(table||/No items to display\./i.test(document.body?.innerText||''))break;
    await sleep(250);
  }
  if(!table)return {empty:/No items to display\./i.test(document.body?.innerText||''),headers:[],records:[]};
  const clean=value=>(value||'').trim();
  const headers=[...table.querySelectorAll('thead th')].map(x=>clean(x.innerText||x.textContent).split('\n')[0]);
  const index=label=>headers.findIndex(value=>value===label);
  const records=[...table.querySelectorAll('tbody tr')].slice(0,limit).map(row=>{
    const cells=[...row.querySelectorAll(':scope > th,:scope > td')].map(cell=>clean(cell.innerText||cell.textContent));
    const get=label=>{const i=index(label);return i>=0?cells[i]:''};
    return {subject:get('Subject').slice(0,240),date:(get('Completed Date')||get('Due Date')).slice(0,40),assignee:get('Assigned To').slice(0,120)};
  }).filter(record=>record.subject);
  return {empty:records.length===0,headers,records,listName};
})()
""".strip()
