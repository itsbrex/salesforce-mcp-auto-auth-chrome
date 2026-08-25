"""Typed read-only Salesforce operations executed inside the user's browser."""

# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import quote, urlparse

from .browser_contracts import (
    validate_activity_payload,
    validate_ownership_payload,
    validate_ownership_term,
    validate_pipeline_payload,
    validate_salesforce_id,
)
from .instance import normalize_host

API_VERSION = "v60.0"
Runner = Callable[[Sequence[str], float], str]


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
        binary: str | None = None,
        pace_seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.pin_browser = pin_browser
        self.pin_profile = pin_profile
        self.browsers = browsers
        self.profiles = profiles
        self.runner = runner or _run_command
        self.binary = _resolve_binary(binary)
        self.lightning_url = _lightning_url(self.instance_url)
        self.pace_seconds = (
            (0 if runner is not None else 0.9) if pace_seconds is None else pace_seconds
        )
        if (
            isinstance(self.pace_seconds, bool)
            or not isinstance(self.pace_seconds, (int, float))
            or not 0 <= self.pace_seconds <= 5
        ):
            raise ValueError("pace_seconds must be from 0 through 5")
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_browser_operation_at: float | None = None
        self._active_session: tuple[str, str, str, str] | None = None
        self._managed_session_lock = RLock()

    def search_ownership(self, term: str) -> list[dict[str, str]]:
        """Search fixed ownership fields through Salesforce's native search page."""
        term = validate_ownership_term(term)
        with self._managed_session() as (profile, session, target, original_url):
            try:
                url = (
                    f"{self.instance_url}/_ui/search/ui/UnifiedSearchResults"
                    f"?searchType=2&str={quote(term, safe='')}"
                )
                target = self._navigate(profile, session, target, url)
                payload = self._eval(profile, session, target, _OWNERSHIP_JS)
            finally:
                self._restore_tab(profile, session, target, original_url)
        return validate_ownership_payload(payload)

    def session_is_active(self) -> bool:
        """Validate current session through browser-owned same-origin fetch."""
        # Opening a session already asserts identity, but a *cached* one was
        # asserted at some earlier point and may since have died, so re-check
        # it here rather than reporting "active" on the strength of a handle.
        with self._managed_session() as (profile, session, target, _original_url):
            self._assert_identity(profile, session, target)
            return True

    def close(self) -> None:
        """Close one process-owned background tab, if opened."""
        self._discard_active_session()

    def _discard_active_session(self) -> None:
        """Forget the cached session and best-effort close its tab."""
        with self._managed_session_lock:
            active = self._active_session
            self._active_session = None
            if active is None:
                return
            profile, session, _target, _home_url = active
            with suppress(BrowserBridgeError):
                self._command(profile, session, ["close"], timeout=10)

    def get_account_pipeline(self, account_id: str) -> list[dict[str, Any]]:
        """Return fixed standard opportunity fields for one Account."""
        account_id = validate_salesforce_id(account_id, "001", field_name="account_id")
        with self._managed_session() as (profile, session, target, _original_url):
            script = _PIPELINE_JS.replace("__ACCOUNT_ID__", json.dumps(account_id))
            payload = self._eval(profile, session, target, script)
        return validate_pipeline_payload(payload)

    def get_account_activities(
        self, account_id: str, *, limit: int = 25
    ) -> dict[str, list[dict[str, str]]]:
        """Return open and historical activities from native related-list pages."""
        account_id = validate_salesforce_id(account_id, "001", field_name="account_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 25
        ):
            raise ValueError("limit must be an integer from 1 through 25")

        with self._managed_session() as (profile, session, target, original_url):
            try:
                open_payload = self._related_list(
                    profile,
                    session,
                    target,
                    account_id,
                    "OpenActivities",
                    _OPEN_ACTIVITY_JS,
                    limit,
                )
                history_payload = self._related_list(
                    profile,
                    session,
                    target,
                    account_id,
                    "ActivityHistories",
                    _ACTIVITY_HISTORY_JS,
                    limit,
                )
            finally:
                self._restore_tab(profile, session, target, original_url)
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
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 25
        ):
            raise ValueError("limit must be an integer from 1 through 25")

        with self._managed_session() as (profile, session, target, original_url):
            pipeline_by_account = self._account_pipeline_batch(
                profile, session, target, parsed_ids
            )
            contexts: list[dict[str, Any]] = []
            try:
                for account_id in parsed_ids:
                    open_payload = self._related_list(
                        profile,
                        session,
                        target,
                        account_id,
                        "OpenActivities",
                        _OPEN_ACTIVITY_JS,
                        limit,
                    )
                    history_payload = self._related_list(
                        profile,
                        session,
                        target,
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
            finally:
                self._restore_tab(profile, session, target, original_url)
        return contexts

    @contextmanager
    def _managed_session(self) -> Iterator[tuple[str, str, str, str]]:
        with self._managed_session_lock:
            yield from self._managed_session_unlocked()

    def _managed_session_unlocked(self) -> Iterator[tuple[str, str, str, str]]:
        if self._active_session is not None:
            # A cached session can die outside this process — the user closes
            # the tab, the browser discards it under memory pressure, the
            # OpenCLI daemon restarts. Dropping the cache on failure is what
            # keeps that from being permanent: this call still reports the
            # error, but the next one opens a fresh session instead of
            # replaying the same dead handle forever.
            try:
                yield self._active_session
            except BaseException:
                self._discard_active_session()
                raise
            return
        profile = self._resolve_opencli_profile()
        session = f"salesforce-mcp-{os.getpid()}-{secrets.token_hex(4)}"
        opened = False
        home_url = f"{self.lightning_url}/lightning/page/home"
        try:
            payload = self._command(
                profile,
                session,
                ["open", home_url, "--window", "background"],
                timeout=45,
            )
            target = _target_from_open(payload)
            opened = True
            self._assert_identity(profile, session, target)
            self._active_session = (profile, session, target, home_url)
            yield self._active_session
        except BaseException:
            self._active_session = None
            if opened:
                with suppress(BrowserBridgeError):
                    self._command(profile, session, ["close"], timeout=10)
            raise

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
            # The cookie session is pinned to a specific browser. Require the
            # bridge profile to be that same browser — never fall back to an
            # arbitrary lone connected profile. The identity check downstream
            # only confirms the org host and a 005-shaped user ID, not *which*
            # user, so a bridge belonging to another browser or Salesforce user
            # would otherwise be accepted and reads would run under the wrong
            # principal. Set SALESFORCE_OPENCLI_PROFILE to override explicitly.
            matches = [
                item
                for item in connected
                if item[1].casefold() == self.pin_browser.casefold()
            ]
        else:
            matches = connected
        if len(matches) != 1:
            raise BrowserBridgeError(
                "Salesforce browser profile is unavailable or ambiguous"
            )
        return matches[0][0]

    def _assert_identity(self, profile: str, session: str, target: str) -> None:
        payload = self._eval(profile, session, target, _IDENTITY_JS)
        if not isinstance(payload, dict):
            raise BrowserBridgeError("Salesforce browser identity check failed")
        host = payload.get("host")
        user_id = payload.get("userId")
        expected_host = urlparse(self.instance_url).hostname or ""
        if (
            not isinstance(host, str)
            or normalize_host(host) != normalize_host(expected_host)
            or not isinstance(user_id, str)
            or not re.fullmatch(r"005[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?", user_id)
        ):
            raise BrowserBridgeError("Salesforce browser identity does not match")

    def _related_list(
        self,
        profile: str,
        session: str,
        target: str,
        account_id: str,
        related_list: str,
        script: str,
        limit: int,
    ) -> object:
        url = (
            f"{self.lightning_url}/lightning/r/Account/{account_id}/related/"
            f"{related_list}/view"
        )
        target = self._navigate(profile, session, target, url)
        expression = script.replace("__LIMIT__", str(limit))
        return self._eval(profile, session, target, expression)

    def _navigate(
        self, profile: str, session: str, target: str, url: str
    ) -> str:
        active = self._active_session
        if active is not None and active[0] == profile and active[1] == session:
            target = active[2]
        payload = self._command(
            profile, session, ["open", url, "--tab", target], timeout=45
        )
        next_target = _target_from_open(payload)
        active = self._active_session
        if active is not None and active[0] == profile and active[1] == session:
            self._active_session = (profile, session, next_target, active[3])
        return next_target

    def _account_pipeline_batch(
        self, profile: str, session: str, target: str, account_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        pipelines: dict[str, list[dict[str, Any]]] = {}
        for account_id in account_ids:
            script = _PIPELINE_JS.replace("__ACCOUNT_ID__", json.dumps(account_id))
            records = validate_pipeline_payload(
                self._eval(profile, session, target, script)
            )
            if any(record["accountId"][:15] != account_id[:15] for record in records):
                raise RuntimeError(
                    "Salesforce browser contract drift: batch account changed"
                )
            pipelines[account_id] = records
        return pipelines

    def _eval(self, profile: str, session: str, target: str, script: str) -> object:
        return self._command(
            profile, session, ["eval", script, "--tab", target], timeout=35
        )

    def _restore_tab(
        self, profile: str, session: str, target: str, original_url: str
    ) -> None:
        with suppress(BrowserBridgeError):
            self._navigate(profile, session, target, original_url)

    def _command(
        self,
        profile: str,
        session: str,
        args: list[str],
        *,
        timeout: float,
    ) -> object:
        if args and args[0] in {"open", "eval"}:
            self._pace_browser_operation()
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
            raise BrowserBridgeError(
                "OpenCLI browser bridge returned invalid data"
            ) from error

    def _pace_browser_operation(self) -> None:
        now = self.monotonic()
        if self._last_browser_operation_at is not None:
            remaining = self.pace_seconds - (now - self._last_browser_operation_at)
            if remaining > 0:
                self.sleep(remaining)
                now = self.monotonic()
        self._last_browser_operation_at = now

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


def _lightning_url(instance_url: str) -> str:
    parsed = urlparse(instance_url)
    host = parsed.hostname or ""
    suffix = ".my.salesforce.com"
    if not host.endswith(suffix):
        raise BrowserBridgeError("Salesforce instance host is unsupported")
    return f"https://{host[: -len(suffix)]}.lightning.force.com"


def _target_from_open(payload: object) -> str:
    if not isinstance(payload, dict):
        raise BrowserBridgeError("Salesforce browser target is unavailable")
    target = payload.get("page") or payload.get("targetId") or payload.get("id")
    if (
        not isinstance(target, str)
        or not target
        or len(target) > 200
        or any(character.isspace() for character in target)
    ):
        raise BrowserBridgeError("Salesforce browser target is unavailable")
    return target


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

_OWNERSHIP_JS = r"""
(async()=>{
  const prefixes={'001':'Account','003':'Contact','00Q':'Lead'};
  const actionLabels=new Set(['edit','del','change owner']);
  const clean=value=>(value||'').trim();
  const recordMatch=href=>(href||'').match(/(001|003|00Q)[A-Za-z0-9]{12,15}/);
  // Classic action links (Edit / Del) embed the record ID too, but their path
  // keeps going after it ("/001.../e"); a detail link's path ends at the ID
  // (optionally "/view" in Lightning-shaped hrefs). Detecting tables and rows
  // by that link shape instead of translated header text keeps the search
  // working in every Salesforce UI language.
  const detailMatch=anchor=>{
    const href=anchor.getAttribute('href')||'';
    let path=href;
    try{path=new URL(href,location.origin).pathname;}catch(error){}
    return path.match(/\/((001|003|00Q)[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?)(?:\/view)?$/);
  };
  const sleep=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
  let tables=[];
  for(let attempt=0;attempt<40;attempt++){
    tables=[...document.querySelectorAll('table')];
    if(tables.some(table=>[...table.querySelectorAll('a[href]')].some(anchor=>recordMatch(anchor.getAttribute('href')))))break;
    await sleep(250);
  }
  const records=[];
  for(const table of tables){
    const headerRow=[...table.querySelectorAll('tr')].find(row=>row.querySelector(':scope > th'));
    if(!headerRow)continue;
    const headers=[...headerRow.querySelectorAll(':scope > th, :scope > td')].map(cell=>clean(cell.textContent));
    const counts={'001':0,'003':0,'00Q':0};
    for(const anchor of table.querySelectorAll('a[href]')){
      const match=detailMatch(anchor);
      if(match)counts[match[2]]+=1;
    }
    // A Contact grid links each row's Contact and often its Account, so ties
    // resolve toward the more specific object (003, then 00Q, then 001).
    const primaryPrefix=['003','00Q','001'].reduce((best,prefix)=>counts[prefix]>counts[best]?prefix:best,'003');
    if(!counts[primaryPrefix])continue;
    const rows=[...table.querySelectorAll(':scope > tbody > tr, :scope > tr')];
    for(const row of rows){
      if(row===headerRow)continue;
      const cells=[...row.querySelectorAll(':scope > th, :scope > td')];
      const anchors=cells.flatMap(cell=>[...cell.querySelectorAll('a[href]')]);
      const primaryAnchor=anchors.find(anchor=>detailMatch(anchor)?.[2]===primaryPrefix&&!actionLabels.has(clean(anchor.textContent).toLowerCase()));
      if(!primaryAnchor)continue;
      const idMatch=detailMatch(primaryAnchor);
      if(!idMatch)continue;
      const valueFor=pattern=>{
        const index=headers.findIndex(header=>pattern.test(header));
        return index>=0&&index<cells.length?clean(cells[index].textContent):'';
      };
      const mailtoAnchor=anchors.find(anchor=>(anchor.getAttribute('href')||'').toLowerCase().startsWith('mailto:'));
      const accountAnchor=primaryPrefix==='003'?anchors.find(anchor=>detailMatch(anchor)?.[2]==='001'):null;
      records.push({
        type:prefixes[primaryPrefix],
        id:idMatch[1],
        name:clean(primaryAnchor.textContent).slice(0,240),
        owner:valueFor(/owner/i).slice(0,120),
        email:(valueFor(/e-?mail/i)||(mailtoAnchor?clean(mailtoAnchor.textContent):'')).slice(0,320),
        website:valueFor(/website/i).slice(0,2048),
        accountId:accountAnchor?(detailMatch(accountAnchor)?.[1]||''):'',
        company:primaryPrefix==='00Q'?valueFor(/^company$/i).slice(0,240):''
      });
      if(records.length>=60)break;
    }
    if(records.length>=60)break;
  }
  const unique=[...new Map(records.map(record=>[record.id,record])).values()];
  return {sourceType:'classic_search_page',totalSize:unique.length,records:unique};
})()
""".strip()

_PIPELINE_JS = f"""
(async()=>{{
  const accountId=__ACCOUNT_ID__;
  const standardFields=['Opportunity.Id','Opportunity.Name','Opportunity.StageName','Opportunity.CloseDate','Opportunity.Owner.Name','Opportunity.Amount','Opportunity.ExpectedRevenue','Opportunity.Probability','Opportunity.Type','Opportunity.AccountId'];
  const customFields=['Opportunity.Size__c','Opportunity.Size_Type__c','Opportunity.Term_Months__c','Opportunity.Lease_Type__c'];
  const baseFor=fieldList=>'/services/data/{API_VERSION}/ui-api/related-list-records/'+accountId+'/Opportunities?fields='+encodeURIComponent(fieldList.join(','))+'&pageSize=200';
  const fetchPage=(base,token)=>fetch(token==null?base:base+'&pageToken='+encodeURIComponent(token),{{
    headers:{{Accept:'application/json'}},credentials:'same-origin'
  }});
  // The four CRE custom fields only exist in some orgs, and the UI API rejects
  // the whole request when any requested field is invalid. Probe with the full
  // list once and drop the custom fields on a 400 so the standard pipeline
  // fields still come back from every org.
  let base=baseFor(standardFields.concat(customFields));
  let response=await fetchPage(base,null);
  if(response.status===400){{
    base=baseFor(standardFields);
    response=await fetchPage(base,null);
  }}
  // Follow nextPageToken so Accounts with more than one page of Opportunities
  // return complete data instead of tripping the contract's done/count checks.
  // Bounded at 50 pages (10k opportunities) as a runaway guard.
  let raw=[],sourceKeys=null,token=null;
  for(let page=0;page<50;page++){{
    if(page>0)response=await fetchPage(base,token);
    if(!response.ok)return {{error:'request_failed',status:response.status}};
    const payload=await response.json();
    if(sourceKeys==null)sourceKeys=Object.keys(payload).sort();
    raw=raw.concat(payload.records||[]);
    token=payload.nextPageToken;
    if(token==null)break;
  }}
  return {{
    sourceKeys:sourceKeys||[],
    done:token==null,totalSize:raw.length,
    records:raw.map(record=>({{
      id:record.id||record.fields?.Id?.value||'',
      accountId:record.fields?.AccountId?.value||'',
      name:record.fields?.Name?.displayValue||record.fields?.Name?.value||'',
      stage:record.fields?.StageName?.displayValue||record.fields?.StageName?.value||'',
      closeDate:record.fields?.CloseDate?.value||record.fields?.CloseDate?.displayValue||'',
      owner:record.fields?.Owner?.displayValue||'',
      amount:record.fields?.Amount?.value??null,
      expectedRevenue:record.fields?.ExpectedRevenue?.value??null,
      probability:record.fields?.Probability?.value??null,
      size:record.fields?.Size__c?.value??null,
      sizeType:record.fields?.Size_Type__c?.displayValue||record.fields?.Size_Type__c?.value||'',
      term:record.fields?.Term_Months__c?.value??null,
      leaseType:record.fields?.Lease_Type__c?.displayValue||record.fields?.Lease_Type__c?.value||'',
      type:record.fields?.Type?.displayValue||record.fields?.Type?.value||''
    }}))
  }};
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
