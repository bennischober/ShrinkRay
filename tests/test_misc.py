"""Helper and config-hygiene tests. Split from tests/test_shrinkray.py (2026-10-23)."""

import ast
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from shrinkray import config as config_helper


class TestScanTimeAndUnitHelpers(unittest.TestCase):
    """The scan engine's time/size helpers, now living in engine/scan.py."""

    def test_convert_unit(self):
        from shrinkray.engine.scan import SIZE_UNIT, convert_unit

        self.assertEqual(convert_unit(1024, SIZE_UNIT.KB), 1)
        self.assertEqual(convert_unit(1048576, SIZE_UNIT.MB), 1)
        self.assertEqual(convert_unit(1073741824, SIZE_UNIT.GB), 1)
        self.assertEqual(convert_unit(123, SIZE_UNIT.BYTES), 123)

    def test_timestr_to_int(self):
        from shrinkray.engine.scan import timestr_to_int

        self.assertEqual(timestr_to_int("01:02:03"), 3723)
        self.assertEqual(timestr_to_int("00:00:00"), 0)
        self.assertEqual(timestr_to_int(None), 0)
        self.assertEqual(timestr_to_int("garbage"), 0)

    def test_get_other_codecs(self):
        from shrinkray.engine.scan import get_other_codecs

        self.assertEqual(get_other_codecs([]), "")
        self.assertEqual(get_other_codecs([{"name": "aac"}]), "aac")
        self.assertEqual(get_other_codecs([{"name": "aac"}, {"name": "subrip"}]), "aac, subrip")


class TestCalcTime(unittest.TestCase):
    """calc_time lives with its only caller (__main__.py's plan loop)."""

    def test_calc_time(self):
        from shrinkray.__main__ import calc_time

        self.assertEqual(calc_time(None, "01:00:00"), "01:00:00")
        self.assertEqual(calc_time("01:00:00", None), "01:00:00")
        self.assertEqual(calc_time("00:59:59", "00:00:01"), "01:00:00")
        self.assertEqual(calc_time("10:00:00", "05:30:15"), "15:30:15")


class TestRemovedIntegrationsKey(unittest.TestCase):
    """The Jellyfin integration was removed (issue #43): a config.json that
    still carries its 'integrations' key must load, but the removal must be
    visible - exactly one warning naming the key, never silence."""

    def test_leftover_key_warns_once_and_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                '{"integrations": {"jellyfin": {"url": "http://nas:8096"}}}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(config_helper, "_config", None),
                mock.patch("shrinkray.config.log_warning") as warn,
            ):
                conf = config_helper.load_config(tmp)
            # the config still loads; every other default is intact
            self.assertEqual(conf.extensions, config_helper.DEFAULT_EXTENSIONS)
            # exactly one warning, naming the removed key
            self.assertEqual(warn.call_count, 1)
            self.assertIn("integrations", warn.call_args.args[0])

    def test_clean_config_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(config_helper, "_config", None),
                mock.patch("shrinkray.config.log_warning") as warn,
            ):
                config_helper.load_config(tmp)
            # the 'config file not found' warning is the only expected one
            self.assertFalse(any("integrations" in str(call.args) for call in warn.call_args_list))


class TestEncoderPresetConfig(unittest.TestCase):
    """handbrake.encoder_preset (issue #31): quality mode's encoder speed
    preset must be configurable, validated, and reachable from every entry
    point — before this, 'medium' was a constant no caller could override."""

    def test_default_presets_are_split(self):
        conf = config_helper.Conf({})
        # probe = measurement setting, final = output setting (issue #22)
        self.assertEqual(conf.handbrake_probe_encoder_preset, "5")
        self.assertEqual(conf.handbrake_encoder_preset, "3")

    def test_config_value_overrides_default(self):
        conf = config_helper.Conf({"handbrake": {"encoder_preset": "6"}})
        self.assertEqual(conf.handbrake_encoder_preset, "6")

    def test_invalid_preset_is_rejected_at_load(self):
        # a value outside the encoder's vocabulary must never reach HandBrake
        # ('Fast 1080p30' is the FIXED-mode preset; passing it as a speed
        # preset is exactly the footgun this validation prevents)
        with self.assertRaises(ValueError) as ctx:
            config_helper.Conf({"handbrake": {"encoder_preset": "Fast 1080p30"}})
        self.assertIn("encoder_preset", str(ctx.exception))
        self.assertIn("Fast 1080p30", str(ctx.exception))


