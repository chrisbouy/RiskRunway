"""
Local Epic API Mock Server

Mimics Applied Epic's REST API and SDK Module with demo data for:
- Acme Manufacturing Corp (Prospect - Submission stage, has ACORD 125)
- Tree Frogs Adventure Park, LLC (Submitted - Quoting stage, has ACORD 125 + quote)

Run: python epic_mock_server.py
Then set EPIC_BASE_URL=http://localhost:5002 in .env
"""
from flask import Flask, request, jsonify, send_file
import uuid
import os
from datetime import datetime

app = Flask(__name__)

# Resolve sample_docs path relative to this file
SAMPLE_DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sample_docs')

# ─── MOCK DATA ───────────────────────────────────────────────

CLIENTS = [
    {
        "id": "c-acme-0001",
        "name": "Acme Manufacturing Corp",
        "lookupCode": "ACMEMFG-01",
        "type": "PROSPECT",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["1234 Industrial Pkwy"],
            "city": "Tampa",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "33601",
            "countryCode": "USA"
        }
    },
    {
        "id": "c-frogs-0001",
        "name": "Tree Frogs Adventure Park, LLC",
        "lookupCode": "TREFRG-01",
        "type": "INSURED",
        "active": True,
        "individualOrBusiness": "BUSINESS",
        "businessTypes": ["COMMERCIAL"],
        "address": {
            "streets": ["5600 Adventure Way"],
            "city": "Orlando",
            "stateOrProvince": "FL",
            "zipOrPostalCode": "32819",
            "countryCode": "USA"
        }
    },
]

POLICY_TYPES = {
    "pt-gl": {"id": "pt-gl", "code": "GL", "description": "General Liability", "businessType": "COMMERCIAL"},
    "pt-pkg": {"id": "pt-pkg", "code": "PKG", "description": "Commercial Package", "businessType": "COMMERCIAL"},
}

POLICIES = [
    # Acme - GL (Prospective, not submitted yet)
    {
        "id": "p-acme-gl-001",
        "client": "c-acme-0001",
        "description": "General Liability",
        "policyNumber": "",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-gl",
        "organization": "org-001",
    },
    # Tree Frogs - Commercial Package (Prospective, submitted to MGAs)
    {
        "id": "p-frogs-pkg-001",
        "client": "c-frogs-0001",
        "description": "Commercial Package",
        "policyNumber": "",
        "effectiveOn": "2026-09-01",
        "expirationOn": "2027-09-01",
        "status": "PROSPECTIVE",
        "policyType": "pt-pkg",
        "organization": "org-001",
    },
]

