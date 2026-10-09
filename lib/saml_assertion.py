# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read, validate and protect the Domain Join inputs: SAML assertion and stack ARN.

A SAML assertion is a bearer credential for a short window, so this module keeps it out of
places it should not be: it is read from a file (or an environment variable), never the command line,
tries not to echo it in an error message, and scrubs it from log output.

Imports nothing third-party so it can be used - and tested - before any heavy dependency.
"""

import codecs
import logging
import os
import re
import shlex
import stat
import sys
import traceback
from urllib.parse import quote

ENV_SAML_ASSERTION = "AGENTACCESS_SAML_ASSERTION"

# arn:aws:appstream:<region>:<12-digit account>:stack/<name> (any partition)
_STACK_ARN = re.compile(r"arn:aws[a-z-]*:appstream:[a-z0-9-]+:\d{12}:stack/[A-Za-z0-9][A-Za-z0-9_.-]*")
# Standard or URL-safe base64, which is what an IdP hands back for a SAML Response.
_BASE64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")

_REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 16        # don't scrub short strings that could match ordinary log text
_MIN_ARGV_SECRET_LENGTH = 40   # a command-line word this long and made only of base64 characters
_MIN_ASSERTION_LENGTH = 32     # a real SAML Response is kilobytes; anything shorter is a mistake


def normalize_assertion(text):
    """Remove every whitespace character: ``base64`` wraps lines, editors add newlines."""
    return "".join(text.split())


def is_plausible_assertion(text):
    """True if ``text`` looks like a base64 SAML Response (not XML, a URL or an ARN)."""
    return bool(_BASE64.fullmatch(text))


def is_valid_stack_arn(text):
    return bool(_STACK_ARN.fullmatch(text))


def _shown(value):
    """``value`` for an error message, unless it looks like the assertion itself."""
    text = str(value)
    if len(text) >= _MIN_ARGV_SECRET_LENGTH and is_plausible_assertion(normalize_assertion(text)):
        return "<a value that looks like the SAML assertion, not shown>"
    return repr(text)


# --- warnings that must survive the banner's `clear` -----------------------------------------

_warnings = []


def warn(message):
    """Print a warning to stderr now and remember it, since the banner clears the screen."""
    _warnings.append(message)
    sys.stderr.write(f"warning: {message}\n")


def pending_warnings():
    return list(_warnings)


# --- resolving the CLI inputs ----------------------------------------------------------------

def _decode(data):
    """Decode a file's bytes, allowing the BOMs Windows editors and PowerShell write."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _read_assertion_file(parser, path):
    path = os.path.expanduser(path)
    try:
        info = os.stat(path)
        with open(path, "rb") as fh:
            data = fh.read()
        text = _decode(data)
    except UnicodeDecodeError:
        parser.error(f"--saml-assertion-file {_shown(path)} is not text; save it as plain ASCII/UTF-8.")
    except OSError as exc:
        parser.error(f"could not read --saml-assertion-file {_shown(path)}: {exc.strerror or type(exc).__name__}")
    if os.name != "nt" and stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warn(f"{path} can be read by other users; restrict it with: chmod 600 {shlex.quote(path)}")
    return text


