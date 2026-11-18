"""Private Telegram pairing; token input stays in the local terminal and file."""

import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import tomllib
import warnings

import httpx

from . import CollectionError

MAX_BYTES = 1024 * 1024
TOKEN_PATTERN = re.compile(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,200}')


def secret_path(root=None):
    return (Path(root) if root is not None else Path.cwd()) / 'data' / 'secrets' / 'telegram.json'


def _token(value):
    if not isinstance(value, str) or not TOKEN_PATTERN.fullmatch(value):
        raise CollectionError('telegram_token_invalid')
    return value


def _chat_id(value):
    # This setup deliberately supports a single private chat, not groups/channels.
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value):
        raise CollectionError('telegram_private_chat_invalid')
    return value


def _safe_parents(path):
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise CollectionError('telegram_credential_directory_unsafe')
        if parent.exists() and parent.stat().st_uid != os.getuid():
            raise CollectionError('telegram_credential_owner_unsafe')
    if path.is_symlink():
        raise CollectionError('telegram_credential_file_unsafe')


def save_credentials(token, chat_id, root=None):
    value = {'token': _token(token), 'chat_id': _chat_id(chat_id)}
    path = secret_path(root)
    _safe_parents(path)
    path.parent.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    _safe_parents(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.telegram-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_credentials(root=None):
    path = secret_path(root)
    _safe_parents(path)
    try:
        if path.parent.stat().st_mode & 0o077:
            raise CollectionError('telegram_credential_permissions_unsafe')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 4096):
                raise CollectionError('telegram_credential_permissions_unsafe')
            value = json.load(stream)
        if not isinstance(value, dict) or set(value) != {'token', 'chat_id'}:
            raise ValueError
        return {'token': _token(value['token']), 'chat_id': _chat_id(value['chat_id'])}
    except (OSError, UnicodeError, ValueError, TypeError):
        raise CollectionError('telegram_credentials_missing_or_unreadable') from None


def request_api(token, method, payload):
    """Only onboarding read methods; never expose token-bearing HTTP errors."""
    _token(token)
    if method not in {'getMe', 'getUpdates'}:
        raise CollectionError('telegram_setup_method_invalid')
    try:
        with httpx.stream('POST', f'https://api.telegram.org/bot{token}/{method}',
                          json=payload, timeout=20, follow_redirects=False,
                          trust_env=False) as response:
            if response.is_redirect:
                raise CollectionError('telegram_setup_redirect_blocked')
            if response.status_code == 409:
                raise CollectionError('telegram_setup_updates_conflict')
            if response.status_code in {401, 403, 404}:
                raise CollectionError('telegram_setup_auth_failed')
            if response.status_code != 200:
                raise CollectionError('telegram_setup_http_failed')
            chunks = []
            size = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise CollectionError('telegram_setup_response_too_large')
                chunks.append(chunk)
        data = json.loads(b''.join(chunks))
        if not isinstance(data, dict) or data.get('ok') is not True or 'result' not in data:
            raise ValueError
        return data['result']
    except (httpx.RequestError, UnicodeError):
        raise CollectionError('telegram_setup_network_error') from None
    except (ValueError, TypeError):
        raise CollectionError('telegram_setup_response_invalid') from None


def _paired_chat(updates, command, bot_username):
    if not isinstance(updates, list):
        raise CollectionError('telegram_setup_response_invalid')
    chats = set()
    allowed = {command, command.replace('/start ', f'/start@{bot_username} ', 1)}
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get('message')
        if (not isinstance(message, dict) or not isinstance(message.get('text'), str)
                or message['text'] not in allowed):
            continue
        chat, sender = message.get('chat'), message.get('from')
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        identity = chat.get('id')
        if (chat.get('type') == 'private' and type(identity) is int and identity > 0
                and type(sender.get('id')) is int and sender['id'] == identity
                and sender.get('is_bot') is False):
            chats.add(_chat_id(str(identity)))
    if not chats:
        raise CollectionError('telegram_setup_pairing_not_found')
    if len(chats) != 1:
        raise CollectionError('telegram_setup_pairing_ambiguous')
    return chats.pop()


