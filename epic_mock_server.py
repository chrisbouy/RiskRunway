"""
Local Epic API Mock Server

Mimics Applied Epic's REST API and SDK Module with realistic surplus lines data.
Run: python epic_mock_server.py
Then set EPIC_BASE_URL=http://localhost:5002 in .env

Supports:
- POST /v1/auth/connect/token (always returns a token)
- GET  /epic/client/v1/clients (search by name)
- GET  /epic/client/v1/clients/<id>
- GET  /epic/policy/v2/policies (filter by client, status)
- GET  /epic/policy/v2/policies/<id>
- GET  /epic/policy/v2/lines (filter by policy)
- PUT  /sdk/v1/policies (update policy)
- PUT  /sdk/v1/lines (update line)
- POST /epic/attachment/v2/attachments (create attachment)
"""
from flask import Flask, request, jsonify
import uuid
from datetime import datetime

app = Flask(__name__)

# ─── MOCK DATA ───────────────────────────────────────────────

CLIENTS = [
    {
        "id": "c001-aaaa-bbbb-cccc-111111111111",
        "name": "Martinez Roofing Inc",
        "lookupCode": "MARTROOF-01",
        "type": "INSURED",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["4521 Industrial Blvd"],
            "city": "Fort Lauderdale",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33309",
            "countryCode": "USA"
        }
    },
    {
        "id": "c002-aaaa-bbbb-cccc-222222222222",
        "name": "Sunshine Hospitality Group LLC",
        "lookupCode": "SUNHOSP-01",
        "type": "INSURED",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["8900 Collins Ave", "Suite 300"],
            "city": "Miami Beach",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33140",
            "countryCode": "USA"
        }
    },
    {
        "id": "c003-aaaa-bbbb-cccc-333333333333",
        "name": "Atlantic Waste Solutions",
        "lookupCode": "ATLWASTE-01",
        "type": "INSURED",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["1200 NW 78th Ave"],
            "city": "Doral",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33126",
            "countryCode": "USA"
        }
    },
    {
        "id": "c004-aaaa-bbbb-cccc-444444444444",
        "name": "Premier Auto Transport LLC",
        "lookupCode": "PREMAUT-01",
        "type": "INSURED",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["3300 SW 42nd St"],
            "city": "Hollywood",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33312",
            "countryCode": "USA"
        }
    },
    {
        "id": "c005-aaaa-bbbb-cccc-555555555555",
        "name": "Coastal Development Partners",
        "lookupCode": "COASTDEV-01",
        "type": "PROSPECT",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["200 S Biscayne Blvd", "Floor 28"],
            "city": "Miami",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33131",
            "countryCode": "USA"
        }
    },
]

