import shutil
from pathlib import Path
from django.core.management.utils import get_random_secret_key

import logging
logger = logging.getLogger(__name__)



DEFAULT_PLACEHOLDER = '__REPLACE_ME__'
SAMPLE_NAME = '.env.sample'


def ensure_env(env_path: Path, placeholder: str = DEFAULT_PLACEHOLDER) -> str:
    """Make sure `.env` exists and has a real SECRET_KEY, and return that key.

    A missing `.env` is copied from `.env.sample` beside it, or created with only a key if the
    sample is gone too. A blank or placeholder key is replaced with a random one. Nothing else
    in an existing file is changed.
    """
    if not env_path.exists():
        sample = env_path.with_name(SAMPLE_NAME)
        if sample.exists():
            shutil.copyfile(sample, env_path)
            logger.info(f'Created .env from {SAMPLE_NAME}.')
        else:
            new_key = get_random_secret_key()
            env_path.write_text(f'SECRET_KEY={new_key}\n')
            logger.info('Created new .env file with random SECRET_KEY.')
            return new_key

    # Read current lines
    lines = env_path.read_text().splitlines()
    key_line_idx = None
    for idx, line in enumerate(lines):
        if line.startswith('SECRET_KEY='):
            key_line_idx = idx
            key_value = line.split('=', 1)[1].strip()
            if key_value and key_value != placeholder:
                return key_value
            break

    # Generate and insert new key
    new_key = get_random_secret_key()
    if key_line_idx is not None:
        lines[key_line_idx] = f'SECRET_KEY={new_key}'
    else:
        lines.append(f'SECRET_KEY={new_key}')

    env_path.write_text('\n'.join(lines) + '\n')
    logger.info('Updated SECRET_KEY in .env file.')
    return new_key
