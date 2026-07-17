"""
developer_agent.py — Calls the Developer Agent LLM to generate source code
                     and tests from a PRD + TDD specification pair.

Output contract (JSON the LLM must produce)
────────────────────────────────────────────
{
  "files": [
    { "path": "src/module.py",         "content": "<full source>" },
    { "path": "tests/test_module.py",  "content": "<full test source>" }
  ],
  "test_command": "pytest tests/ -v --cov=src --cov-report=term"
}

All paths are relative to generated/<feature_name>/.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from tools.config import SPECS_DIR, OUTPUT_DIR
from tools.llm_client import LLMClient
from tools.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

_INSTRUCTIONS_FILE = (
    Path(__file__).resolve().parent.parent
    / "agent_instructions"
    / "developer_agent.md"
)


def _load_instructions() -> str:
    return _INSTRUCTIONS_FILE.read_text(encoding="utf-8")


def _build_prompt(prd_text: str, tdd_text: str) -> str:
    instructions = _load_instructions()
    return (
        f"{instructions}\n\n"
        "──────────────────────────────────────────────\n"
        "PRD:\n"
        f"```\n{prd_text}\n```\n\n"
        "TDD:\n"
        f"```\n{tdd_text}\n```\n\n"
        "Now generate the implementation.  Remember: respond with ONLY valid JSON."
    )


async def generate_feature(feature_name: str) -> dict:
    """
    Ask the Developer Agent LLM to produce code for *feature_name*.

    Returns the parsed JSON dict:
        { "files": [...], "test_command": "..." }

    Raises ValueError if the LLM response cannot be parsed.
    """
    prd_path = SPECS_DIR / feature_name / "prd.md"
    tdd_path = SPECS_DIR / feature_name / "tdd.yaml"

    for p in (prd_path, tdd_path):
        if not p.exists():
            raise FileNotFoundError(f"Spec file not found: {p}")

    prd_text = prd_path.read_text(encoding="utf-8")
    tdd_text = tdd_path.read_text(encoding="utf-8")

    prompt = _build_prompt(prd_text, tdd_text)
    system = (
        "You are an expert developer agent. "
        "Always respond with the EXACT JSON structure requested — "
        "no markdown fences, no preamble, no trailing text."
    )

    client = LLMClient(role="developer")
    logger.info("[DeveloperAgent] Calling LLM for feature '%s'…", feature_name)
    raw = await client.generate(system, prompt)

    return _parse_response(raw, feature_name)


def _parse_response(raw: str, feature_name: str) -> dict:
    """Extract and validate the JSON payload from the LLM response."""
    data = parse_llm_json(raw, context=f"DeveloperAgent:{feature_name}")

    if "files" not in data or not isinstance(data["files"], list):
        raise ValueError(
            f"[DeveloperAgent] JSON missing 'files' list for '{feature_name}'."
        )
    if not data["files"]:
        raise ValueError(
            f"[DeveloperAgent] LLM returned empty 'files' list for '{feature_name}'."
        )

    logger.info(
        "[DeveloperAgent] Generated %d file(s): %s",
        len(data["files"]),
        [f["path"] for f in data["files"]],
    )
    return data


async def write_generated_files(feature_name: str, files: list[dict]) -> Path:
    """
    Write *files* to OUTPUT_DIR/<feature_name>/, wiping any previous output.
    Returns the feature output directory.
    """
    feature_dir = OUTPUT_DIR / feature_name

    # Clean previous run
    if feature_dir.exists():
        shutil.rmtree(feature_dir)

    for file_info in files:
        rel_path: str = file_info["path"]
        content:  str = file_info["content"]

        dest = feature_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        logger.debug("[DeveloperAgent] Wrote %s", dest.relative_to(OUTPUT_DIR))

    logger.info("[DeveloperAgent] Files written to %s", feature_dir)

    # Write a conftest.py at the feature root so pytest can find src/
    conftest = feature_dir / "conftest.py"
    if not conftest.exists():
        conftest.write_text(
            "import sys, pathlib\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
            encoding="utf-8",
        )

    return feature_dir
