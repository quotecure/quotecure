"""Thin REST client for GoHighLevel's v2 API (services.leadconnectorhq.com).

No retry/backoff -- every function either returns a value or raises GHLError on any
non-2xx response. Callers in app.py decide what to do with a failure (the convention
there is best-effort: wrap the whole sync in one try/except and log, never let a GHL
problem block or fail the actual QuoteCure action it's attached to).

Every function takes db and reads the token/locationId out of company_settings itself,
the same way _send_quote_email() threads Gmail creds through -- there's no module-level
singleton or app-context global here.
"""
import requests

_BASE = 'https://services.leadconnectorhq.com'
_API_VERSION = '2021-07-28'
_TIMEOUT = 15


class GHLError(Exception):
    pass


def _creds(db):
    row = db.execute("SELECT ghl_api_token, ghl_location_id FROM company_settings WHERE id=1").fetchone()
    token = (row['ghl_api_token'] if row else '') or ''
    location_id = (row['ghl_location_id'] if row else '') or ''
    if not token or not location_id:
        raise GHLError('GHL is not configured (missing API token or Location ID in Admin Settings).')
    return token, location_id


def _headers(token, json_body=True):
    h = {'Authorization': f'Bearer {token}', 'Version': _API_VERSION}
    if json_body:
        h['Content-Type'] = 'application/json'
    return h


def _raise_for_status(resp):
    if resp.status_code >= 300:
        raise GHLError(f'GHL API {resp.status_code}: {resp.text[:300]}')


# ── Contacts ─────────────────────────────────────────────────────────────────
def find_contact_by_email(db, email):
    """Returns the first matching contact dict, or None. GHL's contact search index is
    eventually consistent -- a contact created moments ago by this same process may not
    show up yet. That's an accepted edge case here (see _ensure_ghl_contact in app.py):
    worst case is an occasional duplicate Contact, not a functional break."""
    token, location_id = _creds(db)
    resp = requests.post(
        f'{_BASE}/contacts/search',
        headers=_headers(token),
        json={'locationId': location_id, 'filters': [{'field': 'email', 'operator': 'eq', 'value': email}], 'pageLimit': 1},
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    contacts = resp.json().get('contacts') or []
    return contacts[0] if contacts else None


def create_contact(db, name, email, phone):
    token, location_id = _creds(db)
    resp = requests.post(
        f'{_BASE}/contacts/',
        headers=_headers(token),
        json={'locationId': location_id, 'name': name, 'email': email or None, 'phone': phone or None},
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    return resp.json()['contact']['id']


# ── Opportunities ────────────────────────────────────────────────────────────
def create_opportunity(db, contact_id, pipeline_id, stage_id, name, monetary_value, status='open'):
    token, location_id = _creds(db)
    resp = requests.post(
        f'{_BASE}/opportunities/',
        headers=_headers(token),
        json={
            'locationId': location_id, 'contactId': contact_id, 'pipelineId': pipeline_id,
            'pipelineStageId': stage_id, 'name': name, 'monetaryValue': monetary_value, 'status': status,
        },
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    return resp.json()['opportunity']['id']


def update_opportunity(db, opportunity_id, stage_id, monetary_value=None, status=None):
    """status is GHL's separate won/lost/open/abandoned field -- moving the stage alone
    doesn't close the deal in GHL's own reporting, both need to be set together."""
    token, _ = _creds(db)
    body = {'pipelineStageId': stage_id}
    if monetary_value is not None:
        body['monetaryValue'] = monetary_value
    if status:
        body['status'] = status
    resp = requests.put(f'{_BASE}/opportunities/{opportunity_id}', headers=_headers(token), json=body, timeout=_TIMEOUT)
    _raise_for_status(resp)
    return resp.json()['opportunity']['id']


# ── Notes ────────────────────────────────────────────────────────────────────
def add_note(db, contact_id, body_text):
    token, _ = _creds(db)
    resp = requests.post(
        f'{_BASE}/contacts/{contact_id}/notes',
        headers=_headers(token), json={'body': body_text}, timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    return resp.json()['note']['id']


# ── Conversation file attachment ─────────────────────────────────────────────
def attach_file_to_conversation(db, contact_id, filename, mime_type, file_bytes):
    """Requires the token's Private Integration to have the Conversations scope enabled
    (Contacts/Opportunities/Notes scopes do NOT cover this -- confirmed live: this call
    401s with 'not authorized for this scope' until Jim adds it in GHL Settings ->
    Private Integrations). Best-effort caller in app.py already tolerates this failing."""
    token, location_id = _creds(db)
    resp = requests.post(
        f'{_BASE}/conversations/messages/upload',
        headers=_headers(token, json_body=False),
        data={'locationId': location_id, 'contactId': contact_id},
        files={'fileAttachment': (filename, file_bytes, mime_type)},
        timeout=_TIMEOUT,
    )
    _raise_for_status(resp)
    return resp.json()
