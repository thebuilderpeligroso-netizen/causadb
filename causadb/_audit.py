"""`causadb._audit` — F.12.6: measure AI-authored code survival in git history.

Standalone module (no `.causadb/` workspace, no ledger, no runtime) that reads
a git repository via the `git` CLI (`subprocess.run`) and reports how much
code introduced by AI agents survives at HEAD today.

Algorithm (from Causari `audit.rs`, reimplemented in Python — Article VIII
allows this because there are >= 2 concrete consumers: the CLI handler and
the test suite):

  0. Pre-check `git --version` → `AuditError` if git is missing.
  1. `git log --reverse --no-merges --pretty=format:'%H%x1f%an%x1f%ae%x1f%B'`
     → commits oldest-first.
  2. `detect_ai(commit)` → scan message for trailers
     (`Co-Authored-By: Claude`, `Copilot`, `Cursor`, `Aider`) and author
     email/name markers. Evidence class: VERIFIED (explicit trailer),
     PROBABLE (weak marker like `(aider)` in headline), UNKNOWN (not AI).
  3. For AI commits: `git show --numstat` → lines introduced per file.
  4. `git blame --line-porcelain HEAD -- <file>` per file → surviving lines
     = count(blame owners == this commit).
  5. Survival = surviving / introduced, clamped to [0.0, 1.0].
  6. `aggregate(commits, repo_dir)` → per-agent rollup.

Fall-Closed: any git failure raises `AuditError` (no best-effort, no silent
skip — Article V).
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class Evidence(Enum):
    """Strength of the AI-authorship signal for a commit."""
    VERIFIED = "verified"      # explicit Co-Authored-By trailer
    PROBABLE = "probable"      # weak marker (e.g. `(aider)` in headline)
    UNKNOWN = "unknown"        # not AI


@dataclass
class Commit:
    """A single git commit relevant to the audit."""
    sha: str
    author_name: str
    author_email: str
    message: str
    introduced: int = 0          # total lines added across files
    surviving: int = 0           # lines still present at HEAD
    files: List[str] = field(default_factory=list)  # files touched


class AuditError(Exception):
    """Fall-Closed error for the audit subsystem (git missing, git failure)."""


@dataclass
class AuditReport:
    """Top-level report returned by `AuditReport.build(repo_dir)`."""
    repo_dir: str
    total_commits: int
    ai_commits: int
    agents: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------
    @classmethod
    def build(cls, repo_dir) -> "AuditReport":
        """Read commits, detect AI, compute survival, aggregate per agent."""
        repo = _to_path(repo_dir)
        _check_git()
        commits = read_commits(repo)
        agg = aggregate(commits, repo)
        ai_count = sum(1 for c in commits if detect_ai(c)[0])
        return cls(
            repo_dir=str(repo),
            total_commits=len(commits),
            ai_commits=ai_count,
            agents=agg,
        )

    # ------------------------------------------------------------------
    # Output renderers
    # ------------------------------------------------------------------
    def to_markdown(self) -> str:
        lines = []
        lines.append("# CausaDB AI Audit")
        lines.append("")
        lines.append(f"- **Repo:** `{self.repo_dir}`")
        lines.append(f"- **Total commits:** {self.total_commits}")
        lines.append(f"- **AI commits:** {self.ai_commits}")
        lines.append("")
        lines.append("## Per-agent survival")
        lines.append("")
        if not self.agents:
            lines.append("_No AI-authored commits detected._")
            return "\n".join(lines)
        lines.append("| Agent | Evidence | Commits | Introduced | Surviving | Survival |")
        lines.append("|-------|----------|--------|------------|-----------|----------|")
        for a in self.agents:
            surv_pct = f"{a['survival'] * 100:.1f}%"
            lines.append(
                f"| {a['agent']} | {a['evidence']} | {a['commits']} | "
                f"{a['introduced']} | {a['surviving']} | {surv_pct} |"
            )
        return "\n".join(lines)

    def to_json(self) -> str:
        import json
        payload = {
            "repo_dir": self.repo_dir,
            "total_commits": self.total_commits,
            "ai_commits": self.ai_commits,
            "agents": self.agents,
        }
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def to_terminal(self) -> str:
        lines = []
        lines.append("=== CausaDB AI Audit ===")
        lines.append(f"Repo: {self.repo_dir}")
        lines.append(f"Total commits: {self.total_commits}")
        lines.append(f"AI commits: {self.ai_commits}")
        lines.append("")
        if not self.agents:
            lines.append("No AI-authored commits detected.")
            return "\n".join(lines)
        # Simple fixed-width table.
        header = f"{'Agent':<12} {'Evidence':<10} {'Commits':>7} {'Intro':>7} {'Surv':>7} {'Survival':>9}"
        lines.append(header)
        lines.append("-" * len(header))
        for a in self.agents:
            surv_pct = f"{a['survival'] * 100:.1f}%"
            lines.append(
                f"{a['agent']:<12} {a['evidence']:<10} {a['commits']:>7} "
                f"{a['introduced']:>7} {a['surviving']:>7} {surv_pct:>9}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 0 — pre-check
# ---------------------------------------------------------------------------

def _check_git() -> None:
    """Raise `AuditError` if the `git` CLI is not available."""
    try:
        subprocess.run(
            ["git", "--version"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise AuditError(
            "git CLI is required. Install git and try again."
        ) from e


def _to_path(repo_dir) -> Path:
    return Path(repo_dir).resolve()


# ---------------------------------------------------------------------------
# Step 1 — read commits
# ---------------------------------------------------------------------------

# %x1f is the ASCII unit-separator (0x1F) — safe delimiter that won't appear
# in author names or commit messages.
_LOG_FMT = "%H%x1f%an%x1f%ae%x1f%B"
# %x1e is the ASCII record-separator (0x1E) — separates commits. We append it
# to the format so we can split multi-commit messages reliably (commit
# messages contain newlines).
_LOG_FMT_WITH_SEP = "%H%x1f%an%x1f%ae%x1f%B%x1e"


def read_commits(repo_dir) -> List[Commit]:
    """Return all non-merge commits oldest-first.

    Uses `%x1e` (record separator) between commits and `%x1f` (unit separator)
    between fields, so commit messages containing newlines parse correctly.
    """
    repo = _to_path(repo_dir)
    _check_git()
    proc = subprocess.run(
        ["git", "log", "--reverse", "--no-merges",
         f"--pretty=format:{_LOG_FMT_WITH_SEP}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AuditError(f"git log failed: {proc.stderr.strip() or proc.stdout!r}")
    raw = proc.stdout
    if not raw.strip():
        return []
    # Split on record separator. The last element after the final %x1e is empty.
    records = [r for r in raw.split("\x1e") if r.strip()]
    commits: List[Commit] = []
    for rec in records:
        parts = rec.split("\x1f")
        if len(parts) < 4:
            # Malformed record — skip rather than crash (Fall-Closed only
            # applies to git-level failures, not parser edge cases).
            continue
        sha, author_name, author_email, message = parts[0], parts[1], parts[2], parts[3]
        commits.append(Commit(
            sha=sha.strip(),
            author_name=author_name.strip(),
            author_email=author_email.strip(),
            message=message.strip(),
        ))
    return commits


# ---------------------------------------------------------------------------
# Step 2 — detect AI
# ---------------------------------------------------------------------------

# Agent canonical names. Order matters: the first matching trailer wins,
# so more specific markers (e.g. `claude`) are listed before generic ones.
_AGENTS = {
    "claude": "claude",
    "copilot": "copilot",
    "cursor": "cursor",
    "aider": "aider",
}

# Trailer regex — case-insensitive `Co-Authored-By: <name>`.
_TRAILER_RE = re.compile(
    r"co-authored-by:\s*([^\n<]+?)(?:\s*<[^>]+>)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Weak markers in the headline (not trailers). e.g. `(aider)`.
_HEADLINE_MARKER_RE = re.compile(
    r"\((aider|copilot|cursor|claude)\)",
    re.IGNORECASE,
)

# Author email markers — domains/addresses strongly associated with AI agents.
_EMAIL_MARKERS = {
    "noreply@anthropic.com": "claude",
    "anthropic.com": "claude",
    "github.com": "copilot",  # noreply@github.com Copilot trailer
}

# Author name markers (lowercased substring match).
_NAME_MARKERS = {
    "claude": "claude",
    "copilot": "copilot",
    "cursor": "cursor",
    "aider": "aider",
}


def detect_ai(commit: Commit) -> Tuple[bool, Optional[str], Evidence]:
    """Classify a commit as AI-authored or not.

    Returns `(is_ai, agent_name_or_None, evidence_class)`.

    Detection order (strongest signal first):
      1. `Co-Authored-By: <Agent>` trailer → VERIFIED.
      2. Author email domain match → VERIFIED.
      3. Author name marker → VERIFIED.
      4. Headline `(agent)` marker → PROBABLE.
    """
    # 1. Trailer scan.
    for m in _TRAILER_RE.finditer(commit.message):
        name = m.group(1).strip().lower()
        for marker, canonical in _AGENTS.items():
            if marker in name:
                return True, canonical, Evidence.VERIFIED

    # 2. Author email.
    email = commit.author_email.lower()
    for marker, canonical in _EMAIL_MARKERS.items():
        if marker in email:
            # `github.com` alone is too broad — require the trailer or a
            # Copilot-specific name. Skip bare `@github.com` humans.
            if canonical == "copilot" and "noreply@github.com" not in email:
                continue
            # If the email is `noreply@github.com` we only treat it as
            # Copilot when paired with a Copilot name/trailer — otherwise
            # it's a generic GitHub noreply. Defer to name check.
            if canonical == "copilot" and email == "noreply@github.com":
                continue
            return True, canonical, Evidence.VERIFIED

    # 3. Author name.
    name = commit.author_name.lower()
    for marker, canonical in _NAME_MARKERS.items():
        if marker in name:
            return True, canonical, Evidence.VERIFIED

    # 4. Headline weak marker.
    hm = _HEADLINE_MARKER_RE.search(commit.message)
    if hm:
        return True, hm.group(1).lower(), Evidence.PROBABLE

    return False, None, Evidence.UNKNOWN


# ---------------------------------------------------------------------------
# Step 3 + 4 — introduced lines + surviving lines
# ---------------------------------------------------------------------------

def _numstat_for_commit(sha: str, repo: Path) -> Tuple[int, List[str]]:
    """Return (total_lines_added, list_of_paths) for a commit."""
    proc = subprocess.run(
        ["git", "show", "--no-color", "--numstat", "--format=", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AuditError(f"git show --numstat failed for {sha}: {proc.stderr.strip()}")
    total = 0
    files: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # numstat format: "<added>\t<deleted>\t<path>"
        # Binary files show "-\t-\t<path>".
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_s, _deleted_s, path = parts[0], parts[1], "\t".join(parts[2:])
        if added_s == "-":
            # Binary file — no line count.
            continue
        try:
            added = int(added_s)
        except ValueError:
            continue
        total += added
        files.append(path)
    return total, files


def _blame_surviving(sha: str, file_path: str, repo: Path) -> int:
    """Count lines in `file_path` at HEAD whose blame points to `sha`.

    Uses `git blame --line-porcelain HEAD -- <file>` and counts lines whose
    `commit <sha>` header matches. Returns 0 if the file no longer exists
    at HEAD (fully deleted).
    """
    # First check the file exists at HEAD.
    check = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{file_path}"],
        cwd=str(repo),
        capture_output=True,
    )
    if check.returncode != 0:
        return 0
    proc = subprocess.run(
        ["git", "blame", "--line-porcelain", "HEAD", "--", file_path],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Blame can fail on empty files or unusual paths — treat as 0.
        return 0
    count = 0
    for line in proc.stdout.splitlines():
        # `--line-porcelain` emits a header line per content line of the form:
        #   `<40-hex-sha> <orig-lineno> <final-lineno> [group-size]`
        # We match the SHA at the start of the line. Lines beginning with a
        # TAB are content; lines beginning with a lowercase key (`author`,
        # `summary`, etc.) are metadata — neither starts with a 40-hex SHA.
        if len(line) >= 40 and line[:40] == sha and line[40:41] in (" ", "\t"):
            count += 1
    return count


def compute_survival(commit: Commit, repo_dir) -> float:
    """Compute survival ratio for a commit, clamped to [0.0, 1.0].

    Survival = surviving_lines / introduced_lines.
    Returns 0.0 if the commit introduced no lines (e.g. binary-only).
    """
    repo = _to_path(repo_dir)
    _check_git()
    if commit.introduced == 0 and not commit.files:
        # Lazily populate introduced + files if not already done.
        introduced, files = _numstat_for_commit(commit.sha, repo)
        commit.introduced = introduced
        commit.files = files
    if commit.introduced <= 0:
        return 0.0
    surviving = 0
    for f in commit.files:
        surviving += _blame_surviving(commit.sha, f, repo)
    commit.surviving = surviving
    ratio = surviving / commit.introduced
    # Clamp to 100% — blame can over-count due to context lines or renames.
    if ratio > 1.0:
        ratio = 1.0
    if ratio < 0.0:
        ratio = 0.0
    return ratio


# ---------------------------------------------------------------------------
# Step 6 — aggregate per agent
# ---------------------------------------------------------------------------

def aggregate(commits: List[Commit], repo_dir) -> List[Dict[str, Any]]:
    """Roll up survival stats per AI agent.

    Returns a list of dicts:
      `{"agent", "evidence", "commits", "introduced", "surviving", "survival"}`

    Non-AI commits are skipped. The strongest evidence class across the
    agent's commits is reported.
    """
    repo = _to_path(repo_dir)
    per_agent: Dict[str, Dict[str, Any]] = {}
    for c in commits:
        is_ai, agent, evidence = detect_ai(c)
        if not is_ai or agent is None:
            continue
        # Ensure introduced + files are populated.
        if c.introduced == 0 and not c.files:
            introduced, files = _numstat_for_commit(c.sha, repo)
            c.introduced = introduced
            c.files = files
        # Compute survival (also populates c.surviving).
        compute_survival(c, repo)
        slot = per_agent.setdefault(agent, {
            "agent": agent,
            "evidence": Evidence.UNKNOWN.value,
            "commits": 0,
            "introduced": 0,
            "surviving": 0,
            "survival": 0.0,
        })
        slot["commits"] += 1
        slot["introduced"] += c.introduced
        slot["surviving"] += c.surviving
        # Promote evidence: VERIFIED > PROBABLE > UNKNOWN.
        if evidence == Evidence.VERIFIED:
            slot["evidence"] = Evidence.VERIFIED.value
        elif evidence == Evidence.PROBABLE and slot["evidence"] != Evidence.VERIFIED.value:
            slot["evidence"] = Evidence.PROBABLE.value
    # Final survival ratio per agent.
    for slot in per_agent.values():
        if slot["introduced"] > 0:
            r = slot["surviving"] / slot["introduced"]
            slot["survival"] = min(max(r, 0.0), 1.0)
        else:
            slot["survival"] = 0.0
    # Sort by descending introduced, then agent name for determinism.
    return sorted(
        per_agent.values(),
        key=lambda a: (-a["introduced"], a["agent"]),
    )