def resolve_saml_args(parser, args):
    """Validate and normalise the Domain Join options on ``args`` (``parser.error`` on misuse).

    Returns ``True`` when Domain Join was selected, having stored the normalised assertion
    in ``args.saml_assertion`` whichever way it was supplied (``--saml-assertion-file`` or
    ``$AGENTACCESS_SAML_ASSERTION``), and ``False`` when the
    run uses a streaming URL. Error messages avoid echoing anything that looks like the
    assertion.
    """
    path = getattr(args, "saml_assertion_file", None)
    streaming_url = (getattr(args, "streaming_url", None) or "").strip()
    stack_arn = getattr(args, "stack_arn", None)

    if getattr(args, "removed_inline_assertion", None) is not None:
        parser.error("--saml-assertion has been removed: an assertion on the command line is visible in "
                     "the process list. Save it to a file and pass --saml-assertion-file "
                     f"(or set ${ENV_SAML_ASSERTION}).")

    if path is not None:
        source, assertion = "--saml-assertion-file", _read_assertion_file(parser, path)
    elif not streaming_url and os.environ.get(ENV_SAML_ASSERTION):
        # the environment is only a fallback when no streaming URL was given
        source, assertion = f"${ENV_SAML_ASSERTION}", os.environ[ENV_SAML_ASSERTION]
    else:
        source, assertion = None, None

    if assertion is None:
        if stack_arn:
            parser.error("--stack-arn is only used with --saml-assertion-file.")
        return False

    if streaming_url:
        parser.error(f"--streaming-url and {source} are mutually exclusive.")
    assertion = normalize_assertion(assertion)
    if not assertion:
        parser.error(f"the SAML assertion from {source} is empty.")
    if os.path.exists(assertion):
        parser.error(f"the value of {source} is a file path; pass that path with --saml-assertion-file.")
    if len(assertion) < _MIN_ASSERTION_LENGTH or not is_plausible_assertion(assertion):
        parser.error(
            f"the SAML assertion from {source} is not base64 (or is far too short). Pass the base64 "
            "SAML Response exactly as the identity provider issued it, not the XML."
        )
    if not stack_arn:
        hint = f" Unset ${ENV_SAML_ASSERTION} or pass --streaming-url to use a streaming URL." \
            if source.startswith("$") else ""
        parser.error(f"--stack-arn is required with {source}.{hint}")
    if not is_valid_stack_arn(stack_arn):
        parser.error(
            f"--stack-arn {_shown(stack_arn)} is not a WorkSpaces Applications stack ARN; "
            "expected arn:aws:appstream:<region>:<account-id>:stack/<stack-name>."
        )

    args.saml_assertion = assertion
    install_log_redaction(assertion)
    return True


def register_argv_secrets(argv):
    """Treat long base64-looking command-line words as secrets from now on.

    argparse echoes the words it does not understand (``unrecognized arguments: ...``,
    ``ambiguous option: --saml-ass=<value>``) before any of our validation runs, and an
    unquoted ``$(cat wrapped.b64)`` splits the assertion into several words. Registering
    them lets :func:`redact` scrub those messages. Existing paths are not secrets.
    """
    for word in argv:
        for candidate in (word, word.partition("=")[2]):
            if (len(candidate) >= _MIN_ARGV_SECRET_LENGTH and is_plausible_assertion(candidate)
                    and not os.path.exists(candidate)):
                install_log_redaction(candidate)


# --- redaction ---------------------------------------------------------------------------------

_secrets = set()
_previous_factory = None


def _spellings(secret):
    """The secret as it would appear if a server echoed it back JSON-escaped or percent-encoded."""
    slash_escaped = secret.replace("/", "\\/")
    plus_escaped = secret.replace("+", "\\u002B")
    both = slash_escaped.replace("+", "\\u002B")
    return {secret, slash_escaped, plus_escaped, both, both.replace("\\u002B", "\\u002b"),
            quote(secret, safe="")}


def install_log_redaction(*secrets):
    """Replace ``secrets`` with ``[REDACTED]`` in every log record from now on.

    Hooks the log-record factory, so it covers every logger and handler (``mcp``,
    ``mcp_proxy_for_aws``, ``httpx``, ...), including ones configured later. The message,
    its arguments and any exception traceback are scrubbed, and so are the common
    re-encodings of a secret (JSON ``\\/`` and ``\\u002B`` escapes, percent-encoding).
    """
    global _previous_factory
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            _secrets.update(_spellings(secret))
    if _previous_factory is not None:
        return
    _previous_factory = logging.getLogRecordFactory()

    def factory(*factory_args, **factory_kwargs):
        record = _previous_factory(*factory_args, **factory_kwargs)
        _scrub(record)
        return record

    logging.setLogRecordFactory(factory)


def redact(text):
    """``text`` with every registered secret replaced (longest spelling first)."""
    if not _secrets:
        return text
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    return text


def _scrub(record):
    try:
        message = record.getMessage()
    except Exception:  # malformed format string / args: leave it for logging to report
        return
    cleaned = redact(message)
    if cleaned != message:
        record.msg, record.args = cleaned, ()
    if record.exc_info and not record.exc_text:
        text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        if redact(text) != text:
            record.exc_text = redact(text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
