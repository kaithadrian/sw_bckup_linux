#!/usr/bin/env python3
"""
switch_backup.py - back up Cisco 2960, Cisco SF350 and HP ProCurve switch configs
over telnet, using telnetlib3 (asyncio).

Replaces the old telnetlib script (telnetlib was removed from Python 3.13). Switches
are backed up concurrently, passwords stay out of the code, and every failure is
reported with a reason.

NOTE: telnet is unencrypted. Usernames and passwords cross the network in clear
text, so only run this on a trusted management network.

Install:   pip install -r requirements.txt
Logins:    copy .env.example to .env and fill in your credentials (CRED_<id>_USER and
           CRED_<id>_PASSWORD); a missing password is asked for when you run the script
Run:       python switch_backup.py                       # back up every switch
           python switch_backup.py --only MDF1 V1        # just these (by name or IP)
           python switch_backup.py --list                # show what would be backed up
           python switch_backup.py --method tftp --tftp-server 192.168.36.87 ^
                                   --tftp-root C:\\backup\\cisco

Linux / Windows
  The same script runs on both (Python 3.9+). On Linux, give --tftp-root a normal path:
    - script runs on the TFTP server itself:   --tftp-root /srv/tftp
    - TFTP server is another machine:          mount its Samba share first (see deploy/README
      in the reply), then  --tftp-root /mnt/tftp --require-mount
  UNC paths (\\\\server\\share) are Windows-only. Under cron/systemd nothing can answer a
  password prompt, so put the credentials in .env. Backups are written owner-only (umask 077),
  and a second run is refused while one is already in progress.

Backup methods
  local (default)  log in, run "show running-config", save the output on this PC.
                   No TFTP server needed, and the saved file is the proof it worked.
  tftp             the switch copies its config to a TFTP server (as the old script did).
                   With --tftp-root (the TFTP folder as seen from this PC, e.g. an SMB
                   share such as \\\\server\\tftp) the script then, for each switch:
                     1. makes sure a <MMDDYYYY> folder exists there (e.g. 10122026),
                     2. moves the received file into it,
                     3. verifies the file is in that folder with the right size.

Inventory (inventory.csv): name,host,model,credentials,port
  model        c2960 | sf350 | hp
  credentials  credential id(s) to try for this switch, in order, e.g. "3" or "1 2"
               (blank = try every credential). A rejected login moves on to the next one.
  port         optional, default 23

Credentials: the id in CRED_<id>_USER / CRED_<id>_PASSWORD. The one that worked last time is
remembered (credential_cache.json, ids only, no passwords) and tried first next run.
--try-all ignores the inventory column and tries every credential on every switch.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from getpass import getpass
from pathlib import Path

import telnetlib3
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
RUN_CONFIG_CMD = "show running-config"
FOLDER_FMT = "%m%d%Y"       # dated folder name, same as the old script
FILE_DATE_FMT = "%d%m%y"    # date inside file names, same as the old script
DEFAULT_TFTP_SERVER = "192.168.36.87"


@dataclass(frozen=True)
class Profile:
    paging: str          # command that turns off "--More--" paging
    tftp: str            # command that pushes the running-config to TFTP
    needs_enable: bool   # old script ran "enable" on the 2960s


PROFILES = {
    "c2960": Profile("terminal length 0", "copy running-config tftp://{server}/{file}", True),
    "sf350": Profile("terminal datadump", "copy running-config tftp://{server}/{file}", False),
    "hp": Profile("no page", "copy running-config tftp {server} {file}", False),
}

# Everything below is matched against the last (unfinished) line of the switch output.
USER_RE = re.compile(r"(user ?name|login|user)\s*:\s*$", re.I)
PASS_RE = re.compile(r"pass(word|code)?\s*:\s*$", re.I)
ANYKEY_RE = re.compile(r"press any key|any key to continue", re.I)
MORE_RE = re.compile(r"--\s*more\s*--[^\n]*$", re.I)
QUESTION_RE = re.compile(r"(\?|\[confirm\]|\[y/n\]\??)\s*$", re.I)
GENERIC_PROMPT_RE = re.compile(r"^\w[^\r\n#>]{0,60}[#>] ?$")  # "sw1#", "HP Switch 2530>"

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ERASE_RE = re.compile(r"\x08+ +\x08+")  # how switches erase "--More--" after a keypress

TFTP_ERROR_RE = re.compile(
    r"%\s*error|error opening|timed out|timeout|unable to|invalid input|"
    r"permission denied|no such file|not found|failed",
    re.I,
)
CLI_ERROR_RE = re.compile(r"^\s*(%|invalid input|unknown command|error)", re.I)

console = Console()
log = logging.getLogger("switch_backup")


class SwitchError(Exception):
    """Something went wrong talking to one switch."""


class LoginRejected(SwitchError):
    """The switch refused the username/password (never retried)."""


@dataclass
class Credential:
    id: str
    username: str
    password: str


@dataclass
class Switch:
    name: str
    host: str
    model: str
    credentials: list[str]   # credential ids to try, in order (empty = try all)
    port: int = 23


@dataclass
class Result:
    switch: Switch
    ok: bool = False
    credential: str = ""
    filename: str = ""
    seconds: float = 0.0
    message: str = ""


# ---------------------------------------------------------------------------
# Inventory and credentials
# ---------------------------------------------------------------------------
def load_inventory(path: str) -> list[Switch]:
    switches, seen = [], set()
    try:
        f = open(path, newline="", encoding="utf-8-sig")
    except OSError as e:
        sys.exit(f"Cannot read inventory {path}: {e}")
    with f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            name = (row.get("name") or "").strip()
            if not name or name.startswith("#"):
                continue
            model = (row.get("model") or "").strip().lower()
            if model not in PROFILES:
                sys.exit(f"{path} line {line_no}: unknown model '{model}' "
                         f"(use one of: {', '.join(PROFILES)})")
            try:
                port = int((row.get("port") or "").strip() or 23)
            except ValueError:
                sys.exit(f"{path} line {line_no}: port must be a number")
            sw = Switch(
                name=name,
                host=(row.get("host") or "").strip(),
                model=model,
                credentials=re.findall(r"\w+", row.get("credentials") or ""),
                port=port,
            )
            if not sw.host:
                sys.exit(f"{path} line {line_no}: host is required")
            if name in seen:
                sys.exit(f"{path} line {line_no}: duplicate name '{name}' "
                         "(a repeated name would silently drop a switch)")
            seen.add(name)
            switches.append(sw)
    return switches


def defined_credential_ids() -> list[str]:
    ids = {m.group(1) for k in os.environ if (m := re.fullmatch(r"CRED_(\w+)_USER", k))}
    return sorted(ids, key=lambda i: (len(i), i))


def load_credentials(switches: list[Switch], try_all: bool) -> dict[str, Credential]:
    """
    Credentials come from CRED_<id>_USER and CRED_<id>_PASSWORD in the environment/.env.
    Only the ones a selected switch might use are needed; a missing password is prompted for.
    """
    defined = defined_credential_ids()
    if not defined:
        sys.exit("No credentials found. Add CRED_<id>_USER and CRED_<id>_PASSWORD lines "
                 "to .env (see .env.example).")
    if try_all or any(not s.credentials for s in switches):
        needed = set(defined)
    else:
        needed = {c for s in switches for c in s.credentials}
    unknown = needed - set(defined)
    if unknown:
        sys.exit(f"Inventory uses credential(s) {', '.join(sorted(unknown))} "
                 f"but .env only defines: {', '.join(defined)}")
    creds = {}
    for cid in sorted(needed, key=lambda i: (len(i), i)):
        user = os.environ[f"CRED_{cid}_USER"].strip()
        password = os.environ.get(f"CRED_{cid}_PASSWORD")
        if not password:
            if not sys.stdin.isatty():
                sys.exit(f"CRED_{cid}_PASSWORD is not set and there is no terminal to ask on "
                         "(running from cron or systemd?). Put it in .env.")
            password = getpass(f"Password for credential {cid} ({user}, CRED_{cid}_PASSWORD): ")
        creds[cid] = Credential(cid, user, password)
    return creds


def credential_order(sw: Switch, creds: dict[str, Credential], cache: dict[str, str],
                     try_all: bool) -> list[Credential]:
    """Which credentials to try for this switch, best guess first."""
    ids = list(creds) if (try_all or not sw.credentials) else list(dict.fromkeys(sw.credentials))
    cached = cache.get(sw.name)
    if cached in ids:
        ids.remove(cached)
        ids.insert(0, cached)
    return [creds[i] for i in ids]


def load_cache(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_cache(path: Path, cache: dict[str, str]) -> None:
    try:
        path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as e:
        log.warning("could not save credential cache: %s", e)


# ---------------------------------------------------------------------------
# Telnet session helper (an "expect"-style layer on top of telnetlib3)
# ---------------------------------------------------------------------------
def clean(text: str) -> str:
    """Remove terminal escape codes, backspace tricks and carriage returns."""
    text = ANSI_RE.sub("", text)
    text = ERASE_RE.sub("", text)
    return text.replace("\x08", "").replace("\x00", "").replace("\r", "")


class Session:
    def __init__(self, reader, writer, timeout: float, transcript: list[str]):
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.transcript = transcript      # cleaned text received, kept for troubleshooting
        self.buf = ""                     # received but not yet consumed
        self.prompt_re = GENERIC_PROMPT_RE

    async def send(self, text: str = "") -> None:
        self.writer.write(text + "\r\n")
        await self.writer.drain()

    async def read_chunk(self, timeout: float) -> bool:
        """Read one chunk into the buffer. False on timeout; raises if the switch hangs up."""
        try:
            data = await asyncio.wait_for(self.reader.read(4096), timeout)
        except asyncio.TimeoutError:
            return False
        if not data:
            raise SwitchError("connection closed by the switch")
        self.buf += data
        return True

    async def expect(self, patterns, timeout: float | None = None, settle: float = 0.2):
        """
        Wait until the last line of output matches one of the (name, regex) patterns.
        Returns (name, text) and consumes the buffer. If more output arrives within
        `settle` seconds the match is re-checked, so a half-printed line can't fool us.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout or self.timeout)
        while True:
            if loop.time() > deadline:
                raise self._timeout_error(patterns)
            last_line = clean(self.buf[-2000:]).rsplit("\n", 1)[-1]
            matched = next((name for name, rx in patterns if rx.search(last_line)), None)
            if matched:
                if await self.read_chunk(settle):
                    continue  # more output arrived: look again
                text = clean(self.buf)
                self.buf = ""
                self.transcript.append(text)
                return matched, text
            if not await self.read_chunk(max(0.05, deadline - loop.time())):
                raise self._timeout_error(patterns)

    def _timeout_error(self, patterns) -> SwitchError:
        waiting_for = "/".join(name for name, _ in patterns)
        tail = " ".join(clean(self.buf)[-120:].split())
        return SwitchError(f"timed out waiting for {waiting_for}; last output: '{tail}'")

    async def run(self, command: str, timeout: float | None = None, answer_questions: bool = False) -> str:
        """Run a command and return its output (paging handled, echo and prompt removed)."""
        await self.send(command)
        patterns = [("more", MORE_RE), ("prompt", self.prompt_re)]
        if answer_questions:
            patterns.append(("question", QUESTION_RE))
        parts: list[str] = []
        while True:
            name, text = await self.expect(patterns, timeout)
            if name == "more":
                parts.append(re.sub(MORE_RE, "", text))
                self.writer.write(" ")           # next page
                await self.writer.drain()
            elif name == "question":
                parts.append(text)
                await self.send("")              # accept the default answer
            else:
                parts.append(text)
                break
        lines = "".join(parts).split("\n")
        if lines and command.strip() in lines[0]:
            lines = lines[1:]                    # the echoed command
        if lines and self.prompt_re.search(lines[-1]):
            lines = lines[:-1]                   # the prompt that came back
        return "\n".join(line.rstrip() for line in lines).strip("\n")


