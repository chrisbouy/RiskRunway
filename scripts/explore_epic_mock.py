"""
Quick script to explore what test data exists in Applied Epic's mock API.
Run: python scripts/explore_epic_mock.py

Requires EPIC_CLIENT_ID and EPIC_CLIENT_SECRET in .env
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

# Load env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

BASE_URL = os.environ.get('EPIC_BASE_URL', 'https://api.mock.myappliedproducts.com')
CLIENT_ID = os.environ.get('EPIC_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('EPIC_CLIENT_SECRET', '')

if not CLIENT_ID or not CLIENT_SECRET:
    print("ERROR: Set EPIC_CLIENT_ID and EPIC_CLIENT_SECRET in .env")
    sys.exit(1)


def get_token():
    resp = requests.post(f"{BASE_URL}/v1/auth/connect/token", data={
        'grant_type': 'client_credentials',
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
    })
    if resp.status_code != 200:
        print(f"Token error: {resp.status_code} - {resp.text}")
        sys.exit(1)
    return resp.json()['access_token']


def api_get(token, url, params=None):
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/hal+json, application/json',
    }
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code >= 400:
        return None
    return resp.json()


def main():
    token = get_token()
    print(f"✓ Authenticated against {BASE_URL}\n")

    # --- Clients ---
    print("=" * 60)
    print("CLIENTS")
    print("=" * 60)

    # Try various search terms to find all clients
    search_terms = ['a', 'b', 'c', 'd', 'e', 'j', 'l', 'm', 's']
    all_clients = {}

    for term in search_terms:
        data = api_get(token, f"{BASE_URL}/epic/client/v1/clients", {
            'name_contains': term,
            'limit': 100,
            'active_status': 'active,inactive',
        })
        if data and '_embedded' in data:
            for client in data['_embedded'].get('clients', []):
                cid = client.get('id')
                if cid and cid not in all_clients:
                    all_clients[cid] = client

    if not all_clients:
        print("  No clients found via REST API. Trying SDK...")
        # Try SDK module
        data = api_get(token, f"{BASE_URL}/sdk/v1/clients", {
            'QueryValue': 'a',
            'SearchType': 'AccountName',
            'ClientStatus': 'All',
            'ClientType': 'All',
        })
        if data:
            print(f"  SDK response: {json.dumps(data, indent=2)[:500]}")
    else:
        print(f"  Found {len(all_clients)} unique client(s):\n")
        for cid, client in all_clients.items():
            name = client.get('name', 'Unknown')
            lookup = client.get('lookupCode', '')
            ctype = client.get('type', '')
            active = client.get('active', '')
            print(f"  • {name}")
            print(f"    ID: {cid}")
            print(f"    Lookup: {lookup} | Type: {ctype} | Active: {active}")
            addr = client.get('address', {})
            if addr:
                city = addr.get('city', '')
                state = addr.get('stateOrProvince', '')
                if city or state:
                    print(f"    Location: {city}, {state}")
            print()

    # --- Policies ---
    print("=" * 60)
    print("POLICIES")
    print("=" * 60)

    # Get all policies (no client filter)
    data = api_get(token, f"{BASE_URL}/epic/policy/v2/policies", {
        'limit': 100,
        'embed': 'client,policyType',
    })

    policies = []
    if data and '_embedded' in data:
        policies = data['_embedded'].get('policies', [])

    if not policies:
        # Try with different status
        for status in ['PROSPECTIVE', 'CONTRACTED', 'PROSPECTIVE,CONTRACTED']:
            data = api_get(token, f"{BASE_URL}/epic/policy/v2/policies", {
                'limit': 100,
                'status': status,
                'embed': 'client,policyType',
            })
            if data and '_embedded' in data:
                policies = data['_embedded'].get('policies', [])
                if policies:
                    break

    if not policies:
        print("  No policies found via REST API.\n")
        # Try per-client
        if all_clients:
            print("  Trying per-client lookup...")
            for cid in list(all_clients.keys())[:5]:
                data = api_get(token, f"{BASE_URL}/epic/policy/v2/policies", {
                    'client': cid,
                    'limit': 50,
                    'embed': 'client,policyType',
                })
                if data and '_embedded' in data:
                    for p in data['_embedded'].get('policies', []):
                        policies.append(p)
    
    if policies:
        print(f"  Found {len(policies)} policy/policies:\n")
        for policy in policies:
            embedded = policy.get('_embedded', {})
            client_embed = embedded.get('client', {})
            ptype_embed = embedded.get('policyType', {})

            desc = policy.get('description', 'No description')
            pid = policy.get('id', '')
            status = policy.get('status', '')
            pnum = policy.get('policyNumber', '')
            eff = policy.get('effectiveOn', '')
            exp = policy.get('expirationOn', '')
            client_name = client_embed.get('name', policy.get('client', ''))
            ptype_desc = ptype_embed.get('description', '')
            ptype_code = ptype_embed.get('code', '')
            btype = ptype_embed.get('businessType', '')

            print(f"  • {desc}")
            print(f"    ID: {pid}")
            print(f"    Client: {client_name}")
            print(f"    Type: {ptype_desc} ({ptype_code}) | Business: {btype}")
            print(f"    Status: {status} | Number: {pnum}")
            print(f"    Dates: {eff} → {exp}")
            print()
    else:
        print("  Could not find any policies in the mock.\n")

    # --- Lines (for first few policies) ---
    if policies:
        print("=" * 60)
        print("LINES (first 5 policies)")
        print("=" * 60)

        for policy in policies[:5]:
            pid = policy.get('id')
            desc = policy.get('description', 'Unknown')
            data = api_get(token, f"{BASE_URL}/epic/policy/v2/lines", {
                'policy': pid,
                'embed': 'lineType,issuingCompany',
            })
            
            lines = []
            if data and '_embedded' in data:
                lines = data['_embedded'].get('lines', [])

            print(f"\n  Policy: {desc} ({pid})")
            if lines:
                for line in lines:
                    embedded = line.get('_embedded', {})
                    lt = embedded.get('lineType', {})
                    ic = embedded.get('issuingCompany', {})
                    print(f"    └─ Line: {lt.get('description', 'Unknown')} ({lt.get('code', '')})")
                    print(f"       ID: {line.get('id')}")
                    print(f"       Stage: {line.get('stage', 'N/A')} | Carrier: {ic.get('name', 'None')}")
                    print(f"       Dates: {line.get('effectiveOn', '')} → {line.get('expirationOn', '')}")
            else:
                print(f"    └─ No lines found")

    print("\n" + "=" * 60)
    print("Done. Use IDs above to test import/export flows.")


if __name__ == '__main__':
    main()
