"""Run the complete synthetic support API scenario, with no sockets or secrets."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import tempfile
from unittest.mock import patch

import httpx
from .api import create_app
from .lead_scoring import EXAMPLE_FEATURES
from .mock_provider import EXAMPLES, MockProvider
from .profile_store import Store
from .security import SecurityStore


async def run_demo():
    with tempfile.TemporaryDirectory(prefix='support-demo-') as folder:
        store = Store(Path(folder)/'support.sqlite3')
        access = SecurityStore(store.path)
        tokens = {role:secrets.token_urlsafe(32) for role in ('admin','agent','viewer')}
        for role,token in tokens.items():
            access.bootstrap_token(token, 'fictional-shop', 'demo-'+role, role)
        app = create_app(store, MockProvider(), security=access)
        headers = {role:{'Authorization':'Bearer '+token} for role,token in tokens.items()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local-demo') as client:
            async def checked(method, path, role='agent', expect=200, **kwargs):
                response = await client.request(method,path,headers=headers[role],**kwargs)
                if response.status_code != expect:
                    raise RuntimeError(f'{method} {path}: expected {expect}, got {response.status_code}')
                return response
            await checked('PUT','/knowledge/shipping',role='admin',json={
                'version':'v1','title':'Fictional shipping policy',
                'text':'Standard shipping takes eight days after confirmation. This is fictional demo policy.'})
            answer = (await checked('POST','/knowledge/answer',role='viewer',json={'query':'standard shipping'})).json()
            assert answer['status'] == 'answered' and answer['citations']
            for i,message in enumerate(EXAMPLES,1):
                response = await checked('POST','/profiles/demo/turns',json={'message_id':f'm{i}','message':message})
                assert not response.json().get('retryable')
            assert response.json()['external_delivery'] is False
            trace_id = response.headers['X-Trace-ID']
            score = (await checked('POST','/leads/demo/score',json=EXAMPLE_FEATURES)).json()
            assert score['advisory_only'] is True
            await checked('POST','/profiles/demo/turns',role='viewer',expect=403,
                          json={'message_id':'forbidden','message':EXAMPLES[0]})
            traces = (await checked('GET','/ops/traces',role='admin')).json()['items']
            audit = (await checked('GET','/ops/audit',role='admin')).json()['items']
            assert any(x['trace_id'] == trace_id for x in traces)
            assert any(x['details'].get('trace_id') == trace_id for x in audit)
            access.revoke_token(tokens['viewer'])
            await checked('GET','/profiles/demo',role='viewer',expect=401)
        return {'synthetic':True,'offline':True,'checks':{
            'versioned_knowledge_citation':True,'three_turn_profile_and_correction':True,
            'advisory_ml_classification':True,'viewer_write_denied':True,'token_revocation':True,
            'trace_audit_correlation':True,'external_delivery':False},
            'lead_example':score,'trace_count':len(traces),'audit_count':len(audit),
            'limitations':['No real model, customer data, shipping or sales messages were used.',
                           'Classification is trained on synthetic data; scores are not conversion probabilities.',
                           'Knowledge answer is a validated excerpt, not a semantic-correctness guarantee.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    with patch('socket.socket.connect', side_effect=AssertionError('offline demo cannot use network')):
        report = asyncio.run(run_demo())
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