LINES = [
    # Acme GL line - not submitted
    {
        "id": "l-acme-gl-001",
        "policy": "p-acme-gl-001",
        "client": "c-acme-0001",
        "lineType": "pt-gl",
        "effectiveOn": "2026-08-01",
        "expirationOn": "2027-08-01",
        "stage": "NOT_SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
    # Tree Frogs Package line - submitted
    {
        "id": "l-frogs-pkg-001",
        "policy": "p-frogs-pkg-001",
        "client": "c-frogs-0001",
        "lineType": "pt-pkg",
        "effectiveOn": "2026-09-01",
        "expirationOn": "2027-09-01",
        "stage": "SUBMITTED",
        "billMode": "AGENCY",
        "issuingCompany": None,
        "issuingLocation": {"stateOrProvince": "FL", "country": "USA"},
    },
]

# Attachments - these reference real files in sample_docs/
ATTACHMENTS = [
    # Acme - ACORD 125 (system generated application)
    {
        "id": "att-acme-125",
        "description": "ACORD 125 - Commercial Insurance Application",
        "active": True,
        "systemGenerated": True,
        "account": "c-acme-0001",
        "attachedTos": [
            {"id": "p-acme-gl-001", "type": "POLICY", "description": "General Liability", "primary": True}
        ],
        "file": {
            "id": "file-acme-125",
            "name": "ACORD_125_Application",
            "extension": ".pdf",
            "size": 85947,
            "url": "http://localhost:5002/files/acme/ACORD_125_Application.pdf",
            "status": "OK"
        },
        "attachedOn": "2026-06-10T14:00:00Z",
        "receivedOn": "2026-06-10T14:00:00Z",
        "folder": None,
        "_local_path": os.path.join(SAMPLE_DOCS, "Acme", "ACORD_125_Application.pdf"),
    },
    # Tree Frogs - ACORD 125 (system generated)
    {
        "id": "att-frogs-125",
        "description": "ACORD 125 - Commercial Insurance Application",
        "active": True,
        "systemGenerated": True,
        "account": "c-frogs-0001",
        "attachedTos": [
            {"id": "p-frogs-pkg-001", "type": "POLICY", "description": "Commercial Package", "primary": True}
        ],
        "file": {
            "id": "file-frogs-125",
            "name": "ACORD125",
            "extension": ".pdf",
            "size": 102400,
            "url": "http://localhost:5002/files/frogs/ACORD125.pdf",
            "status": "OK"
        },
        "attachedOn": "2026-06-05T10:00:00Z",
        "receivedOn": "2026-06-05T10:00:00Z",
        "folder": None,
        "_local_path": os.path.join(SAMPLE_DOCS, "Frogs", "ACORD125.pdf"),
    },
    # Tree Frogs - Quote from MGA (user attached)
    {
        "id": "att-frogs-quote-a",
        "description": "Quote - Frog A MGA",
        "active": True,
        "systemGenerated": False,
        "account": "c-frogs-0001",
        "attachedTos": [
            {"id": "p-frogs-pkg-001", "type": "POLICY", "description": "Commercial Package", "primary": True}
        ],
        "file": {
            "id": "file-frogs-quote-a",
            "name": "quote_frogA",
            "extension": ".pdf",
            "size": 75000,
            "url": "http://localhost:5002/files/frogs/quote_frogA.pdf",
            "status": "OK"
        },
        "attachedOn": "2026-06-08T16:30:00Z",
        "receivedOn": "2026-06-08T16:30:00Z",
        "folder": None,
        "_local_path": os.path.join(SAMPLE_DOCS, "Frogs", "quote_frogA.pdf"),
    },
]

# Track updates during session
updated_lines = {}
updated_policies = {}
created_attachments = []


# ─── AUTH ─────────────────────────────────────────────────────

@app.route('/v1/auth/connect/token', methods=['POST'])
def token():
    return jsonify({
        "access_token": "mock-token-" + str(uuid.uuid4())[:8],
        "token_type": "Bearer",
        "expires_in": 3600,
    })


# ─── CLIENTS ─────────────────────────────────────────────────

@app.route('/epic/client/v1/clients', methods=['GET'])
def get_clients():
    name_filter = (request.args.get('name_contains') or '').lower()
    results = [c for c in CLIENTS if name_filter in c['name'].lower()]
    limit = int(request.args.get('limit', 100))
    return jsonify({
        "total": len(results[:limit]),
        "_embedded": {"clients": results[:limit]},
        "_links": {"self": {"href": request.url}}
    })


@app.route('/epic/client/v1/clients/<client_id>', methods=['GET'])
def get_client(client_id):
    client = next((c for c in CLIENTS if c['id'] == client_id), None)
    if not client:
        return jsonify({"title": "Not Found", "status": 404}), 404
    return jsonify(client)


# ─── POLICIES ────────────────────────────────────────────────

@app.route('/epic/policy/v2/policies', methods=['GET'])
def get_policies():
    client_filter = request.args.get('client')
    status_filter = request.args.get('status')

    results = POLICIES[:]
    if client_filter:
        results = [p for p in results if p['client'] == client_filter]
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(',')]
        results = [p for p in results if p['status'] in statuses]

    embedded_results = []
    for p in results:
        entry = dict(p)
        ptype = POLICY_TYPES.get(p.get('policyType'), {})
        client = next((c for c in CLIENTS if c['id'] == p['client']), {})
        entry['_embedded'] = {
            'policyType': ptype,
            'client': {'id': client.get('id'), 'name': client.get('name'), 'lookupCode': client.get('lookupCode')} if client else {}
        }
        embedded_results.append(entry)

    return jsonify({
        "total": len(embedded_results),
        "_embedded": {"policies": embedded_results},
        "_links": {"self": {"href": request.url}}
    })


@app.route('/epic/policy/v2/policies/<policy_id>', methods=['GET'])
def get_policy(policy_id):
    policy = next((p for p in POLICIES if p['id'] == policy_id), None)
    if not policy:
        return jsonify({"title": "Not Found", "status": 404}), 404
    result = dict(policy)
    ptype = POLICY_TYPES.get(policy.get('policyType'), {})
    client = next((c for c in CLIENTS if c['id'] == policy['client']), {})
    result['_embedded'] = {'policyType': ptype, 'client': {'id': client.get('id'), 'name': client.get('name')} if client else {}}
    return jsonify(result)


# ─── LINES ───────────────────────────────────────────────────

@app.route('/epic/policy/v2/lines', methods=['GET'])
def get_lines():
    policy_filter = request.args.get('policy')
    results = LINES[:]
    if policy_filter:
        results = [l for l in results if l['policy'] == policy_filter]

    embedded_results = []
    for line in results:
        entry = dict(line)
        ltype = POLICY_TYPES.get(line.get('lineType'), {})
        entry['_embedded'] = {'lineType': ltype, 'issuingCompany': line.get('issuingCompany') or {}}
        embedded_results.append(entry)

    return jsonify({
        "total": len(embedded_results),
        "_embedded": {"lines": embedded_results},
        "_links": {"self": {"href": request.url}}
    })