def _root(args):
    path = getattr(args, 'config', None)
    if path is None:
        return Path.cwd()
    try:
        path = Path(path).resolve()
        with path.open('rb') as handle:
            config = tomllib.load(handle)['telegram']
        relative = config.get('project_root', '..')
        if not isinstance(relative, str) or not relative:
            raise ValueError
        return (path.parent / relative).resolve()
    except (OSError, ValueError, TypeError, KeyError):
        raise CollectionError('telegram_setup_config_invalid') from None


_MESSAGES = {
    'telegram_setup_pairing_not_found': '연결 코드가 없습니다. 표시된 명령을 봇의 개인 채팅에 보낸 뒤 다시 설정하세요.',
    'telegram_setup_pairing_ambiguous': '같은 연결 코드가 여러 개인 채팅에서 발견되어 저장하지 않았습니다.',
    'telegram_setup_updates_conflict': '업데이트 조회가 충돌했습니다. 기존 webhook/다른 polling 사용 여부를 확인하세요. 기존 설정은 변경하지 않았습니다.',
    'telegram_setup_auth_failed': '봇 인증에 실패했습니다. BotFather의 토큰을 확인하세요.',
    'telegram_setup_network_error': 'Telegram 연결에 실패했습니다. 네트워크를 확인하고 다시 설정하세요.',
}


def setup(args):
    try:
        root = _root(args)
        if not sys.stdin.isatty():
            raise CollectionError('telegram_setup_requires_local_terminal')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Telegram bot token (화면에 표시되지 않음): ')
            confirmation = getpass.getpass('동일한 토큰을 한 번 더 입력: ')
        if token != confirmation:
            raise CollectionError('telegram_setup_confirmation_mismatch')
        _token(token)
        bot = request_api(token, 'getMe', {})
        if (not isinstance(bot, dict) or bot.get('is_bot') is not True
                or type(bot.get('id')) is not int or bot['id'] <= 0
                or not isinstance(bot.get('username'), str)
                or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot['username'])):
            raise CollectionError('telegram_setup_response_invalid')
        command = '/start IPO-' + secrets.token_urlsafe(12)
        print('본인이 만든 봇의 개인 채팅에 다음 일회용 연결 명령을 보내세요:')
        print(command)
        input('명령을 보낸 뒤 Enter를 누르세요: ')
        updates = request_api(token, 'getUpdates', {'limit': 100, 'timeout': 0, 'allowed_updates': ['message']})
        chat_id = _paired_chat(updates, command, bot['username'])
        save_credentials(token, chat_id, root)
        print('Telegram 개인 채팅 연결 정보를 로컬에 저장했습니다 (토큰/채팅 ID 비공개, 파일 권한600).')
        print('메시지 전송 및 예약 알림 활성화는 하지 않았습니다.')
        return 0
    except CollectionError as exc:
        print('Telegram 설정 실패: ' + _MESSAGES.get(str(exc), '입력 확인값·설정 파일·로컬 권한을 확인하세요. 비밀값은 출력하지 않습니다.'), file=sys.stderr)
        return 1
    except (OSError, getpass.GetPassWarning):
        print('Telegram 설정 실패: 로컬 터미널과 파일 권한을 확인하세요. 비밀값은 출력하지 않습니다.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nTelegram 설정을 취소했습니다. 메시지 전송 없음.', file=sys.stderr)
        return 1


def status(args):
    try:
        read_credentials(_root(args))
        print('Telegram: 연결 정보 준비됨 (값 비공개, 실제 전송은 별도 확인 필요)')
        return 0
    except (CollectionError, OSError):
        print('Telegram: 연결 정보 없음 또는 안전한 읽기 불가 (값 비공개)')
        return 1
