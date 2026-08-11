#!/usr/bin/env python3
"""Gate-3 calibration harness for the Ralph Dispatch critic gate.

The corpus in this directory fixes worker outputs deliberately: each case is a
known-good or known-flawed work product plus the evidence pack that proves it.
That isolates what is being measured — the critic model and the code-level
routing gate — from live worker variance. Worker-model calibration (schema-valid
rate by tier, escalation lift) is measured separately from the model_calls
ledger during the supervised canary.

Three subcommands:

- ``validate``: corpus/gold integrity. Every critic-layer case must pass the
  exact runtime validators in ``dispatch.py``; every runtime-layer case must
  fail them with the named error. Evidence hashes, URL policy, and gold-label
  well-formedness are checked. CI-safe: no network, no model.
- ``run``: execute the critic-layer cases against a real critic endpoint,
  building each critic payload exactly as the dispatcher does, and record
  observed verdicts with full provenance (model id, prompt hash, corpus hash).
- ``score``: join observed verdicts with gold labels and report the Gate-3
  metrics. The release bar is executable: any flawed case that the code-level
  gate would commit is a critical false pass and fails the command.

Scoring is against the *system*, not the label string: a case "commits" only if
code-level routing would commit it (PASS with risk < 0.3, or CONDITIONAL_PASS
with risk < 0.5), mirroring ``route_critic_verdict``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CALIBRATION_DIR = Path(__file__).resolve().parent
ROOT = CALIBRATION_DIR.parent
sys.path.insert(0, str(ROOT))

import dispatch  # noqa: E402

CORPUS_PATH = CALIBRATION_DIR / "corpus.json"
GOLD_PATH = CALIBRATION_DIR / "gold.json"

STRATA = {
    "clean",
    "missing-disclosure",
    "contradiction",
    "rename-dedupe",
    "injection",
    "runtime-reject",
    "unsupported-quant",
    "legit-conditional",
}
VERDICTS = {"PASS", "CONDITIONAL_PASS", "FAIL"}
WORKER_KINDS = {"scout", "dossier_section", "dossier_assemble"}
# Fictional-only URL policy: the reserved .invalid TLD can never resolve, so a
# corpus case can never be mistaken for (or leak) a real-company claim.
REQUIRED_URL_SUFFIX = ".calibration.invalid"


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise dispatch.ValidationError(f"{label} is missing: {path}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise dispatch.ValidationError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def load_corpus(path: Path = CORPUS_PATH) -> list[dict[str, Any]]:
    decoded = _load_json(path, "corpus")
    if not isinstance(decoded, dict) or not isinstance(decoded.get("cases"), list):
        raise dispatch.ValidationError("corpus must be an object containing cases[]")
    return decoded["cases"]


def load_gold(path: Path = GOLD_PATH) -> dict[str, dict[str, Any]]:
    decoded = _load_json(path, "gold")
    if not isinstance(decoded, dict) or not isinstance(decoded.get("labels"), dict):
        raise dispatch.ValidationError("gold must be an object containing labels{}")
    return decoded["labels"]


def _check_urls(value: Any, problems: list[str], case_id: str) -> None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        hostname = dispatch.urllib.parse.urlsplit(value).hostname or ""
        if not hostname.endswith(REQUIRED_URL_SUFFIX):
            problems.append(f"{case_id}: URL outside {REQUIRED_URL_SUFFIX}: {value}")
    elif isinstance(value, Mapping):
        for child in value.values():
            _check_urls(child, problems, case_id)
    elif isinstance(value, list):
        for child in value:
            _check_urls(child, problems, case_id)


def _validate_case_shape(case: Any, index: int, problems: list[str]) -> str | None:
    if not isinstance(case, dict):
        problems.append(f"cases[{index}] must be an object")
        return None
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id.strip():
        problems.append(f"cases[{index}].case_id is required")
        return None
    if case.get("stratum") not in STRATA:
        problems.append(f"{case_id}: unknown stratum {case.get('stratum')!r}")
    if case.get("layer") not in {"critic", "runtime"}:
        problems.append(f"{case_id}: layer must be critic or runtime")
    if case.get("kind") not in WORKER_KINDS:
        problems.append(f"{case_id}: unknown kind {case.get('kind')!r}")
    if not isinstance(case.get("payload"), dict):
        problems.append(f"{case_id}: payload must be an object")
    if not isinstance(case.get("worker_response"), dict):
        problems.append(f"{case_id}: worker_response must be an object")
    return case_id


def _validate_case_runtime(case: dict[str, Any], problems: list[str]) -> None:
    """A critic-layer case must clear the real validators; a runtime-layer case
    must fail them with the named error. Both run the actual dispatch code."""
    case_id = case["case_id"]
    kind = case["kind"]
    payload = case["payload"]
    try:
        dispatch.validate_payload(kind, payload)
    except dispatch.ValidationError as exc:
        problems.append(f"{case_id}: payload rejected by validate_payload: {exc}")
        return
    for item in payload.get("evidence", []):
        expected = item.get("sha256")
        if expected is None:
            problems.append(f"{case_id}: evidence {item.get('source_id')} is missing sha256")
    text = dispatch.canonical_json(case["worker_response"])
    if case["layer"] == "critic":
        try:
            dispatch.parse_worker_response(kind, text, payload)
        except dispatch.ValidationError as exc:
            problems.append(f"{case_id}: critic-layer worker_response failed runtime: {exc}")
    else:
        expected_error = case.get("expected", {}).get("runtime_error_contains")
        try:
            dispatch.parse_worker_response(kind, text, payload)
            problems.append(f"{case_id}: runtime-layer worker_response unexpectedly validated")
        except dispatch.ValidationError as exc:
            if not expected_error or expected_error not in str(exc):
                problems.append(
                    f"{case_id}: runtime error {str(exc)!r} does not contain {expected_error!r}"
                )


def _validate_gold_entry(
    case: dict[str, Any],
    gold: Mapping[str, Any] | None,
    problems: list[str],
) -> None:
    case_id = case["case_id"]
    if gold is None:
        problems.append(f"{case_id}: no gold label")
        return
    acceptable = gold.get("acceptable_verdicts")
    if case["layer"] == "critic":
        if not isinstance(acceptable, list) or not acceptable or not set(acceptable) <= VERDICTS:
            problems.append(f"{case_id}: acceptable_verdicts must be a non-empty verdict list")
        if gold.get("flawed") and acceptable and "PASS" in acceptable:
            problems.append(f"{case_id}: a flawed case cannot accept PASS")
    elif acceptable is not None:
        problems.append(f"{case_id}: runtime-layer gold must have acceptable_verdicts null")
    if not isinstance(gold.get("flawed"), bool):
        problems.append(f"{case_id}: gold.flawed must be a boolean")
    if not isinstance(gold.get("must_mention"), list):
        problems.append(f"{case_id}: gold.must_mention must be a list")
    if not isinstance(gold.get("rationale"), str) or not gold["rationale"].strip():
        problems.append(f"{case_id}: gold.rationale is required for the human graders")
    grading = gold.get("grading")
    if not isinstance(grading, dict) or set(grading) != {
        "grader_a",
        "grader_b",
        "reconciled",
        "notes",
    }:
        problems.append(f"{case_id}: gold.grading must have grader_a/grader_b/reconciled/notes")
    elif isinstance(grading.get("reconciled"), list) and not (
        set(grading["reconciled"]) <= VERDICTS and grading["reconciled"]
    ):
        problems.append(f"{case_id}: grading.reconciled must be a non-empty verdict list")


def validate_corpus(
    corpus_path: Path = CORPUS_PATH,
    gold_path: Path = GOLD_PATH,
) -> list[str]:
    """Return every corpus/gold integrity problem. Empty means calibratable."""
    problems: list[str] = []
    cases = load_corpus(corpus_path)
    labels = load_gold(gold_path)
    seen: set[str] = set()
    for index, case in enumerate(cases):
        case_id = _validate_case_shape(case, index, problems)
        if case_id is None:
            continue
        if case_id in seen:
            problems.append(f"duplicate case_id: {case_id}")
            continue
        seen.add(case_id)
        if not isinstance(case.get("payload"), dict) or not isinstance(
            case.get("worker_response"), dict
        ):
            continue
        _check_urls(case["payload"], problems, case_id)
        _check_urls(case["worker_response"], problems, case_id)
        _validate_case_runtime(case, problems)
        _validate_gold_entry(case, labels.get(case_id), problems)
    for orphan in sorted(set(labels) - seen):
        problems.append(f"gold label without a corpus case: {orphan}")
    strata_present = {case.get("stratum") for case in cases if isinstance(case, dict)}
    for missing in sorted(STRATA - strata_present):
        problems.append(f"stratum has no cases: {missing}")
    return problems


# ------------------------------------------------------------------------ run


def build_critic_payload(case: Mapping[str, Any]) -> dict[str, Any]:
    """Replicate exactly what _handle_worker_success sends to the critic."""
    envelope = case["worker_response"]
    result_json = dispatch.canonical_json(envelope)
    return {
        "original_task": case["payload"]["task"],
        "work_product": dispatch.canonical_json(envelope["RESULT"]),
        "worker_confidence": envelope["CONFIDENCE"],
        "worker_result_hash": dispatch.sha256_text(result_json),
        "evidence": case["payload"].get("evidence", []),
    }


def run_critic(
    client: dispatch.ModelClient,
    cases: Sequence[Mapping[str, Any]],
    *,
    timeout_seconds: float = dispatch.REQUEST_TIMEOUT_SECONDS,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Call the critic once per critic-layer case; observations fail closed."""
    tier = dispatch.TIERS["T3_critic"]
    observations: list[dict[str, Any]] = []
    selected = [case for case in cases if case["layer"] == "critic"]
    if limit is not None:
        selected = selected[:limit]
    for case in selected:
        critic_payload = build_critic_payload(case)
        system, user = dispatch.load_prompt("critic_review", critic_payload)
        observation: dict[str, Any] = {"case_id": case["case_id"], "stratum": case["stratum"]}
        try:
            raw = client.complete(tier["model"], system, user, tier["max_tokens"], timeout_seconds)
            response = dispatch._normalize_model_response(raw)
            verdict = dispatch.parse_critic_response(response.text)
            observation["verdict"] = verdict
        except dispatch.DispatchError as exc:
            # An unparseable critic response is itself a calibration datum: in
            # production it retries and then parks, never commits.
            observation["error"] = f"{type(exc).__name__}: {exc}"[:500]
        observations.append(observation)
    return observations