# ─── ATTACHMENTS (GET - for import) ─────────────────────────

@app.route('/epic/attachment/v2/attachments', methods=['GET'])
def get_attachments():
    """Get attachments filtered by policy, account, systemGenerated, etc."""
    policy_filter = request.args.get('policy')
    account_filter = request.args.get('account')
    system_generated = request.args.get('systemGenerated')

    results = ATTACHMENTS[:]

    if policy_filter:
        results = [a for a in results if any(
            at.get('id') == policy_filter and at.get('type') == 'POLICY'
            for at in a.get('attachedTos', [])
        )]

    if account_filter:
        results = [a for a in results if a.get('account') == account_filter]

    if system_generated is not None:
        sg = system_generated.lower() == 'true'
        results = [a for a in results if a.get('systemGenerated') == sg]

    # Strip internal _local_path before returning
    clean_results = []
    for a in results:
        clean = {k: v for k, v in a.items() if k != '_local_path'}
        clean_results.append(clean)

    return jsonify({
        "total": len(clean_results),
        "_embedded": {"attachments": clean_results},
        "_links": {"self": {"href": request.url}}
    })


@app.route('/epic/attachment/v2/attachments/<attachment_id>', methods=['GET'])
def get_attachment(attachment_id):
    att = next((a for a in ATTACHMENTS if a['id'] == attachment_id), None)
    if not att:
        return jsonify({"title": "Not Found", "status": 404}), 404
    clean = {k: v for k, v in att.items() if k != '_local_path'}
    return jsonify(clean)


# ─── FILE DOWNLOAD (serves actual PDFs) ─────────────────────

@app.route('/files/acme/<filename>', methods=['GET'])
def serve_acme_file(filename):
    filepath = os.path.join(SAMPLE_DOCS, "Acme", filename)
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filename}"}), 404
    return send_file(filepath, mimetype='application/pdf')


@app.route('/files/frogs/<filename>', methods=['GET'])
def serve_frogs_file(filename):
    filepath = os.path.join(SAMPLE_DOCS, "Frogs", filename)
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filename}"}), 404
    return send_file(filepath, mimetype='application/pdf')


# ─── ATTACHMENTS (POST - for export) ────────────────────────

@app.route('/epic/attachment/v2/attachments', methods=['POST'])
def create_attachment():
    data = request.get_json() or {}
    attachment_id = str(uuid.uuid4())
    upload_url = f"http://localhost:5002/mock-upload/{attachment_id}"

    created_attachments.append({
        "id": attachment_id,
        "description": data.get('description', ''),
        "uploadFileName": data.get('uploadFileName', ''),
        "attachTo": data.get('attachTo', {}),
        "createdAt": datetime.utcnow().isoformat(),
    })
    print(f"[MOCK] Attachment created: {data.get('uploadFileName')} -> {attachment_id}")

    return jsonify({
        "id": attachment_id,
        "description": data.get('description', ''),
        "active": True,
        "uploadUrl": upload_url,
        "file": {"id": attachment_id, "status": "PENDING"},
    }), 201


@app.route('/mock-upload/<attachment_id>', methods=['PUT'])
def mock_upload(attachment_id):
    content_length = request.content_length or 0
    print(f"[MOCK] File uploaded for {attachment_id}: {content_length} bytes")
    return '', 204


# ─── SDK MODULE (Updates) ────────────────────────────────────

@app.route('/sdk/v1/lines', methods=['PUT'])
def update_line():
    data = request.get_json() or {}
    line_id = data.get('LineID')
    updated_lines[str(line_id)] = data
    print(f"[MOCK] Line updated: {line_id}")
    return jsonify({"Envelope": {"Body": {"Update_LineResponse": {}}}})


@app.route('/sdk/v1/policies', methods=['PUT'])
def update_policy():
    data = request.get_json() or {}
    policy_id = data.get('PolicyID')
    updated_policies[str(policy_id)] = data
    print(f"[MOCK] Policy updated: {policy_id}")
    return jsonify({"Envelope": {"Body": {"Update_PolicyResponse": {}}}})


# ─── DEBUG ───────────────────────────────────────────────────

@app.route('/mock-status', methods=['GET'])
def mock_status():
    return jsonify({
        "updated_lines": updated_lines,
        "updated_policies": updated_policies,
        "created_attachments": created_attachments,
    })


if __name__ == '__main__':
    print("=" * 50)
    print("Epic Mock Server - http://localhost:5002")
    print("=" * 50)
    print(f"\nClients:")
    for c in CLIENTS:
        print(f"  • {c['name']} ({c['type']})")
    print(f"\nAttachments:")
    for a in ATTACHMENTS:
        exists = os.path.exists(a['_local_path'])
        status = "✓" if exists else "✗ MISSING"
        print(f"  {status} {a['description']} -> {a['file']['name']}{a['file']['extension']}")
    print()
    app.run(port=5002, debug=True)