async def login(s: Session, cred: Credential) -> str:
    """Answer whatever login prompts the switch shows. Returns the first CLI prompt line."""
    patterns = [("user", USER_RE), ("pass", PASS_RE), ("anykey", ANYKEY_RE), ("prompt", GENERIC_PROMPT_RE)]
    sent_user = sent_pass = False
    while True:
        try:
            name, text = await s.expect(patterns)
        except SwitchError as e:
            if sent_pass and "closed" in str(e):
                raise LoginRejected("switch closed the connection after the password was sent") from e
            raise
        if name == "user":
            if sent_user:
                raise LoginRejected("login rejected (username prompt shown again)")
            await s.send(cred.username)
            sent_user = True
        elif name == "pass":
            if sent_pass:
                raise LoginRejected("login rejected (password prompt shown again)")
            await s.send(cred.password)
            sent_pass = True
        elif name == "anykey":
            s.writer.write(" ")                  # HP "Press any key to continue"
            await s.writer.drain()
        else:
            return text.rsplit("\n", 1)[-1].strip()


async def enable_mode(s: Session, password: str) -> None:
    """Enter privileged mode (the old script reused the login password as the enable password)."""
    await s.send("enable")
    name, _ = await s.expect([("pass", PASS_RE), ("prompt", s.prompt_re)])
    if name == "pass":
        await s.send(password)
        name, _ = await s.expect([("pass", PASS_RE), ("prompt", s.prompt_re)])
        if name == "pass":
            raise SwitchError("enable password rejected")