POLICIES = [
    # Martinez Roofing - GL (prospective, needs marketing)
    {
        "id": "p001-1111-2222-3333-444444444444",
        "client": "c001-aaaa-bbbb-cccc-111111111111",
        "description": "General Liability - Roofing",
        "policyNumber": "",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-gl-001",
        "organization": "org-001",
    },
    # Martinez Roofing - Workers Comp (prospective)
    {
        "id": "p002-1111-2222-3333-555555555555",
        "client": "c001-aaaa-bbbb-cccc-111111111111",
        "description": "Workers Compensation - Roofing Crews",
        "policyNumber": "",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-wc-001",
        "organization": "org-001",
    },
    # Sunshine Hospitality - GL + Liquor (prospective)
    {
        "id": "p003-1111-2222-3333-666666666666",
        "client": "c002-aaaa-bbbb-cccc-222222222222",
        "description": "General Liability - Hotels & Restaurants",
        "policyNumber": "",
        "effectiveOn": "2026-09-15",
        "expirationOn": "2027-09-15",
        "status": "PROSPECTIVE",
        "policyType": "pt-gl-001",
        "organization": "org-001",
    },
    # Sunshine Hospitality - Property (prospective)
    {
        "id": "p004-1111-2222-3333-777777777777",
        "client": "c002-aaaa-bbbb-cccc-222222222222",
        "description": "Commercial Property - Multiple Locations",
        "policyNumber": "",
        "effectiveOn": "2026-09-15",
        "expirationOn": "2027-09-15",
        "status": "PROSPECTIVE",
        "policyType": "pt-cp-001",
        "organization": "org-001",
    },
    # Atlantic Waste - Commercial Auto (prospective)
    {
        "id": "p005-1111-2222-3333-888888888888",
        "client": "c003-aaaa-bbbb-cccc-333333333333",
        "description": "Commercial Auto - Waste Haulers",
        "policyNumber": "",
        "effectiveOn": "2026-07-01",
        "expirationOn": "2027-07-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-ca-001",
        "organization": "org-001",
    },
    # Atlantic Waste - GL (already contracted/bound)
    {
        "id": "p006-1111-2222-3333-999999999999",
        "client": "c003-aaaa-bbbb-cccc-333333333333",
        "description": "General Liability - Waste Operations",
        "policyNumber": "GL-2025-AWA-001",
        "effectiveOn": "2025-07-01",
        "expirationOn": "2026-07-01",
        "status": "CONTRACTED",
        "policyType": "pt-gl-001",
        "organization": "org-001",
    },
    # Premier Auto Transport - Commercial Auto (prospective)
    {
        "id": "p007-1111-2222-3333-aaaaaaaaaaaa",
        "client": "c004-aaaa-bbbb-cccc-444444444444",
        "description": "Commercial Auto - Transport Fleet",
        "policyNumber": "",
        "effectiveOn": "2026-10-01",
        "expirationOn": "2027-10-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-ca-001",
        "organization": "org-001",
    },
    # Coastal Development - Builders Risk (prospective)
    {
        "id": "p008-1111-2222-3333-bbbbbbbbbbbb",
        "client": "c005-aaaa-bbbb-cccc-555555555555",
        "description": "Builders Risk - Oceanfront Condo Project",
        "policyNumber": "",
        "effectiveOn": "2026-11-01",
        "expirationOn": "2028-05-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-br-001",
        "organization": "org-001",
    },
]

POLICY_TYPES = {
    "pt-gl-001": {"id": "pt-gl-001", "code": "GL", "description": "General Liability", "businessType": "COMMERCIAL"},
    "pt-wc-001": {"id": "pt-wc-001", "code": "WC", "description": "Workers Compensation", "businessType": "COMMERCIAL"},
    "pt-cp-001": {"id": "pt-cp-001", "code": "CP", "description": "Commercial Property", "businessType": "COMMERCIAL"},
    "pt-ca-001": {"id": "pt-ca-001", "code": "CA", "description": "Commercial Auto", "businessType": "COMMERCIAL"},
    "pt-br-001": {"id": "pt-br-001", "code": "BR", "description": "Builders Risk", "businessType": "COMMERCIAL"},
}

