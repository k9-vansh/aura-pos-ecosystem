"""The AI services the written reports can use, and one way to ask any of them.

The owner pastes a key for any of these in the owner app (Settings > AI keys). Keys are kept sealed (`clients.pins.seal`),
never sent back to a phone (only the last four characters are), and one provider is marked "in use": the report asks it
first and, if it fails (a wrong key, no credit, a busy service), asks the next saved one, so a report is still written.

Two wire formats cover everything: the OpenAI chat format (OpenAI, OpenRouter, Gemini, DeepSeek, Groq, Mistral and any
"other" service that copies it) and Anthropic's own (Claude directly).
"""
import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

PROVIDERS = {
    'openrouter': {'label': 'OpenRouter', 'style': 'openai', 'url': 'https://openrouter.ai/api/v1/chat/completions',
                   'model': 'anthropic/claude-sonnet-4.5', 'where': 'openrouter.ai/keys',
                   'note': 'One key for hundreds of models, Claude and GPT among them.'},
    'anthropic': {'label': 'Claude (Anthropic)', 'style': 'anthropic', 'url': 'https://api.anthropic.com/v1/messages',
                  'model': 'claude-sonnet-5-5', 'where': 'console.anthropic.com/settings/keys',
                  'note': 'Claude directly from Anthropic.'},
    'openai': {'label': 'OpenAI', 'style': 'openai', 'url': 'https://api.openai.com/v1/chat/completions',
               'model': 'gpt-4.1-mini', 'where': 'platform.openai.com/api-keys', 'note': 'GPT models.',
               'tokens_field': 'max_completion_tokens', 'temperature': False},
    'gemini': {'label': 'Google Gemini', 'style': 'openai',
               'url': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
               'model': 'gemini-2.5-flash', 'where': 'aistudio.google.com/apikey', 'note': 'Gemini models.'},
    'deepseek': {'label': 'DeepSeek', 'style': 'openai', 'url': 'https://api.deepseek.com/chat/completions',
                 'model': 'deepseek-chat', 'where': 'platform.deepseek.com/api_keys', 'note': ''},
    'groq': {'label': 'Groq', 'style': 'openai', 'url': 'https://api.groq.com/openai/v1/chat/completions',
             'model': 'llama-3.3-70b-versatile', 'where': 'console.groq.com/keys', 'note': ''},
    'mistral': {'label': 'Mistral', 'style': 'openai', 'url': 'https://api.mistral.ai/v1/chat/completions',
                'model': 'mistral-small-latest', 'where': 'console.mistral.ai/api-keys', 'note': ''},
    'custom': {'label': 'Other (OpenAI-style)', 'style': 'openai', 'url': '', 'model': '', 'where': '',
               'note': 'Any service that copies the OpenAI chat format: give its address and a model name.',
               'needs_url': True},
}

TIMEOUT = 90
MAX_KEY = 400


class AiError(Exception):
    """A call that did not give an answer. The message is safe to show: it never holds the key."""


def check_url(url):
    """The address of an 'other' service: https, and not a machine on this network (a key and the salon's figures must
    not be sent to a private address). Raises AiError with the reason."""
    parts = urlparse(url or '')
    if parts.scheme != 'https' or not parts.hostname:
        raise AiError('The address must start with https://')
    host = parts.hostname
    if host.lower() in ('localhost',) or host.lower().endswith(('.local', '.internal')):
        raise AiError('That address is a machine on this network. Use the service\'s public address.')
    try:
        found = {ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, ValueError):
        raise AiError('That address could not be found.')
    if any(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast for ip in found):
        raise AiError('That address is a machine on this network. Use the service\'s public address.')
    return url


def clean_key(raw):
    key = str(raw or '').strip()
    if len(key) < 8 or len(key) > MAX_KEY or any(ch.isspace() for ch in key):
        raise AiError('That does not look like a key. Paste it exactly as the service shows it, with no spaces.')
    return key


def _explain(status, body):
    if status in (401, 403):
        return 'The service did not accept this key. Check it was copied fully and is still active.'
    if status == 402:
        return 'The account has no credit left. Add credit with the service and try again.'
    if status == 404:
        return 'The service does not know this model name. Check the model.'
    if status == 429:
        return 'The service is busy or the key has reached its limit. Try again in a little while.'
    if status >= 500:
        return 'The service had a problem on its side. Try again in a few minutes.'
    detail = ''
    try:
        err = body.get('error') if isinstance(body, dict) else None
        detail = (err.get('message') if isinstance(err, dict) else err) or ''
    except Exception:                                                              # noqa: BLE001
        pass
    return f'The service refused the request ({status}). {str(detail)[:160]}'.strip()


def complete(provider, key, model, system, user, base_url='', max_tokens=900, temperature=0.3):
    """The model's text answer to `system` + `user` from this provider. Raises AiError."""
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise AiError('That service is not one this app knows.')
    model = (model or spec['model']).strip()
    url = spec['url']
    if spec.get('needs_url'):
        url = check_url(base_url)
    if not model:
        raise AiError('Give the model name to use.')
    try:
        if spec['style'] == 'anthropic':
            reply = requests.post(url, headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                                                'content-type': 'application/json'},
                                  json={'model': model, 'max_tokens': max_tokens, 'temperature': temperature,
                                        'system': system, 'messages': [{'role': 'user', 'content': user}]},
                                  timeout=TIMEOUT)
        else:
            body = {'model': model, spec.get('tokens_field', 'max_tokens'): max_tokens,
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
            if spec.get('temperature', True):
                body['temperature'] = temperature
            headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
            if provider == 'openrouter':
                headers['X-Title'] = 'Salon owner app'
            reply = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except requests.Timeout:
        raise AiError('The service took too long to answer. Try again.')
    except requests.RequestException:
        raise AiError('Could not reach the service. Check the internet connection of the server.')
    except Exception:                                                              # noqa: BLE001
        logger.warning('AI call to %s failed unexpectedly', provider, exc_info=True)
        raise AiError('The call to the service failed. Try again.')
    try:
        data = reply.json()
    except ValueError:
        data = None
    if not reply.ok:
        raise AiError(_explain(reply.status_code, data))
    try:
        if spec['style'] == 'anthropic':
            text = ''.join(part.get('text', '') for part in data['content'] if part.get('type') == 'text')
        else:
            text = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError):
        raise AiError('The service answered in a form this app could not read.')
    if not str(text or '').strip():
        raise AiError('The service sent an empty answer.')
    return str(text)


def saved():
    """The keys to try, in order: the one in use first, then the others, then the server's own OpenRouter key (if the
    owner never pasted one). Each is (provider, key, model, base_url)."""
    from clients import pins
    from .models import AiKey

    rows = sorted(AiKey.objects.all(), key=lambda r: (not r.is_primary, -r.updated_at.timestamp()))
    out = []
    for row in rows:
        key = pins.unseal(row.sealed_key)
        if key:
            out.append((row.provider, key, row.model, row.base_url))
    env_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if env_key and not any(p == 'openrouter' for p, *_ in out):
        out.append(('openrouter', env_key, os.environ.get('OWNER_AI_MODEL', '').strip(), ''))
    return out


def any_configured():
    return bool(saved())