async def logout(s: Session) -> None:
    """Log out politely so the switch doesn't keep the telnet line busy."""
    try:
        await s.send("exit")
        await s.read_chunk(0.7)
        if re.search(r"\[y/n\]", clean(s.buf), re.I):   # HP asks "Do you want to log out [y/n]?"
            await s.send("y")
            await s.read_chunk(0.3)
    except Exception:  # noqa: BLE001 - best effort, the connection may already be gone
        pass


# ---------------------------------------------------------------------------
# Backup of one switch
# ---------------------------------------------------------------------------
def safe_name(text: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", text).strip("_") or "switch"


async def wait_for_file(path: Path, timeout: float) -> bool:
    """Wait for the file to appear and stop growing (the TFTP server may still be writing)."""
    end = time.time() + timeout
    last_size = -1
    while time.time() < end:
        size = path.stat().st_size if path.exists() else 0
        if size > 0 and size == last_size:
            return True
        last_size = size
        await asyncio.sleep(1)
    return False


def prepare_tftp_folder(root: Path, folder_name: str) -> Path:
    """Step 1: make sure <root>/<date> exists on the TFTP share (safe if it already does)."""
    if not root.is_dir():
        raise OSError(f"{root} is not an accessible folder")
    folder = root / folder_name
    created = not folder.exists()
    folder.mkdir(exist_ok=True)
    if created and os.name == "posix":
        try:  # give it the same access as the TFTP folder itself, so other users can still read it
            os.chmod(folder, root.stat().st_mode & 0o7777)
        except OSError:
            pass
    return folder


def file_into_dated_folder(root: Path, folder_name: str, filename: str) -> str:
    """
    On the TFTP share: create <root>/<date> if needed (step 1), move the received file
    into it (step 2) and verify the result (step 3). On any problem the file is left
    where it was and a SwitchError says what failed.
    """
    src = root / filename
    folder = root / folder_name
    dest = folder / filename
    try:
        prepare_tftp_folder(root, folder_name)
        size = src.stat().st_size
        try:
            os.replace(src, dest)                   # a rename inside the share
        except OSError:
            # e.g. the folder is a different filesystem: copy under a temp name, then swap in
            tmp = dest.with_name(dest.name + ".part")
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            src.unlink()
    except OSError as e:
        raise SwitchError(f"could not move {filename} into {folder} ({e.strerror or e}); "
                          f"it is still in {root}") from None

    if not dest.exists() or dest.stat().st_size != size:
        raise SwitchError(f"verification failed: {dest} is missing or the wrong size")
    if src.exists():
        raise SwitchError(f"verification failed: {filename} is still in {root}")
    return f"moved to {folder_name} and verified ({size} bytes)"


async def backup_once(sw: Switch, cred: Credential, args, outdir: Path, stamp: str,
                      transcript: list[str]) -> tuple[str, str]:
    """One full attempt on one switch. Returns (filename, message)."""
    profile = PROFILES[sw.model]
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                sw.host, sw.port, term="vt100", cols=200, rows=24,
                connect_minwait=0.0, connect_maxwait=1.0,
            ),
            timeout=args.timeout,
        )
    except asyncio.TimeoutError:
        raise SwitchError("could not connect (timed out)") from None
    except OSError as e:
        raise SwitchError(f"could not connect: {e.strerror or e}") from None

    s = Session(reader, writer, args.timeout, transcript)
    try:
        prompt_line = await login(s, cred)
        hostname = re.sub(r"\s*[#>]\s*$", "", prompt_line).strip()
        # From here on only accept this switch's own prompt (also in "(config)" style modes)
        s.prompt_re = re.compile(re.escape(hostname) + r"(\([^)\r\n]*\))?[#>] ?$")

        if profile.needs_enable and prompt_line.rstrip().endswith(">"):
            await enable_mode(s, cred.password)
        await s.run(profile.paging, args.timeout)  # no "--More--" prompts from here on

        filename = f"{safe_name(hostname)}-{sw.host}-{stamp}.config"
        if args.method == "local":
            config = await s.run(RUN_CONFIG_CMD, args.cmd_timeout)
            if len(config.strip()) < 100 or CLI_ERROR_RE.match(config):
                raise SwitchError("running-config looked empty or invalid: "
                                  + " ".join(config.split())[:100])
            (outdir / filename).write_text(config + "\n", encoding="utf-8")
            return filename, f"{len(config.splitlines())} lines"

        out = await s.run(profile.tftp.format(server=args.tftp_server, file=filename),
                          args.cmd_timeout, answer_questions=True)
        if TFTP_ERROR_RE.search(out):
            raise SwitchError("TFTP copy reported an error: " + " ".join(out.split())[-120:])
    finally:
        if s.buf:
            transcript.append(clean(s.buf))
        await logout(s)
        writer.close()

    # TFTP method: wait for the file, then file it into the dated folder and verify it
    if args.tftp_root:
        root = Path(args.tftp_root)
        if not await wait_for_file(root / filename, args.tftp_wait):
            raise SwitchError(f"switch accepted the command but {filename} never appeared in {args.tftp_root}")
        # the share may be slow: keep the file work off the event loop
        message = await asyncio.to_thread(file_into_dated_folder, root, outdir.name, filename)
        return filename, message
    return filename, "sent to TFTP server (not verified; use --tftp-root to verify)"


