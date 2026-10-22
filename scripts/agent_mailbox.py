"""Offline Workspace Context/Haru attachment mailbox; never sends or executes work."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / 'schemas' / 'agent-mailbox'
PROJECT = 'workspace:40_Code/infrastructure/workspace-context'
LIMIT = 262144
KINDS = ('social_packet', 'receipt', 'request')


class MailboxError(ValueError):
    def __init__(self, reason, field='$', code=2):
        self.reason, self.field, self.code = reason, field, code
        super().__init__(reason)


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise MailboxError('duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise MailboxError('nonfinite_json_number')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise MailboxError('invalid_json') from None


def date(value):
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
            raise ValueError()
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise MailboxError('invalid_date') from None


def uuid(value):
    try:
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise MailboxError('invalid_uuid4') from None


def public_url(value):
    try:
        u = urlsplit(value)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query:
            return False
        if u.hostname == 'localhost' or '.' not in u.hostname or u.hostname.endswith(('.local', '.internal')):
            return False
        try:
            if not ipaddress.ip_address(u.hostname).is_global:
                return False
        except ValueError:
            pass
        return u.port in (None, 443)
    except ValueError:
        return False


def format_ok(value, kind):
    if kind == 'uuid4':
        uuid(value)
        return True
    if kind == 'date-time':
        date(value)
        return True
    if kind == 'sha256':
        return bool(re.fullmatch('[0-9a-f]{64}', value))
    if kind == 'git-sha':
        return bool(re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value))
    if kind == 'public-url-or-empty' and value == '':
        return True
    if kind == 'public-url-or-relative' and not value.startswith('https://'):
        return bool(re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', value)) and not {'.', '..'} & set(value.split('/'))
    if kind in ('public-url', 'public-url-or-empty', 'public-url-or-relative'):
        return public_url(value)
    raise MailboxError('unsupported_schema_format')


def schema_check(value, rule, field='$'):
    if 'anyOf' in rule:
        for option in rule['anyOf']:
            try:
                schema_check(value, option, field)
                return
            except MailboxError:
                pass
        raise MailboxError('schema_union', field)
    if 'enum' in rule and not any(type(value) is type(v) and value == v for v in rule['enum']):
        raise MailboxError('schema_enum', field)
    kind = rule.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    if kind and type(value) is not types[kind]:
        raise MailboxError('schema_type', field)
    if kind == 'object':
        properties = rule.get('properties', {})
        if any(key not in value for key in rule.get('required', [])):
            raise MailboxError('missing_required_field', field)
        if rule.get('additionalProperties') is False and set(value) - set(properties):
            raise MailboxError('unknown_field', field)
        for key, subrule in properties.items():
            if key in value:
                schema_check(value[key], subrule, field + '.' + key)
    if kind == 'array':
        if len(value) > rule['maxItems']:
            raise MailboxError('too_many_items', field)
        for index, item in enumerate(value):
            schema_check(item, rule['items'], f'{field}[{index}]')
    if kind == 'string':
        if not rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', LIMIT):
            raise MailboxError('string_length', field)
        if 'format' in rule:
            try:
                good = format_ok(value, rule['format'])
            except MailboxError:
                good = False
            if not good:
                raise MailboxError('invalid_format', field)
    if kind == 'integer' and not rule['minimum'] <= value <= rule['maximum']:
        raise MailboxError('integer_range', field)


def schema(name):
    return decode((SCHEMAS / (name + '.v1.json')).read_bytes())


def scan(value, field='$'):
    if isinstance(value, dict):
        for key, item in value.items():
            # Do not echo untrusted keys in errors.
            scan(key, field + '.object-key')
            safe = key if re.fullmatch('[a-z][a-z0-9_]{0,39}', key) else 'field'
            scan(item, field + '.' + safe)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan(item, f'{field}[{index}]')
    elif isinstance(value, str):
        if field == '$.payload.reason' and value.startswith('{'):
            scan(decode(value.encode()), field)
            return
        patterns = (
            r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}',
            r'\bAKIA[A-Z0-9]{16}\b', r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',
            r'(?i)\b(?:password|api[_ -]?key|private[_ -]?key|secret|token|mnemonic|seed phrase)\s*[:=]\s*\S+',
            r'\babandon(?: abandon){10} about\b',
            r'\b(?:def \w+\(|class \w+[:(]|import os\b|function \w+\(|const \w+\s*=)',
            r'/Users/|/home/|file://|\.env\b',
        )
        if any(re.search(pattern, value) for pattern in patterns):
            raise MailboxError('sensitive_or_code_content', field, 3)
        for token in re.findall(r'[A-Za-z0-9_+/=-]{32,}', value):
            if re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', token):
                if field == '$.payload.reason.content_sha256' or re.fullmatch(r'\$\.payload\.items\[\d+\]\.commits\[\d+\]', field):
                    continue
                if len(token) == 64:
                    raise MailboxError('secret_like_hex_string', field, 3)
            # Public link strings and SHA fields are inspected by format rules;
            # recognizable credential patterns are still rejected above.
            if value.startswith('https://') and public_url(value):
                continue
            entropy = -sum((token.count(c) / len(token)) * math.log2(token.count(c) / len(token)) for c in set(token))
            if entropy > 4.4:
                raise MailboxError('secret_like_string', field, 3)


def validate(raw, current=None, allow_expired=True):
    if len(raw) > LIMIT:
        raise MailboxError('input_too_large')
    value = decode(raw)
    scan(value)
    schema_check(value, schema('envelope'))
    kind = value['kind']
    if kind not in KINDS:
        raise MailboxError('unsupported_kind', '$.kind')
    schema_check(value['payload'], schema(kind), '$.payload')
    direction = ('codex', 'grokbot') if kind == 'social_packet' else ('grokbot', 'codex')
    if (value['from'], value['to']) != direction:
        raise MailboxError('wrong_pilot_route')
    if any(value['sensitivity'][k] for k in ('employer', 'ledger', 'private_code')):
        raise MailboxError('sensitivity_not_allowed', '$.sensitivity', 3)
    if value['sensitivity']['secrets_checked'] is not True:
        raise MailboxError('scan_marker_required', '$.sensitivity')
    current = current or now()
    if date(value['created_at']) > current + timedelta(minutes=5):
        raise MailboxError('future_creation_date')
    expiry = value['expires_at']
    if expiry and date(expiry) < date(value['created_at']):
        raise MailboxError('expiry_before_creation')
    if expiry and date(expiry) <= current and not allow_expired and kind != 'receipt':
        raise MailboxError('expired_message')
    payload = value['payload']
    if kind == 'social_packet':
        if payload['created_at'] != value['created_at']:
            raise MailboxError('packet_date_mismatch')
        if payload['cadence'] == 'event' and not payload['items']:
            raise MailboxError('event_requires_item')
        for item in payload['items']:
            if any(item['sensitivity'].values()):
                raise MailboxError('sensitivity_not_allowed', '$.payload.items.sensitivity', 3)
            if item['time_sensitive'] and not item['time_sensitive_reason']:
                raise MailboxError('timing_reason_required')
            if not item['time_sensitive'] and item['time_sensitive_reason'] is not None:
                raise MailboxError('unexpected_timing_reason')
            if (item['format_suggestion'] == 'double_tap') != (item['related_thread_url'] is not None):
                raise MailboxError('thread_link_mismatch')
            if item['postability'] >= 2 and not (item['proof']['links'] or item['proof']['screenshots'] or item['proof']['metrics']):
                raise MailboxError('postability_requires_proof')
    elif not value['reply_to']:
        raise MailboxError('reply_binding_required')
    if value['reply_to'] is None and value['thread_id'] != value['id']:
        raise MailboxError('new_thread_identity_mismatch')
    if kind == 'receipt':
        evidence = decode(payload['reason'].encode())
        schema_check(evidence, schema('receipt-evidence'), '$.payload.reason')
        if payload['for_id'] != value['reply_to']:
            raise MailboxError('receipt_target_mismatch')
        if evidence['scope'] != {'ack': 'stored', 'done': 'handled', 'rejected': 'rejected'}[payload['status']]:
            raise MailboxError('receipt_scope_mismatch')
        checks = evidence['checks']
        if len({c['name'] for c in checks}) != len(checks):
            raise MailboxError('duplicate_receipt_check')
        required = {'validation', 'storage'} | ({'handling'} if payload['status'] == 'done' else set())
        if payload['status'] != 'rejected' and (not required <= {c['name'] for c in checks} or not all(c['passed'] for c in checks)):
            raise MailboxError('receipt_checks_missing_or_failed')
        event = date(payload['at'])
        if event > date(value['created_at']) or event > current + timedelta(minutes=5):
            raise MailboxError('receipt_event_date_mismatch')
    return value


@contextmanager
def directory(path, create=False, private=False):
    p = Path(path).absolute()
    if '..' in p.parts:
        raise MailboxError('unsafe_path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if os.fstat(fd).st_uid != os.getuid():
            raise MailboxError('directory_not_owned')
        if private and stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            raise MailboxError('store_directory_must_be_private')
        yield fd
    except OSError:
        raise MailboxError('unsafe_or_unavailable_path') from None
    finally:
        os.close(fd)


def read_at(fd, name, limit=LIMIT):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or before.st_uid != os.getuid() or before.st_nlink != 1:
            raise MailboxError('invalid_input_file')
        with os.fdopen(handle, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(handle)
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (before.st_dev, before.st_ino):
            raise MailboxError('input_path_changed')
        if len(raw) > limit or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise MailboxError('input_changed_or_too_large')
        return raw
    finally:
        os.close(handle)


def read_file(path):
    p = Path(path).absolute()
    with directory(p.parent) as fd:
        return read_at(fd, p.name)


def publish(fd, name, raw):
    """Publish fully fsynced bytes via atomic no-replace hard link, never rename."""
    temp = '.pending-' + str(uuid4())
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(handle)
        os.fchmod(handle, 0o400)
        os.fsync(handle)
        os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.close(handle)
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)


def session(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        raise MailboxError('invalid_local_session')
    return value


def check_missing_root(root):
    path = Path(root).absolute()
    if '..' in path.parts or path.is_symlink():
        raise MailboxError('unsafe_path')
    ancestor = path.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    with directory(ancestor):
        pass


def get_record(fd, identity):
    uuid(identity)
    record = decode(read_at(fd, identity + '.json', LIMIT * 2))
    if set(record) != {'message', 'sha256', 'project', 'session', 'recorded_at', 'operation'}:
        raise MailboxError('invalid_stored_record')
    if record['project'] != PROJECT or record['operation'] not in ('stored', 'imported'):
        raise MailboxError('invalid_stored_custody')
    session(record['session'])
    date(record['recorded_at'])
    try:
        raw = base64.b64decode(record['message'], validate=True)
    except (ValueError, TypeError):
        raise MailboxError('invalid_stored_bytes') from None
    value = validate(raw)
    if (record['operation'] == 'stored') != (value['kind'] == 'social_packet'):
        raise MailboxError('stored_direction_mismatch')
    if value['id'] != identity or digest(raw) != record['sha256']:
        raise MailboxError('stored_identity_or_hash_mismatch')
    return raw, value, record


def binding(fd, value):
    if value['reply_to'] is None:
        return
    try:
        original_raw, original, _ = get_record(fd, value['reply_to'])
    except FileNotFoundError:
        raise MailboxError('original_message_missing') from None
    if original['kind'] != 'social_packet' or value['thread_id'] != original['thread_id']:
        raise MailboxError('wrong_thread_or_original_kind')
    if date(value['created_at']) < date(original['created_at']):
        raise MailboxError('reply_before_original')
    if value['kind'] == 'receipt':
        evidence = decode(value['payload']['reason'].encode())
        if evidence['content_sha256'] != digest(original_raw):
            raise MailboxError('original_content_hash_mismatch')
        if date(value['payload']['at']) < date(original['created_at']):
            raise MailboxError('receipt_before_original')


@contextmanager
def locked(fd):
    # Separate exclusive creation from opening an existing lock: macOS can return
    # ENOENT for simultaneous O_CREAT|O_NOFOLLOW opens of the same absent file.
    try:
        handle = os.open('.write-lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    except FileExistsError:
        handle = os.open('.write-lock', os.O_RDWR | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise MailboxError('invalid_lock_file')
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def summary(value, raw):
    payload = value['payload']
    title = (payload['items'][0]['title'] if payload['items'] else 'Empty daily/weekly packet') if value['kind'] == 'social_packet' else payload.get('title', 'Haru receipt')
    return {'id': value['id'], 'kind': value['kind'], 'title': title, 'project': PROJECT,
            'from': value['from'], 'to': value['to'], 'sha256': digest(raw),
            'created_at': value['created_at'], 'delivery_authorized': False}


def store_message(root, raw, local_session, operation, dry_run=False):
    value = validate(raw, allow_expired=False)
    session(local_session)
    if (operation == 'stored') != (value['kind'] == 'social_packet'):
        raise MailboxError('wrong_operation_for_direction')
    result = summary(value, raw)
    if value['reply_to'] and not Path(root).exists():
        raise MailboxError('original_message_missing')
    if dry_run and not Path(root).exists():
        check_missing_root(root)
        return dict(result, state='dry_run', duplicate=False)
    with directory(root, create=not dry_run, private=True) as fd:
        def inspect():
            binding(fd, value)
            try:
                old, _, _ = get_record(fd, value['id'])
            except FileNotFoundError:
                if value['kind'] == 'receipt':
                    for name in os.listdir(fd):
                        if re.fullmatch(r'[0-9a-f-]{36}\.json', name):
                            _, previous, _ = get_record(fd, name[:-5])
                            if previous['kind'] == 'receipt' and previous['reply_to'] == value['reply_to'] and previous['payload'] == value['payload']:
                                return previous['id']
                return None
            if old != raw:
                raise MailboxError('conflicting_message_id')
            return value['id']
        if dry_run:
            retained = inspect()
            return dict(result, state='dry_run', duplicate=bool(retained), retained_id=retained)
        with locked(fd):
            retained = inspect()
            if not retained:
                record = {'message': base64.b64encode(raw).decode(), 'sha256': digest(raw),
                          'project': PROJECT, 'session': local_session, 'recorded_at': stamp(), 'operation': operation}
                publish(fd, value['id'] + '.json', encoded(record))
            return dict(result, state='stored_locally' if operation == 'stored' else 'imported_report', duplicate=bool(retained), retained_id=retained or value['id'])


def import_reply(root, raw, local_session, dry_run=False):
    try:
        return store_message(root, raw, local_session, 'imported', dry_run)
    except MailboxError as error:
        # No payload/filename/hash is copied on rejection, and secrets write nothing.
        if not dry_run and error.code != 3 and Path(root).exists():
            with directory(root, private=True) as fd:
                with locked(fd):
                    publish(fd, '.rejection-' + str(uuid4()) + '.json', encoded({'reason': error.reason, 'field': error.field, 'recorded_at': stamp()}))
        raise


def export_message(root, identity, output, reviewed=False, dry_run=False):
    if not reviewed:
        raise MailboxError('local_content_review_required')
    with directory(root, private=True) as fd:
        raw, value, _ = get_record(fd, identity)
        validate(raw, allow_expired=False)
        binding(fd, value)
    p = Path(output).absolute()
    with directory(p.parent) as dest:
        try:
            existing = read_at(dest, p.name)
        except FileNotFoundError:
            existing = None
        if existing is not None and existing != raw:
            raise MailboxError('attachment_destination_conflict')
        if not dry_run and existing is None:
            try:
                publish(dest, p.name, raw)
            except FileExistsError:
                if read_at(dest, p.name) != raw:
                    raise MailboxError('attachment_destination_conflict') from None
    if not dry_run:
        with directory(root, private=True) as fd:
            with locked(fd):
                name = '.' + identity + '.exported.json'
                try:
                    previous = decode(read_at(fd, name))
                    if set(previous) != {'id', 'sha256', 'recorded_at'} or previous['id'] != identity or previous['sha256'] != digest(raw):
                        raise MailboxError('invalid_export_custody')
                    date(previous['recorded_at'])
                except FileNotFoundError:
                    publish(fd, name, encoded({'id': identity, 'sha256': digest(raw), 'recorded_at': stamp()}))
    return dict(summary(value, raw), state='dry_run' if dry_run else 'attachment_ready', duplicate=existing is not None)


def list_messages(root):
    if not Path(root).exists():
        check_missing_root(root)
        return []
    rows, receipts = [], {}
    with directory(root, private=True) as fd:
        for name in sorted(os.listdir(fd)):
            if name.startswith('.pending-'):
                rows.append({'state': 'incomplete_write', 'explanation': 'Interrupted staging file retained; no handling inferred.'})
                continue
            if name.startswith('.'):
                if name.startswith('.rejection-'):
                    try:
                        event = decode(read_at(fd, name))
                        if set(event) != {'reason', 'field', 'recorded_at'} or not isinstance(event['reason'], str) or not re.fullmatch('[a-z_]+', event['reason']):
                            raise MailboxError('invalid_rejection_custody')
                        date(event['recorded_at'])
                        rows.append({'state': 'rejected_import', 'reason': event['reason'], 'recorded_at': event['recorded_at'], 'explanation': 'A reply import failed checks. Its payload was not retained.'})
                    except (MailboxError, OSError, TypeError, KeyError):
                        rows.append({'state': 'unknown', 'explanation': 'Invalid rejection record; no result inferred.'})
                continue
            if not name.endswith('.json'):
                continue
            try:
                raw, value, record = get_record(fd, name[:-5])
                binding(fd, value)
                row = dict(summary(value, raw), recorded_at=record['recorded_at'], session=record['session'], state='needs_review' if value['kind'] == 'request' else 'local_only',
                           explanation='Discussion request imported; no work authorized.' if value['kind'] == 'request' else 'Stored locally. Remote receipt is missing.')
                if value['kind'] == 'receipt':
                    receipts.setdefault(value['reply_to'], []).append(value)
                else:
                    row['expired'] = bool(value['expires_at'] and date(value['expires_at']) <= now())
                    try:
                        event = decode(read_at(fd, '.' + value['id'] + '.exported.json'))
                    except FileNotFoundError:
                        event = None
                    if event is not None:
                        if set(event) != {'id', 'sha256', 'recorded_at'} or event['id'] != value['id'] or event['sha256'] != digest(raw):
                            raise MailboxError('invalid_export_custody')
                        date(event['recorded_at'])
                        row.update(state='attachment_ready', exported_at=event['recorded_at'], explanation='Attachment prepared. Remote receipt is missing; delivery remains unknown.')
                    rows.append(row)
            except (MailboxError, OSError, TypeError, KeyError):
                rows.append({'state': 'unknown', 'explanation': 'Unreadable or invalid retained record; no success inferred.'})
    for row in rows:
        reports = receipts.get(row.get('id'), [])
        if not reports:
            continue
        statuses = {r['payload']['status'] for r in reports}
        evidence_by_status = {}
        for report in reports:
            evidence_by_status.setdefault(report['payload']['status'], set()).add(report['payload']['reason'])
        if {'done', 'rejected'} <= statuses or any(len(v) > 1 for v in evidence_by_status.values()):
            row.update(state='conflicting_reports', explanation='Haru reports differ. Review them; no settled result inferred.')
        elif 'done' in statuses:
            row.update(state='reported_handled', explanation='Haru reports message handling. Task acceptance is separate.')
        elif 'rejected' in statuses:
            row.update(state='reported_rejected', explanation='Haru reports rejection; no work completion inferred.')
        else:
            row.update(state='reported_stored', explanation='Haru reports storage. Handling is not confirmed.')
        row['receipt_ids'] = [r['id'] for r in reports]
        row['reported_at'] = max((r['payload']['at'] for r in reports), key=date)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', default=str(ROOT / 'data' / 'mailbox'))
    subs = parser.add_subparsers(dest='command', required=True)
    prepare = subs.add_parser('prepare')
    prepare.add_argument('--payload', required=True)
    prepare.add_argument('--output', required=True)
    prepare.add_argument('--dry-run', action='store_true')
    check = subs.add_parser('validate')
    check.add_argument('file')
    for command in ('store', 'import-reply'):
        item = subs.add_parser(command)
        item.add_argument('file')
        item.add_argument('--session', required=True)
        item.add_argument('--dry-run', action='store_true')
    export = subs.add_parser('export')
    export.add_argument('id')
    export.add_argument('--output', required=True)
    export.add_argument('--reviewed', action='store_true')
    export.add_argument('--dry-run', action='store_true')
    subs.add_parser('list')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            payload = decode(read_file(args.payload))
            scan(payload)
            identity = str(uuid4())
            value = {'version': 1, 'id': identity, 'from': 'codex', 'to': 'grokbot', 'kind': 'social_packet',
                     'created_at': payload['created_at'], 'reply_to': None, 'thread_id': identity, 'priority': 'low',
                     'expires_at': None, 'sensitivity': {'employer': False, 'ledger': False, 'private_code': False, 'secrets_checked': True}, 'payload': payload}
            raw = encoded(value)
            validate(raw, allow_expired=False)
            output = Path(args.output).absolute()
            with directory(output.parent) as fd:
                try:
                    read_at(fd, output.name)
                except FileNotFoundError:
                    if not args.dry_run:
                        publish(fd, output.name, raw)
                else:
                    raise MailboxError('prepare_destination_exists')
            result = dict(summary(value, raw), state='dry_run' if args.dry_run else 'prepared')
        elif args.command == 'validate':
            raw = read_file(args.file)
            value = validate(raw)
            if value['reply_to']:
                with directory(args.store, private=True) as fd:
                    binding(fd, value)
            result = dict(summary(value, raw), state='valid', expired=bool(value['expires_at'] and date(value['expires_at']) <= now()))
        elif args.command in ('store', 'import-reply'):
            raw = read_file(args.file)
            result = store_message(args.store, raw, args.session, 'stored', args.dry_run) if args.command == 'store' else import_reply(args.store, raw, args.session, args.dry_run)
        elif args.command == 'export':
            result = export_message(args.store, args.id, args.output, args.reviewed, args.dry_run)
        else:
            result = {'messages': list_messages(args.store), 'delivery_authorized': False}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MailboxError as error:
        print(json.dumps({'error': error.reason, 'field': error.field}), file=sys.stderr)
        return error.code
    except (OSError, TypeError, KeyError, RecursionError, ValueError):
        print(json.dumps({'error': 'invalid_or_unavailable_input', 'field': '$'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
