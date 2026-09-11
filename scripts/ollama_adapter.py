"""Bounded native Ollama calls behind the existing fixed OpenAI gateway route.

Native responses are buffered, validated, then returned as JSON or OpenAI SSE.
Only operator configuration can select resource options, never the agent.
"""
import json
import secrets
import time

MAX_RESPONSE = 2 * 1024 * 1024


def prepare(data, endpoint):
    options = endpoint['ollama']
    requested = data.get('max_completion_tokens', data.get('max_tokens', options['max_output']))
    if type(requested) is not int or requested <= 0:
        raise ValueError('Invalid output limit')
    messages, names = [], {}
    for message in data['messages']:
        role = message.get('role')
        if role not in ('system', 'developer', 'user', 'assistant', 'tool'):
            raise ValueError('Unsupported role')
        content = message.get('content') or ''
        if isinstance(content, list):
            content = '\n'.join(part['text'] for part in content)
        converted = {'role': 'system' if role == 'developer' else role, 'content': content}
        if message.get('tool_calls'):
            calls = []
            for call in message['tool_calls']:
                fn = call['function']
                arguments = fn.get('arguments', {})
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError('Tool arguments must be an object')
                names[call['id']] = fn['name']
                calls.append({'function': {'name': fn['name'], 'arguments': arguments}})
            converted['tool_calls'] = calls
        if role == 'tool':
            converted['tool_name'] = names[message['tool_call_id']]
        messages.append(converted)
    result = {'model': endpoint['model'], 'messages': messages, 'stream': False,
              'think': False, 'keep_alive': '2m',
              'options': {'num_ctx': options['context'], 'num_batch': options['batch'],
                          'num_predict': min(requested, options['max_output'])}}
    for key in ('temperature', 'top_p', 'seed', 'stop', 'presence_penalty', 'frequency_penalty'):
        if key in data:
            result['options'][key] = data[key]
    choice = data.get('tool_choice', 'auto')
    tools = data.get('tools', [])
    if isinstance(choice, dict):
        name = choice['function']['name']
        tools = [tool for tool in tools if tool['function']['name'] == name]
        if len(tools) != 1:
            raise ValueError('Unknown requested tool')
    elif choice not in ('auto', 'none', 'required'):
        raise ValueError('Unsupported tool choice')
    if tools and choice != 'none':
        result['tools'] = tools
    fmt = data.get('response_format')
    if fmt:
        if fmt.get('type') == 'json_object':
            result['format'] = 'json'
        elif fmt.get('type') == 'json_schema':
            result['format'] = fmt['json_schema']['schema']
        elif fmt.get('type') != 'text':
            raise ValueError('Unsupported response format')
    return result


def finish(data, endpoint, raw):
    native = json.loads(raw)
    if not isinstance(native, dict) or native.get('error') or native.get('done') is not True:
        raise ValueError('Incomplete or failed native response')
    if native.get('model') != endpoint['model']:
        raise ValueError('Unexpected native model')
    msg = native['message']
    content = msg.get('content', '')
    if not isinstance(content, str):
        raise ValueError('Invalid native content')
    calls = []
    allowed = {tool['function']['name'] for tool in data.get('tools', [])}
    for call in msg.get('tool_calls', []):
        fn = call['function']
        if fn['name'] not in allowed or not isinstance(fn.get('arguments'), dict):
            raise ValueError('Unexpected native tool')
        calls.append({'id': 'call_' + secrets.token_hex(12), 'type': 'function',
                      'function': {'name': fn['name'], 'arguments': json.dumps(fn['arguments'])}})
    choice = data.get('tool_choice', 'auto')
    if choice == 'none' and calls or choice == 'required' and not calls:
        raise ValueError('Tool choice not satisfied')
    if isinstance(choice, dict) and (not calls or any(c['function']['name'] != choice['function']['name'] for c in calls)):
        raise ValueError('Requested tool not called')
    if data.get('parallel_tool_calls') is False and len(calls) > 1:
        raise ValueError('Multiple tool calls not allowed')
    reason = 'tool_calls' if calls else ('length' if native.get('done_reason') == 'length' else 'stop')
    usage = {'prompt_tokens': int(native.get('prompt_eval_count', 0)),
             'completion_tokens': int(native.get('eval_count', 0))}
    usage['total_tokens'] = sum(usage.values())
    base = {'id': 'chatcmpl-' + secrets.token_hex(12), 'created': int(time.time()), 'model': endpoint['model']}
    message = {'role': 'assistant', 'content': content or None}
    if calls:
        message['tool_calls'] = calls
    if not data.get('stream'):
        response = {**base, 'object': 'chat.completion', 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}], 'usage': usage}
        return 'application/json', json.dumps(response).encode()
    chunks = []
    def chunk(delta, finish_reason=None):
        value = {**base, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]}
        chunks.append('data: ' + json.dumps(value) + '\n\n')
    chunk({'role': 'assistant'})
    if content:
        chunk({'content': content})
    for index, call in enumerate(calls):
        chunk({'tool_calls': [{**call, 'index': index}]})
    chunk({}, reason)
    if data.get('stream_options', {}).get('include_usage'):
        chunks.append('data: ' + json.dumps({**base, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage}) + '\n\n')
    chunks.append('data: [DONE]\n\n')
    return 'text/event-stream', ''.join(chunks).encode()