LINES = [
    # Martinez Roofing GL line
    {
        "id": "l001-aaaa-bbbb-cccc-111111111111",
        "policy": "p001-1111-2222-3333-444444444444",
        "client": "c001-aaaa-bbbb-cccc-111111111111",
        "lineType": "pt-gl-001",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Martinez Roofing WC line
    {
        "id": "l002-aaaa-bbbb-cccc-222222222222",
        "policy": "p002-1111-2222-3333-555555555555",
        "client": "c001-aaaa-bbbb-cccc-111111111111",
        "lineType": "pt-wc-001",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Sunshine GL line
    {
        "id": "l003-aaaa-bbbb-cccc-333333333333",
        "policy": "p003-1111-2222-3333-666666666666",
        "client": "c002-aaaa-bbbb-cccc-222222222222",
        "lineType": "pt-gl-001",
        "effectiveOn": "2026-09-15",
        "expirationOn": "2027-09-15",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Sunshine Property line
    {
        "id": "l004-aaaa-bbbb-cccc-444444444444",
        "policy": "p004-1111-2222-3333-777777777777",
        "client": "c002-aaaa-bbbb-cccc-222222222222",
        "lineType": "pt-cp-001",
        "effectiveOn": "2026-09-15",
        "expirationOn": "2027-09-15",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Atlantic Waste Auto line
    {
        "id": "l005-aaaa-bbbb-cccc-555555555555",
        "policy": "p005-1111-2222-3333-888888888888",
        "client": "c003-aaaa-bbbb-cccc-333333333333",
        "lineType": "pt-ca-001",
        "effectiveOn": "2026-07-01",
        "expirationOn": "2027-07-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Atlantic Waste GL line (bound)
    {
        "id": "l006-aaaa-bbbb-cccc-666666666666",
        "policy": "p006-1111-2222-3333-999999999999",
        "client": "c003-aaaa-bbbb-cccc-333333333333",
        "lineType": "pt-gl-001",
        "effectiveOn": "2025-07-01",
        "expirationOn": "2026-07-01",
        "stage": "ISSUED",
        "billMode": "AGENCY",
        "issuingCompany": {"id": "ic-001", "name": "Nautilus Insurance", "lookupCode": "NAUT01"},
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Premier Auto line
    {
        "id": "l007-aaaa-bbbb-cccc-777777777777",
        "policy": "p007-1111-2222-3333-aaaaaaaaaaaa",
        "client": "c004-aaaa-bbbb-cccc-444444444444",
        "lineType": "pt-ca-001",
        "effectiveOn": "2026-10-01",
        "expirationOn": "2027-10-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Coastal Dev Builders Risk line
    {
        "id": "l008-aaaa-bbbb-cccc-888888888888",
        "policy": "p008-1111-2222-3333-bbbbbbbbbbbb",
        "client": "c005-aaaa-bbbb-cccc-555555555555",
        "lineType": "pt-br-001",
        "effectiveOn": "2026-11-01",
        "expirationOn": "2028-05-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
]

# Track updates for demo purposes
updated_lines = {}
updated_policies = {}
created_attachments = []


# ─── AUTH ─────────────────────────────────────────────────────

@app.route('/v1/auth/connect/token', methods=['POST'])
def token():
    """Always return a valid token."""
    return jsonify({
        "access_token": "mock-token-" + str(uuid.uuid4())[:8],
        "token_type": "Bearer",
        "expires_in": 3600,
    })


# ─── CLIENTS ─────────────────────────────────────────────────

@app.route('/epic/client/v1/clients', methods=['GET'])
def get_clients():
    """Search clients by name."""
    name_filter = (request.args.get('name_contains') or '').lower()
    results = [c for c in CLIENTS if name_filter in c['name'].lower()]

    limit = int(request.args.get('limit', 100))
    results = results[:limit]

    return jsonify({
        "total": len(results),
        "_embedded": {"clients": results},
        "_links": {"self": {"href": request.url}}
    })


@app.route('/epic/client/v1/clients/<client_id>', methods=['GET'])
def get_client(client_id):
    """Get client by ID."""
    client = next((c for c in CLIENTS if c['id'] == client_id), None)
    if not client:
        return jsonify({"title": "Not Found", "status": 404}), 404
    return jsonify(client)


# ─── POLICIES ────────────────────────────────────────────────

@app.route('/epic/policy/v2/policies', methods=['GET'])
def get_policies():
    """Get policies with optional client and status filters."""
    client_filter = request.args.get('client')
    status_filter = request.args.get('status')

    results = POLICIES[:]

    if client_filter:
        results = [p for p in results if p['client'] == client_filter]
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(',')]
        results = [p for p in results if p['status'] in statuses]

    # Embed policy type and client data
    embedded_results = []
    for p in results:
        entry = dict(p)
        ptype = POLICY_TYPES.get(p.get('policyType'), {})
        client = next((c for c in CLIENTS if c['id'] == p['client']), {})
        entry['_embedded'] = {
            'policyType': ptype,
            'client': {
                'id': client.get('id'),
                'name': client.get('name'),
                'lookupCode': client.get('lookupCode'),
            } if client else {}
        }
        embedded_results.append(entry)

    return jsonify({
        "total": len(embedded_results),
        "_embedded": {"policies": embedded_results},
        "_links": {"self": {"href": request.url}}
    })


@app.route('/epic/policy/v2/policies/<policy_id>', methods=['GET'])
def get_policy(policy_id):
    """Get single policy by ID."""
    policy = next((p for p in POLICIES if p['id'] == policy_id), None)
    if not policy:
        return jsonify({"title": "Not Found", "status": 404}), 404

    result = dict(policy)
    ptype = POLICY_TYPES.get(policy.get('policyType'), {})
    client = next((c for c in CLIENTS if c['id'] == policy['client']), {})
    result['_embedded'] = {
        'policyType': ptype,
        'client': {'id': client.get('id'), 'name': client.get('name')} if client else {}
    }
    return jsonify(result)


# ─── LINES ───────────────────────────────────────────────────

@app.route('/epic/policy/v2/lines', methods=['GET'])
def get_lines():
    """Get lines with optional policy filter."""
    policy_filter = request.args.get('policy')

    results = LINES[:]
    if policy_filter:
        results = [l for l in results if l['policy'] == policy_filter]

    # Embed line type and issuing company
    embedded_results = []
    for line in results:
        entry = dict(line)
        ltype = POLICY_TYPES.get(line.get('lineType'), {})
        ic = line.get('issuingCompany') or {}
        entry['_embedded'] = {
            'lineType': ltype,
            'issuingCompany': ic if ic else {},
        }
        embedded_results.append(entry)

    return jsonify({
        "total": len(embedded_results),
        "_embedded": {"lines": embedded_results},
        "_links": {"self": {"href": request.url}}
    })


# ─── SDK MODULE (Updates) ────────────────────────────────────

@app.route('/sdk/v1/lines', methods=['PUT'])
def update_line():
    """Update a line (SDK module)."""
    data = request.get_json() or {}
    line_id = data.get('LineID')
    updated_lines[str(line_id)] = data
    print(f"[MOCK] Line updated: {line_id} -> {list(data.keys())}")
    return jsonify({"Envelope": {"Body": {"Update_LineResponse": {}}}})


@app.route('/sdk/v1/policies', methods=['PUT'])
def update_policy():
    """Update a policy (SDK module)."""
    data = request.get_json() or {}
    policy_id = data.get('PolicyID')
    updated_policies[str(policy_id)] = data
    print(f"[MOCK] Policy updated: {policy_id} -> {list(data.keys())}")
    return jsonify({"Envelope": {"Body": {"Update_PolicyResponse": {}}}})


# ─── ATTACHMENTS ─────────────────────────────────────────────

@app.route('/epic/attachment/v2/attachments', methods=['POST'])
def create_attachment():
    """Create an attachment and return an upload URL."""
    data = request.get_json() or {}
    attachment_id = str(uuid.uuid4())
    upload_url = f"http://localhost:5002/mock-upload/{attachment_id}"

    record = {
        "id": attachment_id,
        "description": data.get('description', ''),
        "uploadFileName": data.get('uploadFileName', ''),
        "attachTo": data.get('attachTo', {}),
        "createdAt": datetime.utcnow().isoformat(),
    }
    created_attachments.append(record)
    print(f"[MOCK] Attachment created: {data.get('uploadFileName')} -> {attachment_id}")

    return jsonify({
        "id": attachment_id,
        "description": data.get('description', ''),
        "active": True,
        "uploadUrl": upload_url,
        "file": {"id": attachment_id, "status": "PENDING"},
        "_links": {"self": {"href": f"/epic/attachment/v2/attachments/{attachment_id}"}}
    }), 201


@app.route('/mock-upload/<attachment_id>', methods=['PUT'])
def mock_upload(attachment_id):
    """Accept file upload (just acknowledge it)."""
    content_length = request.content_length or 0
    print(f"[MOCK] File uploaded for attachment {attachment_id}: {content_length} bytes")
    return '', 204


# ─── DEBUG ───────────────────────────────────────────────────

@app.route('/mock-status', methods=['GET'])
def mock_status():
    """Show what's been updated/created during this session."""
    return jsonify({
        "updated_lines": updated_lines,
        "updated_policies": updated_policies,
        "created_attachments": created_attachments,
    })


if __name__ == '__main__':
    print("=" * 50)
    print("Epic Mock Server running on http://localhost:5002")
    print("Set EPIC_BASE_URL=http://localhost:5002 in .env")
    print("=" * 50)
    print(f"\nClients: {len(CLIENTS)}")
    for c in CLIENTS:
        print(f"  • {c['name']} ({c['lookupCode']})")
    print(f"\nPolicies: {len(POLICIES)} ({sum(1 for p in POLICIES if p['status'] == 'PROSPECTIVE')} prospective)")
    print(f"Lines: {len(LINES)}")
    print()
    app.run(port=5002, debug=True)
