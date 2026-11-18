"""Local secret entry; no API requests and no secret command-line arguments."""
import getpass
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings

from . import CollectionError


ENV_NAME='OLLAMA_API_KEY'


def secret_path():
    return Path.cwd()/'data'/'secrets'/'ollama.key'


def _check_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise CollectionError('credential_directory_unsafe')


def _validate_key(value):
    if (not isinstance(value,str) or not value or len(value)>4096
        or any(ord(c)<33 or ord(c)>126 for c in value)):
        raise CollectionError('credential_value_invalid')
    return value


def _safe_parents(path):
    _check_directory(path.parent.parent)
    _check_directory(path.parent)
    if path.is_symlink():
        raise CollectionError('credential_file_unsafe')


def save_key(value):
    value=_validate_key(value)
    path=secret_path();_safe_parents(path)
    path.parent.parent.mkdir(mode=0o700,exist_ok=True)
    path.parent.mkdir(mode=0o700,exist_ok=True)
    os.chmod(path.parent,0o700)
    _safe_parents(path)
    temp=None
    try:
        fd,temp=tempfile.mkstemp(prefix='.ollama-',dir=path.parent)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(value+'\n')
        os.replace(temp,path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def read_key():
    """Environment takes precedence; never report a value in diagnostics."""
    value=os.environ.get(ENV_NAME)
    if value:
        return _validate_key(value)
    path=secret_path();_safe_parents(path)
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'r',encoding='utf-8') as stream:
            mode=os.fstat(stream.fileno())
            if (not stat.S_ISREG(mode.st_mode) or mode.st_uid!=os.getuid()
                or mode.st_mode & 0o077 or mode.st_size>4097):
                raise CollectionError('credential_permissions_unsafe')
            return _validate_key(stream.read(4097).strip())
    except (OSError,UnicodeError):
        raise CollectionError('ollama_key_missing_or_unreadable') from None


def command(args):
    try:
        if args.action=='set':
            if not sys.stdin.isatty():
                raise CollectionError('credential_requires_local_interactive_terminal')
            # Refuse getpass's fallback that could echo secrets to a piped stream.
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                value=getpass.getpass('Ollama API key (화면에 표시되지 않음): ')
                confirmation=getpass.getpass('동일한 키를 한 번 더 입력: ')
            if value!=confirmation:
                raise CollectionError('credential_confirmation_mismatch')
            save_key(value)
            print('Ollama 키를 로컬 data/secrets/ollama.key에 저장했습니다 (권한600).')
            print('키 확인/모델 호출은 하지 않았습니다. 키 값을 채팅에 보내지 마세요.')
        else:
            read_key()
            print('Ollama 키: 사용 준비됨 (값 비공개, 유효성/API 접속은 아직 미검증)')
        return 0
    except (CollectionError,OSError,getpass.GetPassWarning):
        print('키 설정/확인 실패: 로컬 터미널에서 입력·확인 값과 파일 권한을 확인하세요. 키는 출력하지 않습니다.',file=sys.stderr)
        return 1
    except (KeyboardInterrupt,EOFError):
        print('\n키 입력을 취소했습니다. API 호출 없음.',file=sys.stderr)
        return 1