async def backup_switch(sw: Switch, order: list[Credential], args, outdir: Path, stamp: str,
                        sem: asyncio.Semaphore) -> Result:
    async with sem:
        start = time.time()
        result = Result(switch=sw)
        errors: list[str] = []
        transcript: list[str] = []

        for attempt in range(args.retries + 1):
            only_login_failures = True
            for cred in order:
                transcript.clear()
                try:
                    result.filename, result.message = await asyncio.wait_for(
                        backup_once(sw, cred, args, outdir, stamp, transcript),
                        timeout=args.switch_timeout,
                    )
                    result.ok, result.credential = True, cred.id
                    break
                except LoginRejected as e:
                    errors.append(f"credential {cred.id}: {e}")
                    continue  # wrong login for this switch: try the next credential
                except SwitchError as e:
                    errors.append(str(e))
                except asyncio.TimeoutError:
                    errors.append(f"gave up after {args.switch_timeout}s")
                except Exception as e:  # noqa: BLE001 - report anything else per switch
                    errors.append(f"{type(e).__name__}: {' '.join(str(e).split())[:140]}")
                only_login_failures = False
                break  # a network/CLI problem: other credentials won't help
            if result.ok or only_login_failures:
                break  # done, or every credential was refused (retrying only risks lockouts)
            if attempt < args.retries:
                await asyncio.sleep(2)

        if not result.ok:
            result.message = "; ".join(dict.fromkeys(errors))  # unique, in order

        if transcript and (args.transcripts or not result.ok):
            tdir = outdir / ("transcripts" if args.transcripts else "failed_transcripts")
            tdir.mkdir(exist_ok=True)
            (tdir / f"{safe_name(sw.name)}.txt").write_text("".join(transcript), encoding="utf-8")

        result.seconds = time.time() - start
        return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