def run_provenance(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    corpus_hash = dispatch.sha256_text(dispatch.canonical_json(list(cases)))
    prompt_path = dispatch.PROMPT_FILES["critic_review"]
    return {
        "corpus_sha256": corpus_hash,
        "critic_prompt_sha256": dispatch.sha256_text(prompt_path.read_text(encoding="utf-8")),
        "critic_model": dispatch.TIERS["T3_critic"]["model"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ---------------------------------------------------------------------- score


def would_commit(verdict: Mapping[str, Any]) -> bool:
    """Mirror route_critic_verdict's code-level thresholds exactly."""
    risk = verdict["RISK_SCORE"]
    return (verdict["VERDICT"] == "PASS" and risk < 0.3) or (
        verdict["VERDICT"] == "CONDITIONAL_PASS" and risk < 0.5
    )


def _mention_hit(verdict: Mapping[str, Any], keywords: Sequence[str]) -> bool:
    if not keywords:
        return True
    all_issues = list(verdict.get("CRITICAL_ISSUES", [])) + list(verdict.get("MINOR_ISSUES", []))
    issues = " ".join(str(issue) for issue in all_issues).casefold()
    return any(keyword.casefold() in issues for keyword in keywords)


def score(
    cases: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    *,
    require_reconciled: bool = False,
) -> dict[str, Any]:
    observed_by_id = {obs["case_id"]: obs for obs in observations}
    per_case: list[dict[str, Any]] = []
    confusion: dict[str, dict[str, int]] = {}
    flawed_total = flawed_caught = 0
    clean_total = clean_committed = 0
    mention_total = mention_hits = 0
    invalid_outputs = missing = 0
    unreconciled = 0
    critical_false_passes: list[str] = []

    for case in cases:
        if case["layer"] != "critic":
            continue
        case_id = case["case_id"]
        gold = labels[case_id]
        reconciled = (gold.get("grading") or {}).get("reconciled")
        acceptable = reconciled or gold["acceptable_verdicts"]
        if not reconciled:
            unreconciled += 1
        row: dict[str, Any] = {
            "case_id": case_id,
            "stratum": case["stratum"],
            "flawed": gold["flawed"],
            "acceptable_verdicts": acceptable,
            "reconciled": bool(reconciled),
        }
        observation = observed_by_id.get(case_id)
        if observation is None:
            missing += 1
            row["outcome"] = "missing"
            per_case.append(row)
            continue
        if "error" in observation:
            invalid_outputs += 1
            row["outcome"] = "invalid_critic_output"
            row["error"] = observation["error"]
            if gold["flawed"]:
                flawed_total += 1
                flawed_caught += 1  # an unparseable verdict never commits
            per_case.append(row)
            continue
        verdict = observation["verdict"]
        committed = would_commit(verdict)
        gold_key = "|".join(sorted(acceptable))
        confusion.setdefault(gold_key, {}).setdefault(verdict["VERDICT"], 0)
        confusion[gold_key][verdict["VERDICT"]] += 1
        row.update(
            {
                "outcome": "scored",
                "observed_verdict": verdict["VERDICT"],
                "observed_risk": verdict["RISK_SCORE"],
                "would_commit": committed,
                "verdict_in_gold": verdict["VERDICT"] in acceptable,
            }
        )
        if gold["flawed"]:
            flawed_total += 1
            if committed:
                critical_false_passes.append(case_id)
            else:
                flawed_caught += 1
        if acceptable == ["PASS"]:
            clean_total += 1
            clean_committed += int(committed)
        if gold["must_mention"]:
            mention_total += 1
            mention_hits += int(_mention_hit(verdict, gold["must_mention"]))
        per_case.append(row)

    if require_reconciled and unreconciled:
        raise dispatch.ValidationError(
            f"{unreconciled} cases lack a two-grader reconciled label; "
            "complete Gate 3 grading before an acceptance score"
        )

    report = {
        "cases_scored": len(per_case),
        "critical_false_passes": critical_false_passes,
        "critical_error_recall": (flawed_caught / flawed_total) if flawed_total else None,
        "false_pass_rate": (len(critical_false_passes) / flawed_total) if flawed_total else None,
        "clean_commit_rate": (clean_committed / clean_total) if clean_total else None,
        "issue_mention_hit_rate": (mention_hits / mention_total) if mention_total else None,
        "invalid_critic_outputs": invalid_outputs,
        "missing_observations": missing,
        "unreconciled_gold_labels": unreconciled,
        "confusion": confusion,
        "release_bar_met": not critical_false_passes and not missing,
        "per_case": per_case,
    }
    return report


# ------------------------------------------------------------------------ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--gold", default=str(GOLD_PATH))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check corpus and gold integrity against the runtime")

    run_parser = sub.add_parser("run", help="collect critic verdicts for critic-layer cases")
    run_parser.add_argument("--provider", choices=("anthropic", "openai"), required=True)
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--api-key-env")
    run_parser.add_argument("--timeout", type=float, default=dispatch.REQUEST_TIMEOUT_SECONDS)
    run_parser.add_argument("--limit", type=int, help="only the first N critic cases")
    run_parser.add_argument("--out", required=True, help="write observations JSON here")

    score_parser = sub.add_parser("score", help="score observations against gold labels")
    score_parser.add_argument("--observations", required=True)
    score_parser.add_argument(
        "--require-reconciled",
        action="store_true",
        help="refuse to score until every gold label carries a two-grader reconciliation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    corpus_path = Path(args.corpus)
    gold_path = Path(args.gold)
    try:
        if args.command == "validate":
            problems = validate_corpus(corpus_path, gold_path)
            print(json.dumps({"clean": not problems, "problems": problems}, indent=2))
            return 0 if not problems else 1

        problems = validate_corpus(corpus_path, gold_path)
        if problems:
            raise dispatch.ValidationError(
                f"corpus failed validation with {len(problems)} problems; run validate"
            )
        cases = load_corpus(corpus_path)
        labels = load_gold(gold_path)

        if args.command == "run":
            base_url = args.base_url or (
                "https://api.anthropic.com"
                if args.provider == "anthropic"
                else "http://127.0.0.1:8000"
            )
            key_env = args.api_key_env
            if key_env is None:
                key_env = "ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY"
            client = dispatch.HttpModelClient(args.provider, base_url, key_env, args.timeout)
            client.preflight()
            observations = run_critic(client, cases, timeout_seconds=args.timeout, limit=args.limit)
            document = {"provenance": run_provenance(cases), "observations": observations}
            Path(args.out).write_text(
                json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(json.dumps({"observed": len(observations), "out": args.out}))
            return 0

        if args.command == "score":
            document = _load_json(Path(args.observations), "observations")
            observations = document.get("observations")
            if not isinstance(observations, list):
                raise dispatch.ValidationError("observations file must contain observations[]")
            recorded = (document.get("provenance") or {}).get("corpus_sha256")
            current = run_provenance(cases)["corpus_sha256"]
            if recorded is not None and recorded != current:
                raise dispatch.ValidationError(
                    "observations were collected against a different corpus revision; rerun"
                )
            report = score(cases, labels, observations, require_reconciled=args.require_reconciled)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report["release_bar_met"] else 1
        return 2
    except (dispatch.DispatchError, FileNotFoundError, OSError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