class TestConfigSchema(unittest.TestCase):
    """The config.json schema (issue #61): every mistake is an error that
    names the key, never a silently-applied default."""

    def test_string_bool_is_rejected(self):
        # bool("false") == True was the old behavior: a string where a bool
        # belongs silently flipped the setting
        with self.assertRaises(Exception) as ctx:
            config_helper.Conf({"resources": {"idle_priority": "false"}})
        self.assertIn("idle_priority", str(ctx.exception))

    def test_unknown_section_is_rejected(self):
        # a typo'd section ('handbrak') silently used ALL defaults before
        with self.assertRaises(Exception) as ctx:
            config_helper.Conf({"handbrak": {"preset": "X"}})
        self.assertIn("handbrak", str(ctx.exception))

    def test_negative_thread_count_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            config_helper.Conf({"handbrake": {"max_threads": -4}})
        self.assertIn("max_threads", str(ctx.exception))

    def test_empty_extensions_is_rejected(self):
        # decided: REJECT (an empty list would silently scan nothing);
        # documented in the model and the README
        with self.assertRaises(Exception) as ctx:
            config_helper.Conf({"file": {"extensions": []}})
        self.assertIn("extensions", str(ctx.exception))

    def test_seperator_alias_loads_with_one_warning(self):
        with mock.patch("shrinkray.config.log_warning") as warn:
            conf = config_helper.Conf({"file": {"output": {"seperator": "|"}}})
        self.assertEqual(conf.separator, "|")
        self.assertEqual(warn.call_count, 1)
        self.assertIn("seperator", warn.call_args.args[0])

    def test_pydantic_is_a_declared_dependency(self):
        # the direct import in app.py was only satisfied transitively via
        # FastAPI (issue #61)
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pydantic", pyproject)


class TestSubprocessTimeouts(unittest.TestCase):
    """AGENTS.md: every subprocess call sets a timeout (issue #62). Enforced
    by AST, not re-audited: a new subprocess.run without timeout= fails the
    suite. The allowlist is the two watchdog-off fallback branches, whose
    operators explicitly opted out of stall protection (the code comments
    at those sites record the decision)."""

    ALLOWLIST: ClassVar[dict[str, tuple[int, ...]]] = {
        # (module filename, line numbers) of intentional no-timeout runs —
        # the blocking fallbacks used only when the stall watchdog is OFF
        # and no progress/cancel is requested; a fixed timeout would kill
        # hours-long healthy encodes:
        #   encode.py: the _encode_with_command non-streamed branch
        "encode.py": (259,),
    }

    def _run_calls(self, tree: ast.AST) -> list[ast.Call]:
        calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"subprocess", "_subprocess"}
            ):
                calls.append(node)
        return calls

    def test_every_subprocess_run_has_timeout(self):
        src_root = Path(__file__).resolve().parents[1] / "src" / "shrinkray"
        offenders: list[str] = []
        for py in sorted(src_root.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            allowed_lines = self.ALLOWLIST.get(py.name, ())
            for call in self._run_calls(tree):
                if any(kw.arg == "timeout" for kw in call.keywords):
                    continue
                if call.lineno in allowed_lines:
                    continue
                offenders.append(f"{py.name}:{call.lineno}")
        self.assertEqual(offenders, [], "subprocess.run without timeout= in:")


if __name__ == "__main__":
    unittest.main()