@contextmanager
def single_instance(lock_path: Path):
    """Refuse to run twice at once (e.g. a slow cron run overlapping the next one). Linux/Unix only."""
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit(f"Another switch_backup run is already in progress (lock: {lock_path}).")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def warn_if_env_is_open(paths) -> None:
    """The .env file holds passwords: warn if other users on this machine can read it."""
    if os.name != "posix":
        return
    for path in dict.fromkeys(paths):
        try:
            if path.is_file() and path.stat().st_mode & 0o077:
                console.print(f"[yellow]Warning:[/] {path} can be read by other users. Run: chmod 600 {path}")
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Back up switch running-configs over telnet (telnetlib3).")
    p.add_argument("-i", "--inventory", default="inventory.csv", help="inventory CSV (default: inventory.csv)")
    p.add_argument("-o", "--output", default="backups", help="base output folder (default: ./backups)")
    p.add_argument("-n", "--only", nargs="+", metavar="NAME_OR_IP", help="only back up these switches")
    p.add_argument("--list", action="store_true", help="show the switches that would be backed up, then exit")
    p.add_argument("-m", "--method", choices=["local", "tftp"], default="local", help="backup method (default: local)")
    p.add_argument("--tftp-server", default=os.environ.get("TFTP_SERVER", DEFAULT_TFTP_SERVER))
    p.add_argument("--tftp-root", help="the TFTP server's folder as seen from this PC (e.g. an SMB share); "
                                       "each file is moved into a <MMDDYYYY> subfolder there and verified")
    p.add_argument("--tftp-wait", type=int, default=20, help="seconds to wait for each TFTP file (default: 20)")
    p.add_argument("--require-mount", action="store_true",
                   help="refuse to run unless --tftp-root is a mounted filesystem "
                        "(Linux: guards against writing into an empty mount point if the share isn't mounted)")
    p.add_argument("-w", "--workers", type=int, default=10, help="switches to back up in parallel (default: 10)")
    p.add_argument("--timeout", type=int, default=20, help="seconds to wait for a connection or prompt (default: 20)")
    p.add_argument("--cmd-timeout", type=int, default=120, help="seconds to wait for a command to finish (default: 120)")
    p.add_argument("--switch-timeout", type=int, default=240, help="overall limit per switch attempt (default: 240)")
    p.add_argument("--retries", type=int, default=1, help="extra attempts after a timeout (default: 1)")
    p.add_argument("--try-all", action="store_true",
                   help="ignore the inventory's credentials column and try every credential on every switch")
    p.add_argument("--transcripts", action="store_true",
                   help="save what each switch sent (failures are always saved)")
    return p.parse_args()


