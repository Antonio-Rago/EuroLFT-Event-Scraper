"""Read an environment key or configure a private local key with hidden input."""

import getpass
import os
from pathlib import Path
import re
import stat
import tempfile
import warnings


ROOT = Path(__file__).resolve().parents[2] / "_event_collector"


def key_path(root=ROOT):
    return root / "local/credentials/gemini_api_key.txt"


def check_key(key):
    # Keys are opaque secrets, not a fixed vendor prefix/length/alphabet. Allow
    # printable header-safe ASCII, including dots in newer authorization keys.
    # Local checks cannot determine whether Google accepts the credential.
    if not key:
        raise ValueError("No key was entered. Paste the key at the hidden prompt, then press Enter.")
    if len(key) > 4095:
        raise ValueError("The entry exceeds the local size limit. Copy only the API key value.")
    if key[0] in "\"'`" or key[-1] in "\"'`":
        raise ValueError("Remove surrounding quotation marks; paste only the API key value.")
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise ValueError("The entry contains whitespace or non-printing characters. Copy only the API key value.")
    return key


def load_key(root=ROOT):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return check_key(key)
    path = key_path(root)
    if not path.exists():
        return None
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid():
        raise ValueError("Local credential must be an owner-only regular file")
    if metadata.st_size > 4096:
        raise ValueError("Local credential file exceeds expected size")
    return check_key(path.read_text(encoding="utf-8").strip())


def store_key(key, root=ROOT):
    check_key(key)
    path = key_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".key-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    print("Use a Gemini key from a Free-tier AI Studio project with billing disabled.")
    print("Enter it here in the terminal; input is hidden. Do not paste it into chat.")
    try:
        with warnings.catch_warnings():
            # Fail rather than fall back to visibly echoed input in a non-TTY.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("Gemini API key: ").strip()
    except getpass.GetPassWarning:
        print("Key was not saved: hidden input is unavailable. Run this command in an interactive terminal.")
        return 1
    except EOFError:
        print("Key was not saved: input ended before a key was entered. Run the command again.")
        return 1
    except KeyboardInterrupt:
        print("\nKey was not saved: entry was cancelled.")
        return 1
    except OSError:
        print("Key was not saved: the terminal could not read hidden input.")
        return 1
    try:
        check_key(key)
    except ValueError as exc:
        # Only our fixed diagnostics, never the supplied secret.
        print("Key was not saved: " + str(exc))
        return 1
    try:
        store_key(key)
    except OSError as exc:
        # Do not print an exception body: filenames/OS messages need not be public.
        print("Key was not saved: could not write the private credential file (OS error {}).".format(exc.errno))
        return 1
    print("Key saved privately for this local collector. It will not be committed or published.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
