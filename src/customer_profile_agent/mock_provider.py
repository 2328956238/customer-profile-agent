"""Deterministic offline scenarios, not a natural-language model or extractor."""
import json


EXAMPLES = [
    '我想了解 Device Alpha，128GB。',
    '先要 12 台，目的地是示例国 A，我有货代。',
    '数量改成 18 台。',
]


def operation(field, value, source_id, quote, target='item_alpha'):
    return {'operation': 'set', 'target': target, 'field': field, 'value': value,
            'status': 'confirmed', 'source_id': source_id, 'quote': quote}


class MockProvider:
    """Replays fictional tool decisions while exercising the real agent and stores."""

    async def complete(self, messages, tools):
        envelope = json.loads(messages[1]['content'])
        current = envelope['current']
        latest = next((json.loads(row['content']) for row in reversed(messages)
                       if row['role'] == 'tool'), envelope['saved'])
        saved = latest.get('saved', latest)
        profile = saved['profile']
        source_id, content = current['source_id'], current['content']
        if source_id not in profile.get('source_reviews', {}):
            changes = []
            if content == EXAMPLES[0]:
                changes = [operation('model', 'Device Alpha', source_id, 'Device Alpha'),
                           operation('storage', '128GB', source_id, '128GB')]
            elif content == EXAMPLES[1]:
                changes = [operation('qty', 12, source_id, '12 台'),
                           operation('country', '示例国 A', source_id, '示例国 A', 'order'),
                           operation('freight', 'yes', source_id, '我有货代', 'order')]
            elif content == EXAMPLES[2]:
                changes = [operation('qty', 18, source_id, '18 台')]
            name, args = 'update_profile', {
                'expected_revision': profile['revision'], 'operations': changes,
                'reviewed_sources': [{'source_id': source_id,
                    'reason': 'Applied scripted fictional facts.' if changes else 'Outside the fixed offline scenarios; no facts inferred.'}]}
        else:
            args = {'revision': profile['revision'], 'action': 'answer', 'question_id': '',
                    'answer': '离线演示只识别 README 中的三个固定示例句子，请按顺序体验。'}
            if content in EXAMPLES:
                if saved['requirements']['ready']:
                    args.update(action='handoff', answer='示例需求摘要已生成，可在本地查看。')
                else:
                    question = saved['allowed_questions'][0]
                    wording = {'model': '需要哪款产品？', 'qty': '这款产品需要多少台？',
                               'country': '目的地是哪个国家？', 'freight': '是否已有自己的货代？'}
                    args.update(action=question['action'], question_id=question['id'],
                                answer='已记录。' + wording[question['field']])
            name = 'finish_turn'
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'demo_call_' + str(len(messages)), 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