async def run_all(switches, creds, cache, args, outdir, stamp) -> list[Result]:
    sem = asyncio.Semaphore(max(1, args.workers))
    tasks = [asyncio.create_task(backup_switch(s, credential_order(s, creds, cache, args.try_all),
                                               args, outdir, stamp, sem))
             for s in switches]
    results: list[Result] = []
    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
    )
    with progress:
        bar = progress.add_task("Switches", total=len(switches))
        for finished in asyncio.as_completed(tasks):
            r = await finished
            results.append(r)
            if r.ok:
                progress.console.print(f"[green]OK[/]     {r.switch.name:<18} {r.switch.host:<16} "
                                       f"cred {r.credential:<3} {r.seconds:4.1f}s  {r.filename}")
                log.info("OK %s %s cred %s -> %s (%s)", r.switch.name, r.switch.host, r.credential,
                         r.filename, r.message)
            else:
                progress.console.print(f"[red]FAILED[/] {r.switch.name:<18} {r.switch.host:<16} {r.message}")
                log.error("FAILED %s %s: %s", r.switch.name, r.switch.host, r.message)
            progress.advance(bar)
    return results


def main() -> int:
    # Read .env from the current folder, then from the folder this script lives in
    env_files = [Path(find_dotenv(usecwd=True) or "."), Path(__file__).resolve().parent / ".env"]
    for env_file in env_files:
        load_dotenv(env_file)
    if os.name == "posix":
        os.umask(0o077)  # backups hold secrets (SNMP communities, password hashes): owner-only
    args = parse_args()

    switches = load_inventory(args.inventory)
    if args.only:
        wanted = {w.lower() for w in args.only}
        switches = [s for s in switches if s.name.lower() in wanted or s.host in wanted]
        missing = wanted - {s.name.lower() for s in switches} - {s.host for s in switches}
        if missing:
            sys.exit(f"Not in inventory: {', '.join(sorted(missing))}")
    if not switches:
        sys.exit("No switches to back up.")

    if args.list:
        table = Table(title=f"{len(switches)} switches")
        for col in ("Name", "Host", "Port", "Model", "Credentials"):
            table.add_column(col)
        for s in switches:
            table.add_row(s.name, s.host, str(s.port), s.model, " ".join(s.credentials) or "all")
        console.print(table)
        defined = defined_credential_ids()
        console.print("Credentials defined in .env: "
                      + (", ".join(f"{i}={os.environ[f'CRED_{i}_USER']}" for i in defined) or "none"))
        return 0

    warn_if_env_is_open(env_files)
    Path(args.output).mkdir(parents=True, exist_ok=True)
    with single_instance(Path(args.output) / ".switch_backup.lock"):
        return run_backup(args, switches)


