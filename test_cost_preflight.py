import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

import cost_preflight as hook


SCRIPT = Path(__file__).with_name("cost_preflight.py")


class CostPreflightHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name) / "state"
        self.payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-test-123",
            "turn_id": "turn-test-1",
            "cwd": "/tmp/example-project",
            "transcript_path": "",
            "permission_mode": "default",
            "prompt": "Format this short list as a Markdown table.",
            "model": "gpt-5.6-terra",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def hook_environment(self, **env_overrides):
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_COST_PREFLIGHT_STATE_DIR": str(self.state_dir),
                "CODEX_COST_PREFLIGHT_MIN_CONFIRM_DELAY_SECONDS": "0",
                "CODEX_COST_PREFLIGHT_TTL_SECONDS": "90",
            }
        )
        environment.update(env_overrides)
        return environment

    def run_hook(self, payload=None, **env_overrides):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload if payload is not None else self.payload),
            text=True,
            capture_output=True,
            env=self.hook_environment(**env_overrides),
            check=False,
        )

    @staticmethod
    def high_credit(reason, model_name):
        match = re.search(rf"{model_name}: ([0-9.]+)-([0-9.]+) credits", reason)
        if match is None:
            raise AssertionError(f"Missing {model_name} range in: {reason}")
        return float(match.group(2))

    def test_first_submission_blocks_before_model_dispatch(self):
        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["decision"], "block")
        self.assertIn("No model request was sent", response["reason"])

    def test_second_identical_submission_is_allowed(self):
        first = self.run_hook()
        second = self.run_hook()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

    def test_block_message_includes_token_and_three_model_cost_ranges(self):
        result = self.run_hook()

        reason = json.loads(result.stdout)["reason"]
        token_match = re.search(r"Draft: ([0-9,]+)-([0-9,]+) tokens", reason)
        self.assertIsNotNone(token_match, reason)
        self.assertLess(
            int(token_match.group(1).replace(",", "")),
            int(token_match.group(2).replace(",", "")),
        )

        highs = {}
        for model_name in ("Sol", "Terra", "Luna"):
            match = re.search(
                rf"{model_name}: ([0-9.]+)-([0-9.]+) credits", reason
            )
            self.assertIsNotNone(match, reason)
            low, high = map(float, match.groups())
            self.assertGreater(low, 0)
            self.assertGreater(high, low)
            highs[model_name] = high

        self.assertGreater(highs["Sol"], highs["Terra"])
        self.assertGreater(highs["Terra"], highs["Luna"])

    def test_recommendation_matches_prompt_complexity(self):
        cases = (
            (
                "Format this short list as a Markdown table.",
                "Recommended: Luna",
            ),
            (
                "In README.md, change 'teh' to 'the'. Change nothing else.",
                "Recommended: Luna",
            ),
            (
                "Implement a tested CSV export in this Python module and update "
                "the documentation.",
                "Recommended: Terra",
            ),
            (
                "Investigate an intermittent production race condition across "
                "multiple services, identify the root cause, and propose a safe "
                "migration architecture.",
                "Recommended: Sol",
            ),
            (
                "Diagnose an intermittent production race causing duplicate "
                "charges and design a zero-downtime repair.",
                "Recommended: Sol",
            ),
        )

        for index, (prompt, expected) in enumerate(cases):
            with self.subTest(prompt=prompt):
                payload = dict(self.payload)
                payload["session_id"] = f"recommendation-{index}"
                payload["prompt"] = prompt
                result = self.run_hook(payload)
                reason = json.loads(result.stdout)["reason"]
                self.assertIn(expected, reason)

    def test_model_change_uses_the_already_displayed_comparison(self):
        first = self.run_hook()
        changed = dict(self.payload)
        changed["model"] = "gpt-5.6-sol"

        second = self.run_hook(changed)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

    def test_changed_prompt_requires_a_new_preview(self):
        first = self.run_hook()
        changed = dict(self.payload)
        changed["prompt"] += " "

        second = self.run_hook(changed)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(second.stdout)["decision"], "block")

    def test_same_prompt_in_a_different_session_requires_a_new_preview(self):
        first = self.run_hook()
        changed = dict(self.payload)
        changed["session_id"] = "another-session"

        second = self.run_hook(changed)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(second.stdout)["decision"], "block")

    def test_confirmation_is_one_shot(self):
        first = self.run_hook()
        second = self.run_hook()
        third = self.run_hook()

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.stdout, "")
        self.assertEqual(json.loads(third.stdout)["decision"], "block")

    def test_approval_state_contains_no_prompt_and_is_owner_only(self):
        payload = dict(self.payload)
        payload["prompt"] = "PRIVATE-PROMPT-SENTINEL-4729"

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        state_files = list(self.state_dir.glob("*.json"))
        self.assertEqual(len(state_files), 1)
        self.assertNotIn(payload["prompt"], state_files[0].read_text(encoding="utf-8"))
        self.assertNotIn(payload["prompt"], str(state_files[0]))
        self.assertEqual(stat.S_IMODE(state_files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    def test_malformed_hook_input_fails_open_with_a_warning(self):
        malformed_inputs = (
            "{not-json",
            json.dumps([]),
            json.dumps({"session_id": "session", "prompt": None}),
            json.dumps({"prompt": "missing session"}),
        )

        for raw_input in malformed_inputs:
            with self.subTest(raw_input=raw_input):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT)],
                    input=raw_input,
                    text=True,
                    capture_output=True,
                    env=self.hook_environment(),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                response = json.loads(result.stdout)
                self.assertTrue(response["continue"])
                self.assertIn("Cost preflight unavailable", response["systemMessage"])
                self.assertNotIn("decision", response)

    def test_unrelated_hook_event_passes_through_without_state(self):
        payload = dict(self.payload)
        payload["hook_event_name"] = "SessionStart"

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.state_dir.exists())

    def test_previous_thread_usage_increases_the_projected_range(self):
        without_history = dict(self.payload)
        without_history["session_id"] = "without-history"
        baseline = self.run_hook(without_history)
        baseline_reason = json.loads(baseline.stdout)["reason"]

        transcript = Path(self.temp_dir.name) / "thread.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 60_000,
                                "cached_input_tokens": 50_000,
                                "output_tokens": 1_000,
                                "reasoning_output_tokens": 100,
                            }
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with_history = dict(self.payload)
        with_history["session_id"] = "with-history"
        with_history["transcript_path"] = str(transcript)

        historical = self.run_hook(with_history)
        historical_reason = json.loads(historical.stdout)["reason"]

        self.assertGreater(
            self.high_credit(historical_reason, "Sol"),
            self.high_credit(baseline_reason, "Sol"),
        )

    def test_confirmation_window_in_message_uses_configured_ttl(self):
        result = self.run_hook(CODEX_COST_PREFLIGHT_TTL_SECONDS="45")

        reason = json.loads(result.stdout)["reason"]
        self.assertIn("within 45 seconds", reason)

    def test_click_before_minimum_delay_preserves_the_original_preview(self):
        environment = {
            "CODEX_COST_PREFLIGHT_MIN_CONFIRM_DELAY_SECONDS": "60",
            "CODEX_COST_PREFLIGHT_TTL_SECONDS": "90",
        }
        first = self.run_hook(**environment)
        state_file = next(self.state_dir.glob("*.json"))
        original_created_at = json.loads(
            state_file.read_text(encoding="utf-8")
        )["created_at"]

        second = self.run_hook(**environment)
        current_created_at = json.loads(
            state_file.read_text(encoding="utf-8")
        )["created_at"]

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(second.stdout)["decision"], "block")
        self.assertEqual(current_created_at, original_created_at)

    def test_old_state_files_are_removed_during_a_new_preview(self):
        self.state_dir.mkdir(parents=True, mode=0o700)
        self.state_dir.chmod(0o700)
        stale = self.state_dir / "stale.json"
        stale.write_text(
            json.dumps({"created_at": 1, "status": "pending"}),
            encoding="utf-8",
        )
        os.utime(stale, (1, 1))

        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())

    def test_insecure_existing_state_directory_is_not_repermissioned(self):
        insecure = Path(self.temp_dir.name) / "shared-state"
        insecure.mkdir(mode=0o755)
        insecure.chmod(0o755)

        result = self.run_hook(CODEX_COST_PREFLIGHT_STATE_DIR=str(insecure))

        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertIs(response.get("continue"), True, response)
        self.assertEqual(stat.S_IMODE(insecure.stat().st_mode), 0o755)
        self.assertEqual(list(insecure.iterdir()), [])

    def test_concurrent_confirmations_allow_only_one_submission(self):
        initial = self.run_hook()
        self.assertEqual(initial.returncode, 0, initial.stderr)

        processes = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.hook_environment(),
            )
            for _ in range(8)
        ]
        results = [process.communicate(json.dumps(self.payload)) for process in processes]

        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        allowed = sum(stdout == "" for stdout, _ in results)
        blocked = sum(
            bool(stdout) and json.loads(stdout).get("decision") == "block"
            for stdout, _ in results
        )
        self.assertEqual(allowed, 1, results)
        self.assertEqual(blocked, 7, results)

    def test_credit_math_does_not_double_charge_cached_input(self):
        workload = {"input_tokens": 10_000, "cached_tokens": 8_000, "output_tokens": 600}
        expected = {"Sol": 0.800, "Terra": 0.320, "Luna": 0.032}

        for model_name, expected_cost in expected.items():
            with self.subTest(model=model_name):
                actual = hook.credit_cost(
                    workload["input_tokens"],
                    workload["cached_tokens"] / workload["input_tokens"],
                    workload["output_tokens"],
                    hook.MODEL_RATES[model_name],
                )
                self.assertAlmostEqual(actual, expected_cost, places=6)

    def test_transcript_parser_uses_latest_last_usage_not_cumulative_totals(self):
        transcript = Path(self.temp_dir.name) / "usage.jsonl"
        lines = [
            "not json",
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 2_000,
                                "cached_input_tokens": 1_000,
                                "output_tokens": 200,
                            }
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 900_000,
                                "cached_input_tokens": 800_000,
                                "output_tokens": 90_000,
                            },
                            "last_token_usage": {
                                "input_tokens": 10_000,
                                "cached_input_tokens": 8_000,
                                "output_tokens": 600,
                                "reasoning_output_tokens": 200,
                            },
                        },
                    },
                }
            ),
        ]
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

        usage = hook.read_last_token_usage(transcript)

        self.assertEqual(usage["input_tokens"], 10_000)
        self.assertEqual(usage["cached_input_tokens"], 8_000)
        self.assertEqual(usage["output_tokens"], 600)


if __name__ == "__main__":
    unittest.main()
