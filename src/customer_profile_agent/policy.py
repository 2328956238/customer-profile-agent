"""Fictional inquiry rules, deliberately separate from persistence and transport.

No price, stock, delivery coverage or fulfillment promise is modeled here.
"""
from .profile_store import REQUIRED


def requirements(profile):
    missing, ambiguous = [], []
    targets = [(key, facts, REQUIRED) for key, facts in profile['items'].items()]
    targets.append(('order', profile['order'], ('country', 'freight')))
    for target, facts, fields in targets:
        for field in fields:
            fact = facts.get(field)
            if not fact:
                missing.append({'target': target, 'field': field})
            elif fact['status'] == 'ambiguous':
                ambiguous.append({'target': target, 'field': field, 'value': fact['value']})
    for field in ('quantity_mentioned', 'quantity_scope', 'quantity_total'):
        fact = profile['order'].get(field)
        if fact and fact['status'] == 'ambiguous':
            ambiguous.append({'target': 'order', 'field': field, 'value': fact['value']})
    return {'missing': missing, 'ambiguous': ambiguous,
            'ready': bool(profile['items']) and not missing and not ambiguous}


DEMO_POLICY = {
    'label': 'Fictional product inquiry example',
    'required': ['model', 'qty per item', 'country', 'freight'],
    'freight_values': {
        'yes': 'customer has a forwarder',
        'no': 'customer has no forwarder',
        'self': 'customer requests self-collection',
        'assistance': 'customer requests shipping assistance',
    },
    'note': 'No prices, stock or delivery guarantees. Completion creates a local draft only.',
}