def run_backup(args, switches: list[Switch]) -> int:
    now = datetime.now()
    outdir = Path(args.output) / now.strftime(FOLDER_FMT)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(FILE_DATE_FMT)

    if args.method == "tftp" and args.tftp_root:
        if args.require_mount and not os.path.ismount(args.tftp_root):
            sys.exit(f"--require-mount: {args.tftp_root} is not a mounted filesystem. "
                     "Is the share mounted? (try: mount -a)")
        try:
            folder = prepare_tftp_folder(Path(args.tftp_root), outdir.name)
        except OSError as e:
            sys.exit(f"Cannot use --tftp-root: {e.strerror or e}. Check the path and that you can "
                     f"create folders in it.")
        console.print(f"Received files will be moved into [bold]{folder}[/]")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(outdir / "backup.log", encoding="utf-8")],
    )

    creds = load_credentials(switches, args.try_all)
    cache_path = Path(args.output) / "credential_cache.json"
    cache = load_cache(cache_path)
    console.print(f"Backing up [bold]{len(switches)}[/] switches to [bold]{outdir}[/] "
                  f"({args.method}, {args.workers} at a time)")

    results = asyncio.run(run_all(switches, creds, cache, args, outdir, stamp))

    # Remember which credential worked for each switch (ids only, never passwords)
    updated = {**cache, **{r.switch.name: r.credential for r in results if r.ok}}
    if updated != cache:
        save_cache(cache_path, updated)

    order = {s.name: i for i, s in enumerate(switches)}
    results.sort(key=lambda r: order[r.switch.name])
    failed = [r for r in results if not r.ok]
    with open(outdir / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "host", "model", "ok", "credential", "file", "seconds", "message"])
        for r in results:
            w.writerow([r.switch.name, r.switch.host, r.switch.model, r.ok, r.credential,
                        r.filename, f"{r.seconds:.1f}", r.message])

    console.print(f"\n[bold]Total:[/] {len(results)}   [green]OK:[/] {len(results) - len(failed)}   "
                  f"[red]Failed:[/] {len(failed)}   (details: {outdir / 'summary.csv'})")
    if failed:
        table = Table(title="Failed switches")
        for col in ("Name", "Host", "Reason"):
            table.add_column(col)
        for r in failed:
            table.add_row(r.switch.name, r.switch.host, r.message)
        console.print(table)
        console.print(f"What each failed switch sent is in {outdir / 'failed_transcripts'}")
        console.print("Retry just these with:  python switch_backup.py --only "
                      + " ".join(r.switch.name for r in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
