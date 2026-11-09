"""AI keys pasted in the owner app: kept sealed, shown only as their last four characters, any service can write the
weekly report, and a failing one falls back to the next."""
from unittest import mock

from django.test import override_settings

from owner_api import ai_providers
from owner_api.models import AiKey, OwnerWeeklyReport
from owner_api.tests import BASE, OwnerBase, account, signed_in

URL = BASE + 'ai/'
OK_TEXT = '{"headline": "A good week", "summary": "Up.", "bullets": ["a"], "watch": []}'


def reply(status=200, body=None):
    r = mock.Mock()
    r.status_code, r.ok = status, status < 400
    r.json.return_value = body if body is not None else {}
    return r


def openai_body(text=OK_TEXT):
    return {'choices': [{'message': {'content': text}}]}


def claude_body(text=OK_TEXT):
    return {'content': [{'type': 'text', 'text': text}]}


class AiKeyTests(OwnerBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.owner.role.permissions['owner_app']['ai_keys'] = {'read': True, 'update': True}
        self.owner.role.save()

    def save(self, provider, key='sk-test-1234567890', **extra):
        res = self.api.put(URL, {'provider': provider, 'key': key, **extra}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def row(self, data, provider):
        return next(r for r in data['providers'] if r['id'] == provider)

    def test_it_needs_its_own_tick(self):
        reader = signed_in(account(name='R', phone='9811100003', centers=[self.a], ai_report={'read': True}))
        self.assertEqual(reader.get(URL).status_code, 403)
        view_only = account(name='V', phone='9811100004', centers=[self.a], ai_keys={'read': True})
        api = signed_in(view_only)
        self.assertEqual(api.get(URL).status_code, 200)
        self.assertEqual(api.put(URL, {'provider': 'openai', 'key': 'sk-test-1234567890'}, format='json').status_code, 403)
        self.assertFalse(api.get(BASE + 'me/').data['can_manage_ai_keys'])
        self.assertTrue(self.api.get(BASE + 'me/').data['can_manage_ai_keys'])

    def test_a_pasted_key_is_sealed_and_only_its_last_four_characters_come_back(self):
        data = self.save('openrouter', key='sk-or-v1-abcdef123456')
        row = self.row(data, 'openrouter')
        self.assertEqual((row['has_key'], row['last4'], row['primary']), (True, '3456', True))
        self.assertNotIn('abcdef', str(self.api.get(URL).data))
        stored = AiKey.objects.get(provider='openrouter')
        self.assertNotIn('abcdef', stored.sealed_key)
        from clients import pins
        self.assertEqual(pins.unseal(stored.sealed_key), 'sk-or-v1-abcdef123456')

    def test_a_key_with_spaces_or_too_short_is_refused(self):
        for bad in ('short', 'has a space inside it 12345', ''):
            res = self.api.put(URL, {'provider': 'openai', 'key': bad}, format='json')
            self.assertEqual(res.status_code, 400, bad)
        self.assertFalse(AiKey.objects.exists())
        self.assertEqual(self.api.put(URL, {'provider': 'nonsense', 'key': 'sk-test-1234567890'}, format='json').status_code, 400)

    def test_the_first_key_is_in_use_and_another_can_be_made_so(self):
        self.save('openrouter')
        data = self.save('anthropic', key='sk-ant-1234567890')
        self.assertEqual(data['in_use'], 'openrouter')
        data = self.save('anthropic', primary=True)
        self.assertEqual(data['in_use'], 'anthropic')
        self.assertEqual(AiKey.objects.filter(is_primary=True).count(), 1)

    def test_changing_the_model_does_not_need_the_key_again(self):
        self.save('openai')
        data = self.api.put(URL, {'provider': 'openai', 'model': 'gpt-5'}, format='json').data
        self.assertEqual(self.row(data, 'openai')['model'], 'gpt-5')
        from clients import pins
        self.assertEqual(pins.unseal(AiKey.objects.get(provider='openai').sealed_key), 'sk-test-1234567890')

    def test_removing_the_one_in_use_hands_over_to_the_next(self):
        self.save('openrouter')
        self.save('openai')
        data = self.api.put(URL, {'provider': 'openrouter', 'remove': True}, format='json').data
        self.assertEqual((data['in_use'], self.row(data, 'openrouter')['has_key']), ('openai', False))

    def test_the_report_asks_the_one_in_use_in_its_own_format(self):
        self.save('anthropic', key='sk-ant-secret-1234', primary=True)
        with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(200, claude_body())) as post:
            res = self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.anthropic.com/v1/messages')
        self.assertEqual(kwargs['headers']['x-api-key'], 'sk-ant-secret-1234')
        self.assertIn('Never invent a number', kwargs['json']['system'])
        self.assertEqual(kwargs['json']['messages'][0]['role'], 'user')
        report = OwnerWeeklyReport.objects.get()
        self.assertEqual((report.narrative['status'], report.narrative['headline'], report.model_name),
                         ('ok', 'A good week', 'claude-sonnet-5-5'))

    def test_a_failing_service_falls_back_to_the_next_saved_one(self):
        self.save('openrouter', key='sk-or-first-1234', primary=True)
        self.save('openai', key='sk-openai-second-12')
        seen = []

        def post(url, **kwargs):
            seen.append(url)
            return reply(401, {'error': {'message': 'bad key'}}) if 'openrouter' in url else reply(200, openai_body())
        with mock.patch('owner_api.ai_providers.requests.post', side_effect=post):
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(len(seen), 2)
        self.assertIn('openrouter', seen[0])
        self.assertIn('api.openai.com', seen[1])
        n = OwnerWeeklyReport.objects.get()
        self.assertEqual((n.narrative['status'], n.model_name), ('ok', 'gpt-4.1-mini'))

    def test_openai_gets_the_token_field_it_wants(self):
        self.save('openai')
        with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(200, openai_body())) as post:
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        sent = post.call_args.kwargs['json']
        self.assertIn('max_completion_tokens', sent)
        self.assertNotIn('temperature', sent)

    def test_a_report_made_before_any_key_is_made_again_once_one_is_pasted(self):
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': ''}):
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(OwnerWeeklyReport.objects.get().narrative['status'], 'not_configured')
        self.save('openai')
        with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(200, openai_body())):
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(OwnerWeeklyReport.objects.get().narrative['status'], 'ok')

    def test_the_test_button_says_whether_the_key_works_and_why_not(self):
        self.assertEqual(self.api.put(URL, {'provider': 'openai', 'test': True}, format='json').status_code, 400)
        self.save('openai')
        with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(200, openai_body('ok'))):
            res = self.api.put(URL, {'provider': 'openai', 'test': True}, format='json')
        self.assertEqual(res.data['test'], {'provider': 'openai', 'ok': True, 'message': 'This key works.'})
        with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(401, {})):
            res = self.api.put(URL, {'provider': 'openai', 'test': True}, format='json')
        self.assertFalse(res.data['test']['ok'])
        self.assertIn('did not accept', res.data['test']['message'])
        self.assertNotIn('sk-test', str(res.data))

    def test_the_servers_own_openrouter_key_still_works_when_nothing_was_pasted(self):
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': 'env-key-12345'}):
            self.assertTrue(self.api.get(URL).data['server_key'])
            with mock.patch('owner_api.ai_providers.requests.post', return_value=reply(200, openai_body())):
                self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(OwnerWeeklyReport.objects.get().narrative['status'], 'ok')

    def test_an_other_service_must_be_public_https(self):
        for bad in ('http://api.example.com/v1/chat/completions', 'https://localhost/v1', 'https://x.internal/v1', ''):
            res = self.api.put(URL, {'provider': 'custom', 'key': 'sk-test-1234567890', 'model': 'm', 'base_url': bad}, format='json')
            self.assertEqual(res.status_code, 400, bad)
        with mock.patch('owner_api.ai_providers.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.5', 443))]):
            res = self.api.put(URL, {'provider': 'custom', 'key': 'sk-test-1234567890', 'model': 'm',
                                     'base_url': 'https://api.example.com/v1/chat/completions'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('network', res.data['detail'])
        with mock.patch('owner_api.ai_providers.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            res = self.api.put(URL, {'provider': 'custom', 'key': 'sk-test-1234567890',
                                     'base_url': 'https://api.example.com/v1/chat/completions'}, format='json')
            self.assertEqual(res.status_code, 400)                          # the model name is needed too
            res = self.api.put(URL, {'provider': 'custom', 'key': 'sk-test-1234567890', 'model': 'm',
                                     'base_url': 'https://api.example.com/v1/chat/completions'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

    def test_every_listed_service_has_what_a_call_needs(self):
        for pid, spec in ai_providers.PROVIDERS.items():
            self.assertIn(spec['style'], ('openai', 'anthropic'), pid)
            self.assertTrue(spec['url'] or spec.get('needs_url'), pid)
