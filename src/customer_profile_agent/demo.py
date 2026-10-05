"""Run three synthetic turns and demonstrate an idempotent retry."""
import asyncio
import json
import tempfile
from pathlib import Path

from .agent import ProfileAgent
from .mock_provider import EXAMPLES, MockProvider
from .profile_store import Store


async def run():
    with tempfile.TemporaryDirectory(prefix='profile-demo-') as directory:
        store = Store(Path(directory) / 'profiles.sqlite3')
        agent = ProfileAgent(store, MockProvider())
        for index, message in enumerate(EXAMPLES, 1):
            context = {'_message_id': f'm{index}'}
            result = await agent.get_reply(message, [], 'demo-customer', context)
            assert not result.get('retryable'), result
            print(f'\n客户：{message}\n助手：{result["answer"]}')
            print('revision:', result['state_revision'], '| action:', result['action'])
        retry = await agent.get_reply(EXAMPLES[-1], [], 'demo-customer', {'_message_id': 'm3'})
        assert retry == result
        print('\n重试 m3：返回已缓存结果，revision 不增加。')
        profile = store.load('demo-customer')
        print(json.dumps({'revision': profile['revision'], 'items': profile['items'],
                          'order': profile['order'], 'audit_entries': len(profile['audit'])},
                         ensure_ascii=False, indent=2))


def main():
    asyncio.run(run())


if __name__ == '__main__':
    main()
