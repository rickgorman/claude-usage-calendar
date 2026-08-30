import importlib.util
import json
import os
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("claude-usage-calendar.py")
SPEC = importlib.util.spec_from_file_location("usage_calendar", MODULE_PATH)
usage_calendar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage_calendar)


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


class MultiAgentUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.claude_file = (
            self.root
            / ".claude/projects/demo"
            / "11111111-1111-1111-1111-111111111111.jsonl"
        )
        write_jsonl(
            self.claude_file,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-1",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 1,
                        },
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:01Z",
                    "message": {
                        "id": "msg-1",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 5,
                            "cache_creation_input_tokens": 1,
                        },
                    },
                },
            ],
        )

        self.codex_file = (
            self.root
            / ".codex/sessions/2026/01/02"
            / "rollout-2026-01-02T11-00-00-session.jsonl"
        )
        write_jsonl(
            self.codex_file,
            [
                {
                    "type": "event_msg",
                    "ordinal": 7,
                    "timestamp": "2026-01-02T11:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 5,
                                "cached_input_tokens": 80,
                                "cache_write_input_tokens": 2,
                            },
                            "last_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 5,
                                "cached_input_tokens": 80,
                                "cache_write_input_tokens": 2,
                            }
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "ordinal": 8,
                    "timestamp": "2026-01-02T11:00:01Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 5,
                                "cached_input_tokens": 80,
                                "cache_write_input_tokens": 2,
                            },
                            "last_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 5,
                                "cached_input_tokens": 80,
                                "cache_write_input_tokens": 2,
                            },
                        },
                    },
                },
            ],
        )

        self.grok_file = self.root / ".grok/sessions/project/session/updates.jsonl"
        write_jsonl(
            self.grok_file,
            [
                {
                    "timestamp": 1767355200,
                    "params": {
                        "sessionId": "session-1",
                        "update": {
                            "prompt_id": "prompt-1",
                            "usage": {
                                "inputTokens": 30,
                                "outputTokens": 2,
                                "cachedReadTokens": 10,
                                "cacheCreationTokens": 1,
                            },
                        },
                    },
                },
                {
                    "timestamp": 1767355201,
                    "params": {
                        "sessionId": "session-1",
                        "update": {
                            "prompt_id": "prompt-1",
                            "usage": {
                                "inputTokens": 40,
                                "outputTokens": 3,
                                "cachedReadTokens": 20,
                                "cacheCreationTokens": 1,
                            },
                        },
                    },
                },
            ],
        )

        session_id = "22222222-2222-2222-2222-222222222222"
        self.composer_file = (
            self.root
            / ".cursor/projects/demo/agent-transcripts"
            / session_id
            / f"{session_id}.jsonl"
        )
        self.composer_user_content = [{"type": "text", "text": "abcdefgh"}]
        self.composer_assistant_content = [{"type": "text", "text": "abcdefghijkl"}]
        write_jsonl(
            self.composer_file,
            [
                {"role": "user", "message": {"content": self.composer_user_content}},
                {
                    "role": "assistant",
                    "message": {"content": self.composer_assistant_content},
                },
            ],
        )
        os.utime(self.composer_file, (1767358800, 1767358800))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_discovers_each_agent_format(self):
        files = usage_calendar.find_session_files(str(self.root))
        self.assertEqual(
            {agent: len(paths) for agent, paths in files.items()},
            {"claude": 1, "codex": 1, "grok": 1, "composer": 1},
        )

    def test_parses_and_deduplicates_all_agents(self):
        files = usage_calendar.find_session_files(str(self.root))
        daily, hourly, count, agents = usage_calendar.parse_session_files(
            files, timezone.utc
        )

        composer_input = usage_calendar.estimate_tokens(self.composer_user_content)
        composer_output = usage_calendar.estimate_tokens(
            self.composer_assistant_content
        )
        totals = usage_calendar.totals_from_daily(daily)

        self.assertEqual(count, 5)
        self.assertEqual(totals["input_tokens"], 12 + 100 + 40 + composer_input)
        self.assertEqual(totals["output_tokens"], 4 + 5 + 3 + composer_output)
        self.assertEqual(totals["cache_read_input_tokens"], 5 + 80 + 20)
        self.assertEqual(totals["cache_creation_input_tokens"], 1 + 2 + 1)
        self.assertIn("10", hourly["2026-01-02"])
        self.assertTrue(agents["composer"]["estimated"])
        self.assertFalse(agents["claude"]["estimated"])
        self.assertEqual(agents["grok"]["unique_messages"], 1)
        self.assertEqual(
            agents["codex"]["hourly_usage"]["2026-01-02"]["11"][
                "input_tokens"
            ],
            100,
        )
        self.assertEqual(agents["claude"]["color"], "#d97757")

    def test_json_structure_keeps_aggregate_and_agent_views(self):
        files = usage_calendar.find_session_files(str(self.root))
        daily, hourly, count, agents = usage_calendar.parse_session_files(
            files, timezone.utc
        )
        output = usage_calendar.build_usage_data(
            daily, hourly, count, "UTC", agents
        )

        self.assertEqual(output["timezone"], "UTC")
        self.assertEqual(output["unique_messages"], 5)
        self.assertEqual(set(output["agents"]), set(usage_calendar.AGENT_NAMES))
        self.assertEqual(
            output["totals"]["total_tokens"],
            sum(output["totals"][field] for field in usage_calendar.TOKEN_FIELDS),
        )

        html = usage_calendar.generate_html(output)
        self.assertIn('data-breakdown="tokens"', html)
        self.assertIn('data-breakdown="providers"', html)
        self.assertIn("function providerValuesForDate", html)
        self.assertIn("provider-composer", html)


if __name__ == "__main__":
    unittest.main()
