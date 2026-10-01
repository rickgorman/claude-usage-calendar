import importlib.util
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import timedelta, timezone
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


class ParseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cache_dir = Path(self.temp_dir.name) / "cache"
        self.cache_path = str(self.cache_dir / "parse-cache-v1.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_claude_file(self, relative_path, records):
        path = self.root / relative_path
        write_jsonl(path, records)
        return path

    def claude_files_by_agent(self, *paths):
        return {
            agent: []
            for agent in usage_calendar.AGENT_NAMES
            if agent != "claude"
        } | {"claude": [str(path) for path in paths]}

    def parse_result(self, files_by_agent, tz, cache_path=None):
        daily, hourly, count, agents = usage_calendar.parse_session_files(
            files_by_agent, tz, cache_path
        )
        return daily, hourly, count, agents

    def test_default_cache_path_is_keyed_by_search_path_and_timezone(self):
        utc = timezone.utc
        other_tz = timezone(timedelta(hours=-8))
        cache_home = str(self.cache_dir)
        with unittest.mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache_home}):
            first = usage_calendar.default_cache_path("/tmp/sessions-a", utc)
            same = usage_calendar.default_cache_path("/tmp/sessions-a", utc)
            other_search = usage_calendar.default_cache_path("/tmp/sessions-b", utc)
            shifted = usage_calendar.default_cache_path("/tmp/sessions-a", other_tz)

        self.assertEqual(first, same)
        self.assertNotEqual(first, other_search)
        self.assertNotEqual(first, shifted)
        cache_dir = os.path.join(cache_home, "claude-usage-calendar")
        for path in (first, same, other_search, shifted):
            self.assertEqual(os.path.dirname(path), cache_dir)
            self.assertTrue(os.path.basename(path).startswith("parse-cache-v1-"))

    def test_cold_and_warm_runs_match_uncached_baseline(self):
        path = self.write_claude_file(
            ".claude/projects/demo/session-a.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-1",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "cache_read_input_tokens": 1,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path)
        tz = timezone.utc

        baseline = self.parse_result(files, tz, cache_path=None)
        cold = self.parse_result(files, tz, cache_path=self.cache_path)
        warm = self.parse_result(files, tz, cache_path=self.cache_path)

        self.assertEqual(cold, baseline)
        self.assertEqual(warm, baseline)
        self.assertTrue(os.path.isfile(self.cache_path))

    def test_warm_run_skips_parser_for_unchanged_files(self):
        path = self.write_claude_file(
            ".claude/projects/demo/session-b.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-2",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path)
        tz = timezone.utc
        counts = {"calls": 0}
        original = usage_calendar.PARSERS["claude"]

        def counting_parser(filepath, tz_info):
            counts["calls"] += 1
            yield from original(filepath, tz_info)

        usage_calendar.PARSERS["claude"] = counting_parser
        try:
            self.parse_result(files, tz, cache_path=self.cache_path)
            self.assertEqual(counts["calls"], 1)
            counts["calls"] = 0
            self.parse_result(files, tz, cache_path=self.cache_path)
            self.assertEqual(counts["calls"], 0)
        finally:
            usage_calendar.PARSERS["claude"] = original

    def test_modified_file_is_reparsed_and_updates_totals(self):
        path = self.write_claude_file(
            ".claude/projects/demo/session-c.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-3",
                        "usage": {
                            "input_tokens": 5,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path)
        tz = timezone.utc

        _, _, _, agents_before = self.parse_result(
            files, tz, cache_path=self.cache_path
        )
        write_jsonl(
            path,
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-3",
                        "usage": {
                            "input_tokens": 50,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        _, _, _, agents_after = self.parse_result(
            files, tz, cache_path=self.cache_path
        )

        self.assertEqual(
            agents_before["claude"]["totals"]["input_tokens"],
            5,
        )
        self.assertEqual(
            agents_after["claude"]["totals"]["input_tokens"],
            50,
        )

    def test_corrupt_cache_file_is_ignored(self):
        path = self.write_claude_file(
            ".claude/projects/demo/session-d.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-4",
                        "usage": {
                            "input_tokens": 7,
                            "output_tokens": 2,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path)
        tz = timezone.utc
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        Path(self.cache_path).write_text("{not-json", encoding="utf-8")

        daily, hourly, count, agents = self.parse_result(
            files, tz, cache_path=self.cache_path
        )
        self.assertEqual(count, 1)
        self.assertEqual(agents["claude"]["totals"]["input_tokens"], 7)
        payload = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["format_version"], usage_calendar.CACHE_FORMAT_VERSION)

    def test_different_timezone_invalidates_cache(self):
        path = self.write_claude_file(
            ".claude/projects/demo/session-e.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-5",
                        "usage": {
                            "input_tokens": 9,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path)
        utc = timezone.utc
        self.parse_result(files, utc, cache_path=self.cache_path)
        payload = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["tz"], str(utc))
        self.assertEqual(len(usage_calendar.load_parse_cache(self.cache_path, utc)), 1)
        self.assertEqual(
            usage_calendar.load_parse_cache(
                self.cache_path, timezone(timedelta(hours=-8))
            ),
            {},
        )

    def test_removed_file_entry_is_pruned_on_write(self):
        path_a = self.write_claude_file(
            ".claude/projects/demo/session-f1.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-6",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        path_b = self.write_claude_file(
            ".claude/projects/demo/session-f2.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "msg-7",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        tz = timezone.utc
        both = self.claude_files_by_agent(path_a, path_b)
        self.parse_result(both, tz, cache_path=self.cache_path)
        payload = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["files"]), 2)

        only_a = self.claude_files_by_agent(path_a)
        self.parse_result(only_a, tz, cache_path=self.cache_path)
        payload = json.loads(Path(self.cache_path).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["files"]), 1)
        self.assertIn(os.path.abspath(str(path_a)), payload["files"])
        self.assertNotIn(os.path.abspath(str(path_b)), payload["files"])

    def test_claude_cross_file_dedup_matches_warm_and_cold(self):
        path_a = self.write_claude_file(
            ".claude/projects/demo/session-g1.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:00Z",
                    "message": {
                        "id": "shared-msg",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        path_b = self.write_claude_file(
            ".claude/projects/demo/session-g2.jsonl",
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02T10:00:01Z",
                    "message": {
                        "id": "shared-msg",
                        "usage": {
                            "input_tokens": 25,
                            "output_tokens": 4,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    },
                }
            ],
        )
        files = self.claude_files_by_agent(path_a, path_b)
        tz = timezone.utc

        cold = self.parse_result(files, tz, cache_path=None)
        self.parse_result(files, tz, cache_path=self.cache_path)
        warm = self.parse_result(files, tz, cache_path=self.cache_path)

        self.assertEqual(warm, cold)
        self.assertEqual(cold[3]["claude"]["totals"]["input_tokens"], 25)
        self.assertEqual(cold[3]["claude"]["totals"]["output_tokens"], 4)


if __name__ == "__main__":
    unittest.main()
