# backend/integrations/hubspot.py

import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = '8054ea73-82cf-4977-a221-6e5139a0f16b'
CLIENT_SECRET = 'ba76888a-6f99-47e8-b81b-594d51e6241e'

REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'

AUTHORIZATION_URL = 'https://app.hubspot.com/oauth/authorize'
TOKEN_URL = 'https://api.hubapi.com/oauth/v1/token'

SCOPES = 'crm.objects.contacts.read crm.objects.companies.read'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    auth_url = f"{AUTHORIZATION_URL}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPES}&state={encoded_state}"
    return auth_url

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error_description'))
    
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    
    if not encoded_state:
         raise HTTPException(status_code=400, detail='State missing.')

    state_data = json.loads(encoded_state)
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')

    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                TOKEN_URL,
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                },
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to retrieve token: {response.text}")

    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)
    
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    credentials = json.loads(credentials)
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

def create_integration_item_metadata_object(hubspot_obj, obj_type):
    """
    Helper to convert HubSpot JSON to IntegrationItem.
    """
    properties = hubspot_obj.get('properties', {})
    
    name = "Unknown"
    if obj_type == 'Contact':
        firstname = properties.get('firstname', '')
        lastname = properties.get('lastname', '')
        name = f"{firstname} {lastname}".strip() or properties.get('email', 'Unnamed Contact')
    elif obj_type == 'Company':
        name = properties.get('name', 'Unnamed Company')

    return IntegrationItem(
        id=hubspot_obj.get('id'),
        type=obj_type,
        name=name,
        creation_time=properties.get('createdate'),
        last_modified_time=properties.get('lastmodifieddate'),
        parent_id=None 
    )

async def get_items_hubspot(credentials):
    credentials = json.loads(credentials)
    access_token = credentials.get('access_token')
    
    items = []

    async with httpx.AsyncClient() as client:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response_contacts = await client.get('https://api.hubapi.com/crm/v3/objects/contacts', headers=headers)
        if response_contacts.status_code == 200:
            for result in response_contacts.json().get('results', []):
                items.append(create_integration_item_metadata_object(result, 'Contact'))

        response_companies = await client.get('https://api.hubapi.com/crm/v3/objects/companies', headers=headers)
        if response_companies.status_code == 200:
            for result in response_companies.json().get('results', []):
                items.append(create_integration_item_metadata_object(result, 'Company'))

    json_items = [item.__dict__ for item in items]
    
    print("HubSpot Items Fetched:")
    print(json.dumps(json_items, indent=2, default=str)) 

    return json_items