"""Optional Chat Completions transport. Demo mode never instantiates this class."""
import asyncio
import os

import httpx


class Provider:
    def __init__(self, base_url, api_key, model, timeout=40):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def configured(cls):
        config = [os.getenv(name, '').strip() for name in
                  ('PROFILE_BASE_URL', 'PROFILE_API_KEY', 'PROFILE_MODEL')]
        if not all(config):
            raise RuntimeError('Live mode requires PROFILE_BASE_URL, PROFILE_API_KEY and PROFILE_MODEL')
        return cls(*config)

    async def complete(self, messages, tools):
        payload = {'model': self.model, 'messages': messages, 'tools': tools,
                   'tool_choice': 'required', 'temperature': 0}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.post(
                        self.base_url + '/chat/completions', json=payload,
                        headers={'Authorization': 'Bearer ' + self.api_key})
                    if response.status_code in (408, 429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(attempt + 1)
                        continue
                    response.raise_for_status()
                    return response.json()['choices'][0]['message']
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)
