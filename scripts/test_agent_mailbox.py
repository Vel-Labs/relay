"""Requirement-derived offline mailbox checks. No providers, services or real data."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_mailbox as m


def packet():
    at = (m.now() - timedelta(minutes=1)).isoformat()
    identity = str(uuid4())
    item = {'project': m.PROJECT, 'repo_url': '', 'branch': '', 'commits': [],
            'title': 'Local attachment rehearsal', 'kind': 'docs',
            'what_changed': 'A synthetic packet exercises the attachment flow; no remote exchange occurred.',
            'why_it_matters': 'We want to preserve a discovery and identify missing receipts.',
            'proof': {'links': [], 'screenshots': [], 'metrics': []}, 'pillar': 'reliability',
            'audience': 'Haru and Steven', 'hook_ideas': [], 'format_suggestion': 'single',
            'related_thread_url': None, 'time_sensitive': False, 'time_sensitive_reason': None,
            'postability': 0, 'sensitivity': dict.fromkeys(('employer_ledger', 'private_code', 'secrets', 'unreleased'), False), 'cta': 'none'}
    return {'version': 1, 'id': identity, 'from': 'codex', 'to': 'grokbot', 'kind': 'social_packet',
            'created_at': at, 'reply_to': None, 'thread_id': identity, 'priority': 'low', 'expires_at': None,
            'sensitivity': {'employer': False, 'ledger': False, 'private_code': False, 'secrets_checked': True},
            'payload': {'version': 1, 'packet_id': str(uuid4()), 'created_at': at, 'author': 'vel',
                        'cadence': 'event', 'items': [item]}}


def reply(original, raw, status='ack', kind='receipt'):
    value = {'version': 1, 'id': str(uuid4()), 'from': 'grokbot', 'to': 'codex', 'kind': kind,
             'created_at': m.stamp(), 'reply_to': original['id'], 'thread_id': original['thread_id'],
             'priority': 'low', 'expires_at': None, 'sensitivity': deepcopy(original['sensitivity'])}
    if kind == 'request':
        value['payload'] = {'title': 'Discussion only', 'ask': 'What would establish a useful handoff?', 'inputs': [], 'due': None}
    else:
        checks = [{'name': 'validation', 'passed': True}, {'name': 'storage', 'passed': True}]
        if status == 'done':
            checks.append({'name': 'handling', 'passed': True})
        evidence = {'content_sha256': hashlib.sha256(raw).hexdigest(),
                    'scope': {'ack': 'stored', 'done': 'handled', 'rejected': 'rejected'}[status],
                    'checks': checks, 'note': 'Simulated receipt, not a Haru run.'}
        value['payload'] = {'for_id': original['id'], 'status': status, 'reason': json.dumps(evidence), 'at': value['created_at']}
    return value


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / 'store'
        self.value = packet()
        self.raw = m.encoded(self.value)

    def save(self):
        return m.store_message(self.store, self.raw, 'fixture-task', 'stored')

    def invoke(self, *args, ok=True):
        result = subprocess.run([sys.executable, '-I', '-B', str(m.ROOT / 'scripts/agent_mailbox.py'), '--store', str(self.store), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return result

    def assert_invalid(self, value, code=2):
        with self.assertRaises(m.MailboxError) as caught:
            m.validate(m.encoded(value))
        self.assertEqual(caught.exception.code, code)

    def test_valid_full_legacy_payload_preserved(self):
        self.assertEqual(m.validate(self.raw)['payload'], self.value['payload'])
        value = deepcopy(self.value)
        value['payload']['author'] = 'another-author'
        m.validate(m.encoded(value))  # vel is an example, not a schema enum.
        value['payload']['items'] = []
        value['payload']['cadence'] = 'daily'
        m.validate(m.encoded(value))

    def test_haru_schema_mismatches_are_rejected(self):
        for mutation in (
            lambda v: v.pop('created_at'),
            lambda v: v.update(expires=v.pop('expires_at')),
            lambda v: v['sensitivity'].pop('secrets_checked'),
            lambda v: v['payload'].update(packet_id='test-0001'),
            lambda v: v['payload']['items'][0].update(proof=None),
            lambda v: v['payload']['items'][0].pop('what_changed'),
        ):
            with self.subTest(mutation=mutation):
                value = deepcopy(self.value); mutation(value); self.assert_invalid(value)

    def test_types_dates_enums_unsupported_and_duplicate_keys(self):
        for key, bad in [('version', True), ('kind', 'result'), ('priority', 'emergency'), ('created_at', '2026-10-14T10:00:00'), ('id', '../bad')]:
            value = deepcopy(self.value); value[key] = bad; self.assert_invalid(value)
        for raw in (b'{"id":1,"id":2}', b'NaN', b'\xff', b' ' * (m.LIMIT + 1)):
            with self.assertRaises(m.MailboxError): m.validate(raw)
        value = deepcopy(self.value); value['payload']['items'][0]['postability'] = True; self.assert_invalid(value)

    def test_route_project_and_new_thread_are_exact(self):
        for change in ('from', 'to', 'thread_id', 'project'):
            value = deepcopy(self.value)
            if change == 'project': value['payload']['items'][0]['project'] = 'workspace:other'
            elif change == 'thread_id': value[change] = str(uuid4())
            else: value[change] = 'claude'
            self.assert_invalid(value)

    def test_secret_scan_rejects_before_any_storage_and_errors_hide_values(self):
        for secret in ('ghp_' + 'A' * 30, '-----BEGIN PRIVATE KEY-----', 'abandon ' * 11 + 'about', 'password: hidden-value', 'sk-' + 'abcd' * 10):
            value = deepcopy(self.value); value['payload']['items'][0]['what_changed'] = secret
            with self.assertRaises(m.MailboxError) as caught:
                m.store_message(self.store, m.encoded(value), 'fixture', 'stored')
            self.assertEqual(caught.exception.code, 3)
            self.assertNotIn(secret, str(caught.exception))
            self.assertFalse(self.store.exists())
        source = self.root / 'secret.json'; source.write_bytes(m.encoded(value))
        result = subprocess.run([sys.executable, '-I', '-B', str(m.ROOT / 'scripts/agent_mailbox.py'), 'validate', str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_sensitive_flags_code_paths_and_false_scan_marker(self):
        for marker in ('employer', 'ledger', 'private_code'):
            value = deepcopy(self.value); value['sensitivity'][marker] = True; self.assert_invalid(value, 3)
        for marker in ('employer_ledger', 'private_code', 'secrets', 'unreleased'):
            value = deepcopy(self.value); value['payload']['items'][0]['sensitivity'][marker] = True; self.assert_invalid(value, 3)
        for body in ('def work():', '/Users/private/file.txt', 'X7R+fQb6mPZ/8yKwJ5s0NEUvH1zIdcA4'):
            value = deepcopy(self.value); value['payload']['items'][0]['what_changed'] = body; self.assert_invalid(value, 3)
        value = deepcopy(self.value); value['sensitivity']['secrets_checked'] = False; self.assert_invalid(value)

    def test_hex_hashes_only_allowed_in_contract_hash_fields(self):
        value = deepcopy(self.value); value['payload']['items'][0]['what_changed'] = 'abc123ef' * 8
        self.assert_invalid(value, 3)
        value = deepcopy(self.value); value['payload']['items'][0]['commits'] = ['abc123ef' * 5]
        m.validate(m.encoded(value))
        report = reply(self.value, self.raw)
        m.validate(m.encoded(report))

    def test_public_references_and_required_proof(self):
        value = deepcopy(self.value)
        item = value['payload']['items'][0]
        item['postability'] = 2; self.assert_invalid(value)
        item['proof']['links'] = ['https://docs.python.org/3/library/json.html']; m.validate(m.encoded(value))
        for link in ('http://example.com', 'https://localhost/', 'https://127.0.0.1/', 'https://example.com/?key=hidden', 'https://user:pass@example.com/'):
            item['proof']['links'] = [link]; self.assert_invalid(value)
        item['proof']['links'] = []; item['postability'] = 0
        item['proof']['screenshots'] = ['../private.png']; self.assert_invalid(value)

    def test_expiry_is_visible_but_blocks_new_task_operations(self):
        value = deepcopy(self.value)
        value['created_at'] = (m.now() - timedelta(days=2)).isoformat(); value['payload']['created_at'] = value['created_at']
        value['expires_at'] = (m.now() - timedelta(days=1)).isoformat()
        m.validate(m.encoded(value))
        with self.assertRaisesRegex(m.MailboxError, 'expired_message'):
            m.store_message(self.store, m.encoded(value), 'fixture', 'stored')
        self.assertFalse(self.store.exists())

    def test_identical_retry_and_different_bytes_do_not_overwrite(self):
        self.assertFalse(self.save()['duplicate'])
        original_record = (self.store / (self.value['id'] + '.json')).read_bytes()
        self.assertTrue(self.save()['duplicate'])
        with self.assertRaisesRegex(m.MailboxError, 'conflicting_message_id'):
            m.store_message(self.store, self.raw + b' ', 'fixture', 'stored')
        self.assertEqual((self.store / (self.value['id'] + '.json')).read_bytes(), original_record)

    def test_concurrent_writers_publish_one_complete_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda _: self.save(), range(16)))
        self.assertEqual(sum(not row['duplicate'] for row in rows), 1)
        self.assertEqual(len(list(self.store.glob('*.json'))), 1)
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'local_only')

    def test_concurrent_cli_processes_create_one_lock_and_message(self):
        source = self.root / 'input.json'; source.write_bytes(self.raw)
        def write(_):
            return json.loads(self.invoke('store', str(source), '--session', 'fixture-task').stdout)
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(write, range(16)))
        self.assertEqual(sum(not row['duplicate'] for row in rows), 1)
        self.assertEqual(len(list(self.store.glob('*.json'))), 1)

    def test_no_clobber_publication_and_failure_before_link(self):
        output = self.root / 'attachment.json'; output.write_bytes(b'original')
        with m.directory(self.root) as fd:
            with self.assertRaises(FileExistsError): m.publish(fd, output.name, b'replacement')
            with patch.object(m.os, 'link', side_effect=OSError('simulated interruption')):
                with self.assertRaises(OSError): m.publish(fd, 'missing.json', b'complete')
        self.assertEqual(output.read_bytes(), b'original')
        self.assertFalse((self.root / 'missing.json').exists())
        self.assertFalse(list(self.root.glob('.pending-*')))

    def test_dry_runs_never_create_store_lock_attachment_or_rewrite(self):
        self.assertEqual(m.store_message(self.store, self.raw, 'fixture', 'stored', True)['state'], 'dry_run')
        self.assertFalse(self.store.exists())
        self.save()
        before = {p.name: p.read_bytes() for p in self.store.iterdir()}
        output = self.root / 'dry.json'
        m.export_message(self.store, self.value['id'], output, True, True)
        report = reply(self.value, self.raw)
        m.store_message(self.store, m.encoded(report), 'fixture', 'imported', True)
        self.assertFalse(output.exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.iterdir()})

    def test_missing_store_dry_run_rejects_alias_without_creating_anything(self):
        alias = self.root / 'alias'; alias.symlink_to(self.root)
        with self.assertRaises(m.MailboxError): m.store_message(alias / 'new-store', self.raw, 'fixture', 'stored', True)
        self.assertFalse((self.root / 'new-store').exists())
        broken = self.root / 'broken'; broken.symlink_to(self.root / 'absent')
        with self.assertRaises(m.MailboxError): m.list_messages(broken)

    def test_mixed_concurrent_bytes_cannot_overwrite_winner(self):
        other = deepcopy(self.value); other['payload']['items'][0]['title'] = 'Other candidate'
        raw_other = m.encoded(other)
        def write(index):
            try: return m.store_message(self.store, self.raw if index % 2 == 0 else raw_other, 'fixture', 'stored')
            except m.MailboxError as error: return {'error': error.reason}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(16)))
        self.assertEqual(sum(r.get('error') == 'conflicting_message_id' for r in results), 8)
        self.assertEqual(sum(r.get('duplicate') is False for r in results), 1)
        self.assertEqual(len(list(self.store.glob('*.json'))), 1)
        with m.directory(self.store,private=True) as fd:
            retained, _, _ = m.get_record(fd, self.value['id'])
        self.assertIn(retained, (self.raw, raw_other))

    def test_symlinks_traversal_nonregular_and_corrupt_records(self):
        source = self.root / 'input.json'; source.write_bytes(self.raw)
        alias = self.root / 'alias.json'; alias.symlink_to(source)
        with self.assertRaises((m.MailboxError, OSError)): m.read_file(alias)
        directory_alias = self.root / 'alias-dir'; directory_alias.symlink_to(self.root)
        with self.assertRaises(m.MailboxError): m.store_message(directory_alias / 'store', self.raw, 'fixture', 'stored')
        with self.assertRaises(m.MailboxError): m.store_message(self.root / '..' / 'store', self.raw, 'fixture', 'stored')
        self.save()
        path = self.store / (self.value['id'] + '.json'); path.chmod(0o600); path.write_bytes(b'{truncated')
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'unknown')
        (self.store / '.pending-interrupted').write_bytes(b'partial')
        self.assertIn('incomplete_write', {r['state'] for r in m.list_messages(self.store)})

    def test_hardlinks_fifo_and_public_store_are_rejected(self):
        source = self.root / 'input.json'; source.write_bytes(self.raw)
        alias = self.root / 'hardlink.json'; os.link(source, alias)
        with self.assertRaises(m.MailboxError): m.read_file(alias)
        fifo = self.root / 'fifo'; os.mkfifo(fifo)
        with self.assertRaises(m.MailboxError): m.read_file(fifo)
        self.store.mkdir(mode=0o755)
        with self.assertRaisesRegex(m.MailboxError, 'store_directory_must_be_private'): self.save()
        self.assertFalse(list(self.store.iterdir()))

    def test_lock_alias_is_rejected_without_touching_target(self):
        self.store.mkdir(mode=0o700)
        target = self.root / 'unrelated'; target.write_bytes(b'unchanged')
        (self.store / '.write-lock').symlink_to(target)
        with self.assertRaises(m.MailboxError): self.save()
        self.assertEqual(target.read_bytes(), b'unchanged')
        self.assertFalse(list(self.store.glob('*.json')))

    def test_retained_examples_follow_pilot_contract(self):
        examples = m.SCHEMAS / 'examples'
        social = (examples / 'social-packet.json').read_bytes()
        value = m.validate(social)
        m.store_message(self.store, social, 'example-task', 'stored')
        for name in ('receipt.json', 'request.json'):
            raw = (examples / name).read_bytes()
            m.validate(raw)
            m.import_reply(self.store, raw, 'example-task')
        with self.assertRaises(m.MailboxError): m.validate((examples / 'invalid-haru-example.json').read_bytes())
        row = next(r for r in m.list_messages(self.store) if r.get('id') == value['id'])
        self.assertEqual(row['state'], 'reported_stored')

    def test_contract_metadata_drift_never_reports_success(self):
        self.save()
        record = self.store / (self.value['id'] + '.json')
        data = json.loads(record.read_bytes()); data['sha256'] = '0' * 64
        record.chmod(0o600); record.write_bytes(m.encoded(data))
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'unknown')
        with self.assertRaises(m.MailboxError): m.export_message(self.store, self.value['id'], self.root / 'export.json', True)
        self.assertFalse((self.root / 'export.json').exists())

    def test_expired_request_rejected_without_advancing_original(self):
        self.save()
        report = reply(self.value, self.raw, kind='request')
        report['created_at'] = (m.now() - timedelta(seconds=20)).isoformat()
        report['expires_at'] = (m.now() - timedelta(seconds=10)).isoformat()
        with self.assertRaisesRegex(m.MailboxError, 'expired_message'): m.import_reply(self.store, m.encoded(report), 'fixture')
        self.assertEqual(next(r for r in m.list_messages(self.store) if r.get('kind') == 'social_packet')['state'], 'local_only')

    def test_receipts_require_original_hash_thread_and_checks(self):
        self.save()
        original = reply(self.value, self.raw)
        for change in ('hash', 'thread', 'target', 'checks', 'scope'):
            report = deepcopy(original)
            evidence = json.loads(report['payload']['reason'])
            if change == 'hash': evidence['content_sha256'] = '0' * 64
            if change == 'thread': report['thread_id'] = str(uuid4())
            if change == 'target': report['payload']['for_id'] = str(uuid4())
            if change == 'checks': evidence['checks'] = []
            if change == 'scope': evidence['scope'] = 'handled'
            report['payload']['reason'] = json.dumps(evidence)
            with self.assertRaises(m.MailboxError): m.store_message(self.store, m.encoded(report), 'fixture', 'imported')
        self.assertEqual(len(list(self.store.glob('*.json'))), 1)

    def test_receipt_replay_completion_and_contrary_reports(self):
        self.save()
        report = reply(self.value, self.raw)
        raw = m.encoded(report)
        m.store_message(self.store, raw, 'fixture', 'imported')
        self.assertTrue(m.store_message(self.store, raw, 'fixture', 'imported')['duplicate'])
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'reported_stored')
        done = reply(self.value, self.raw, 'done')
        m.store_message(self.store, m.encoded(done), 'fixture', 'imported')
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'reported_handled')
        reject = reply(self.value, self.raw, 'rejected')
        m.store_message(self.store, m.encoded(reject), 'fixture', 'imported')
        self.assertEqual(m.list_messages(self.store)[0]['state'], 'conflicting_reports')
        self.assertEqual(len(list(self.store.glob('*.json'))), 4)

    def test_receipt_semantic_replay_and_safe_rejection_record(self):
        self.save()
        report = reply(self.value, self.raw)
        m.import_reply(self.store, m.encoded(report), 'fixture')
        repeated = deepcopy(report); repeated['id'] = str(uuid4())
        result = m.import_reply(self.store, m.encoded(repeated), 'fixture')
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['retained_id'], report['id'])
        self.assertEqual(len(list(self.store.glob('*.json'))), 2)
        bad = reply(self.value, self.raw)
        evidence = json.loads(bad['payload']['reason']); evidence['content_sha256'] = '0' * 64
        bad['payload']['reason'] = json.dumps(evidence)
        with self.assertRaises(m.MailboxError): m.import_reply(self.store, m.encoded(bad), 'fixture')
        rows = m.list_messages(self.store)
        self.assertEqual(next(row for row in rows if row['state'] == 'rejected_import')['reason'], 'original_content_hash_mismatch')
        self.assertEqual(next(row for row in rows if row.get('kind') == 'social_packet')['state'], 'reported_stored')
        events = list(self.store.glob('.rejection-*.json'))
        self.assertEqual(len(events), 1)
        self.assertNotIn(bad['id'].encode(), events[0].read_bytes())

    def test_invalid_sensitive_reply_never_adds_rejection_log(self):
        self.save()
        request = reply(self.value, self.raw, kind='request'); request['payload']['ask'] = 'ghp_' + 'A' * 30
        before = sorted(p.name for p in self.store.iterdir())
        with self.assertRaises(m.MailboxError): m.import_reply(self.store, m.encoded(request), 'fixture')
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), before)

    def test_expired_parent_can_receive_later_receipt(self):
        self.value['expires_at'] = (m.now() + timedelta(seconds=1)).isoformat(); self.raw = m.encoded(self.value)
        self.save()
        report = reply(self.value, self.raw)
        future = m.now() + timedelta(minutes=10)
        with patch.object(m, 'now', return_value=future):
            m.import_reply(self.store, m.encoded(report), 'fixture')
            row = m.list_messages(self.store)[0]
            self.assertTrue(row['expired'])
            self.assertEqual(row['state'], 'reported_stored')

    def test_post_publication_interruption_retains_complete_bytes(self):
        def fail_directory(handle):
            if os.fstat(handle).st_mode & 0o170000 == 0o040000:
                raise OSError('simulated directory sync interruption')
            return original(handle)
        original = m.os.fsync
        with m.directory(self.root) as fd:
            with patch.object(m.os, 'fsync', side_effect=fail_directory):
                with self.assertRaises(OSError): m.publish(fd, 'complete.json', self.raw)
        self.assertEqual((self.root / 'complete.json').read_bytes(), self.raw)
        self.assertFalse(list(self.root.glob('.pending-*')))

    def test_discussion_request_is_data_and_unknown_parent_rejected(self):
        report = reply(self.value, self.raw, kind='request')
        with self.assertRaises(m.MailboxError): m.store_message(self.store, m.encoded(report), 'fixture', 'imported')
        self.save()
        m.store_message(self.store, m.encoded(report), 'fixture', 'imported')
        row = next(r for r in m.list_messages(self.store) if r['kind'] == 'request')
        self.assertEqual(row['state'], 'needs_review')
        self.assertFalse(row['delivery_authorized'])

    def test_restart_restore_and_actual_cli_attachment_flow(self):
        payload = self.root / 'payload.json'; payload.write_bytes(m.encoded(self.value['payload']))
        outgoing = self.root / 'outgoing.json'
        self.invoke('prepare', '--payload', str(payload), '--output', str(outgoing), '--dry-run')
        self.assertFalse(outgoing.exists())
        prepared = json.loads(self.invoke('prepare', '--payload', str(payload), '--output', str(outgoing)).stdout)
        original_bytes = outgoing.read_bytes(); original = json.loads(original_bytes)
        self.invoke('validate', str(outgoing))
        self.invoke('store', str(outgoing), '--session', 'fixture-task')
        attachment = self.root / 'attachment.json'
        self.invoke('export', prepared['id'], '--output', str(attachment), ok=False)
        self.invoke('export', prepared['id'], '--output', str(attachment), '--reviewed')
        self.assertEqual(attachment.read_bytes(), original_bytes)
        before = json.loads(self.invoke('list').stdout)
        self.assertEqual(before['messages'][0]['state'], 'attachment_ready')
        receipt = self.root / 'receipt.json'; receipt.write_bytes(m.encoded(reply(original, original_bytes)))
        self.invoke('import-reply', str(receipt), '--session', 'fixture-task')
        after = json.loads(self.invoke('list').stdout)
        self.assertEqual(after['messages'][0]['state'], 'reported_stored')
        restored = self.root / 'restored'; restored.mkdir(mode=0o700)
        for path in self.store.glob('*.json'): (restored / path.name).write_bytes(path.read_bytes())
        self.assertEqual(m.list_messages(restored)[0]['sha256'], hashlib.sha256(original_bytes).hexdigest())
        self.assertEqual(m.list_messages(restored)[0]['state'], 'reported_stored')
        export_again = self.root / 'restored-export.json'
        m.export_message(restored, prepared['id'], export_again, True)
        self.assertEqual(export_again.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()
