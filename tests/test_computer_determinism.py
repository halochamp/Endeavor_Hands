from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import computer_use
from tools._screen_state import ScreenElement, ScreenSnapshot, build_snapshot


class ComputerDeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        computer_use.reset_computer_guards()
        self.temp = tempfile.TemporaryDirectory(prefix="endeavor-computer-", dir="/private/tmp")
        self.paths: list[Path] = []

    def tearDown(self) -> None:
        computer_use.reset_computer_guards()
        self.temp.cleanup()

    def _image(self, *, changed: bool = False) -> str:
        frame = np.zeros((360, 640), dtype=np.uint8)
        if changed:
            cv2.rectangle(frame, (180, 100), (460, 260), 255, thickness=-1)
        path = Path(self.temp.name) / f"shot_{len(self.paths)}.png"
        self.assertTrue(cv2.imwrite(str(path), frame))
        self.paths.append(path)
        return str(path)

    def _field(
        self,
        *,
        text: str = "Field",
        focused: bool = True,
        value: str = "",
        role: str = "AXTextField",
    ) -> ScreenElement:
        digest = hashlib.sha256(value.encode()).hexdigest()[:16] if value else ""
        return ScreenElement(
            "e1",
            text,
            role,
            "ax",
            (120.0, 100.0),
            (40.0, 80.0, 160.0, 40.0),
            "left",
            True,
            focused,
            (),
            digest,
        )

    def _snapshot(
        self,
        observation_id: str,
        *,
        image: str,
        app: str = "TestApp",
        elements: list[ScreenElement] | None = None,
        ax: str = "ok",
        ocr_boxes: list[dict] | None = None,
        window_bounds: tuple[float, float, float, float] | None = (0.0, 0.0, 640.0, 360.0),
    ) -> ScreenSnapshot:
        return ScreenSnapshot(
            observation_id,
            app,
            "Window",
            list(elements or []),
            list(ocr_boxes or []),
            image,
            (640, 360),
            (640.0, 360.0),
            ax,
            window_bounds,
        )

    def test_semantic_only_change_is_detected(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, elements=[self._field(text="Before")])
        after = self._snapshot("obs_2", image=image, elements=[self._field(text="After")])
        semantic, visual = computer_use._screen_change_signals(before, after)
        self.assertTrue(semantic)
        self.assertFalse(visual)

    def test_visual_only_change_is_detected(self) -> None:
        before = self._snapshot("obs_1", image=self._image(), elements=[self._field()])
        after = self._snapshot("obs_2", image=self._image(changed=True), elements=[self._field()])
        semantic, visual = computer_use._screen_change_signals(before, after)
        self.assertFalse(semantic)
        self.assertTrue(visual)
        self.assertNotEqual(before.visual_fingerprint, after.visual_fingerprint)

    def test_no_change_has_no_signal(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, elements=[self._field()])
        after = self._snapshot("obs_2", image=image, elements=[self._field()])
        self.assertEqual(computer_use._screen_change_signals(before, after), (False, False))

    def test_focus_verified_and_focus_unavailable_stay_distinct(self) -> None:
        image = self._image()
        focused = self._snapshot("obs_1", image=image, elements=[self._field(text="Search")], ax="ok")
        unavailable = self._snapshot("obs_2", image=image, elements=[self._field(text="Search")], ax="unavailable")
        self.assertTrue(computer_use._expected(focused, "focus:Search"))
        self.assertIsNone(computer_use._expected(unavailable, "focus:Search"))

    def test_text_expectation_matches_exact_text_split_across_ocr_boxes(self) -> None:
        snapshot = self._snapshot("obs_1", image=self._image(), elements=[])
        snapshot.ocr_boxes = [
            {"text": "PASS", "x": 0.10, "y": 0.80, "w": 0.05, "h": 0.02},
            {"text": ": real one-layer Endeavor Hands", "x": 0.16, "y": 0.80, "w": 0.30, "h": 0.02},
            {"text": "sandbox boundary is active", "x": 0.47, "y": 0.80, "w": 0.25, "h": 0.02},
        ]
        self.assertTrue(
            computer_use._expected(
                snapshot,
                "text:PASS: real one-layer Endeavor Hands sandbox boundary is active",
            )
        )
        self.assertFalse(computer_use._expected(snapshot, "text:PASS: different boundary"))

    def test_text_expectation_ignores_background_window_ocr(self) -> None:
        snapshot = self._snapshot(
            "obs_bg",
            image=self._image(),
            elements=[],
            window_bounds=(0.0, 0.0, 300.0, 300.0),
            ocr_boxes=[
                {"text": "TARGET_FROM_BACKGROUND", "x": 0.75, "y": 0.50, "w": 0.20, "h": 0.05},
            ],
        )
        self.assertFalse(computer_use._expected(snapshot, "text:TARGET_FROM_BACKGROUND"))

    def test_text_expectation_is_unknown_without_ax_match_or_window_bounds(self) -> None:
        snapshot = self._snapshot(
            "obs_unknown",
            image=self._image(),
            elements=[],
            ax="unavailable",
            window_bounds=None,
            ocr_boxes=[
                {"text": "MAYBE_BACKGROUND", "x": 0.20, "y": 0.50, "w": 0.20, "h": 0.05},
            ],
        )
        self.assertIsNone(computer_use._expected(snapshot, "text:MAYBE_BACKGROUND"))

    def test_text_expectation_can_use_ax_without_window_bounds(self) -> None:
        snapshot = self._snapshot(
            "obs_ax",
            image=self._image(),
            elements=[self._field(text="Foreground label")],
            window_bounds=None,
        )
        self.assertTrue(computer_use._expected(snapshot, "text:Foreground label"))

    def test_type_value_digest_verifies_without_exposing_value(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, elements=[self._field(value="before")])
        after = self._snapshot("obs_2", image=image, elements=[self._field(value="after")])
        self.assertEqual(computer_use._type_input_verification(before, after), "input_verified")
        self.assertNotIn("before", before.elements[0].value_digest)
        self.assertNotIn("after", after.elements[0].value_digest)

    def test_type_focus_unavailable_is_unverified(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, elements=[self._field(value="a")], ax="unavailable")
        after = self._snapshot("obs_2", image=image, elements=[self._field(value="b")], ax="unavailable")
        self.assertEqual(computer_use._type_input_verification(before, after), "input_unverified")

    def test_type_focus_only_is_explicitly_unverified(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, elements=[self._field(value="")])
        after = self._snapshot("obs_2", image=image, elements=[self._field(value="")])
        self.assertEqual(
            computer_use._type_input_verification(before, after),
            "input_unverified+focus_verified",
        )

    def test_secure_ax_value_is_neither_exposed_nor_digested(self) -> None:
        image = self._image()
        snapshot = build_snapshot(
            "obs_secure",
            app="TestApp",
            ocr_boxes=[],
            ax_state={
                "status": "ok",
                "window": "Window",
                "elements": [
                    {
                        "role": "AXSecureTextField",
                        "name": "Password",
                        "value": "actual-secret-value",
                        "bounds": [40, 80, 160, 40],
                        "focused": True,
                        "enabled": True,
                    }
                ],
            },
            image_path=image,
            capture_size=(640, 360),
            screen_size=(640.0, 360.0),
        )
        self.assertEqual(snapshot.elements[0].text, "Password")
        self.assertEqual(snapshot.elements[0].value_digest, "")
        self.assertNotIn("actual-secret-value", snapshot.fingerprint)

    def test_observation_budget_does_not_consume_mutation_budget(self) -> None:
        computer_use.set_computer_turn_scope(action_max=1, observation_max=2)
        image = self._image()
        see1 = self._snapshot("obs_1", image=image)
        see2 = self._snapshot("obs_2", image=image)
        before = self._snapshot("obs_3", image=image)
        after = self._snapshot("obs_4", image=image)

        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "key", return_value=None),
            mock.patch.object(computer_use._backend, "settle", return_value=None),
            mock.patch.object(computer_use, "_capture_snapshot", side_effect=[see1, see2, before]),
            mock.patch.object(computer_use, "_publish", side_effect=lambda s: s),
            mock.patch.object(computer_use, "_post_action_snapshot", return_value=(after, "no_visible_change")),
            mock.patch.object(computer_use, "_phase", return_value=None),
            mock.patch.object(computer_use, "_progress", return_value=None),
            mock.patch.object(computer_use, "_log_action", return_value=None),
        ):
            self.assertIn("[ok] see", computer_use._computer_impl("see"))
            self.assertIn("[ok] see", computer_use._computer_impl("see"))
            self.assertEqual(computer_use._ACTION_ATTEMPTS[0], 0)
            self.assertEqual(computer_use._OBSERVATION_ATTEMPTS[0], 2)
            self.assertIn("observation limit", computer_use._computer_impl("see"))
            self.assertNotIn("mutation limit", computer_use._computer_impl("key", text="enter"))
            self.assertEqual(computer_use._ACTION_ATTEMPTS[0], 1)
            self.assertIn("mutation limit", computer_use._computer_impl("key", text="enter"))

    def test_type_result_reports_changed_and_input_verified(self) -> None:
        before = self._snapshot("obs_1", image=self._image(), elements=[self._field(value="a")])
        after = self._snapshot("obs_2", image=self._image(changed=True), elements=[self._field(value="ab")])
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "type_text", return_value=None),
            mock.patch.object(computer_use._backend, "settle", return_value=None),
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_post_action_snapshot", return_value=(after, "changed")),
            mock.patch.object(computer_use, "_phase", return_value=None),
            mock.patch.object(computer_use, "_progress", return_value=None),
            mock.patch.object(computer_use, "_log_action", return_value=None),
        ):
            result = computer_use._computer_impl("type", text="b")
        self.assertIn("[ok] type effect=changed+input_verified", result)

    def test_type_surfaces_external_clipboard_race_as_warning(self) -> None:
        before = self._snapshot("obs_1", image=self._image(), elements=[self._field(value="a")])
        after = self._snapshot("obs_2", image=self._image(changed=True), elements=[self._field(value="ab")])
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(
                computer_use._backend,
                "type_text",
                return_value="clipboard_changed_externally",
            ),
            mock.patch.object(computer_use._backend, "settle", return_value=None),
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_post_action_snapshot", return_value=(after, "changed")),
            mock.patch.object(computer_use, "_phase", return_value=None),
            mock.patch.object(computer_use, "_progress", return_value=None),
            mock.patch.object(computer_use, "_log_action", return_value=None),
        ):
            result = computer_use._computer_impl("type", text="b")
        self.assertIn("[warning] type", result)
        self.assertIn("input_verified+clipboard_changed_externally", result)

    def test_type_focus_only_stays_warning_even_when_screen_changed(self) -> None:
        before = self._snapshot("obs_1", image=self._image(), elements=[self._field(value="")])
        after = self._snapshot("obs_2", image=self._image(changed=True), elements=[self._field(value="")])
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "type_text", return_value=None),
            mock.patch.object(computer_use._backend, "settle", return_value=None),
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_post_action_snapshot", return_value=(after, "changed")),
            mock.patch.object(computer_use, "_phase", return_value=None),
            mock.patch.object(computer_use, "_progress", return_value=None),
            mock.patch.object(computer_use, "_log_action", return_value=None),
        ):
            result = computer_use._computer_impl("type", text="b")
        self.assertIn(
            "[warning] type effect=changed+input_unverified+focus_verified",
            result,
        )

    def test_type_no_visible_change_and_unverified_is_warning(self) -> None:
        before = self._snapshot("obs_1", image=self._image(), elements=[], ax="unavailable")
        after = self._snapshot("obs_2", image=self._image(), elements=[], ax="unavailable")
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "type_text", return_value=None),
            mock.patch.object(computer_use._backend, "settle", return_value=None),
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_post_action_snapshot", return_value=(after, "no_visible_change")),
            mock.patch.object(computer_use, "_phase", return_value=None),
            mock.patch.object(computer_use, "_progress", return_value=None),
            mock.patch.object(computer_use, "_log_action", return_value=None),
        ):
            result = computer_use._computer_impl("type", text="b")
        self.assertIn("[warning] type effect=no_visible_change+input_unverified", result)

    def test_wrong_frontmost_app_is_refused_before_keypress(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image, app="Finder")
        computer_use._LAST_FRONTMOST_APP[0] = "TextEdit"
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "key") as key_mock,
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_publish", side_effect=lambda s: s),
            mock.patch.object(computer_use, "_phase", return_value=None),
        ):
            result = computer_use._computer_impl("key", text="enter")
        self.assertIn("frontmost app changed", result)
        key_mock.assert_not_called()

    def test_repeated_action_protection_remains_intact(self) -> None:
        image = self._image()
        latest = self._snapshot("obs_1", image=image)
        computer_use._LATEST_OBSERVATION[0] = latest
        computer_use._LAST_FRONTMOST_APP[0] = latest.app
        computer_use._LAST_SIGNATURE[0] = "|".join(("key", "", "enter", "", "3", "", "", "", "", ""))
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "key") as key_mock,
            mock.patch.object(computer_use, "_capture_snapshot", return_value=latest),
            mock.patch.object(computer_use, "_phase", return_value=None),
        ):
            result = computer_use._computer_impl("key", text="enter")
        self.assertIn("repeated action", result)
        key_mock.assert_not_called()

    def test_destructive_guard_still_terminates_mutations(self) -> None:
        image = self._image()
        before = self._snapshot("obs_1", image=image)
        computer_use._DESTRUCTIVE_GUARD[0] = True
        with (
            mock.patch.object(computer_use._backend, "failsafe_abort", return_value=False),
            mock.patch.object(computer_use._backend, "click") as click_mock,
            mock.patch.object(computer_use, "_capture_snapshot", return_value=before),
            mock.patch.object(computer_use, "_phase", return_value=None),
        ):
            result = computer_use._computer_impl("click", target="Delete")
        self.assertIn("file deletion is disabled", result)
        self.assertEqual(computer_use._ACTION_ATTEMPTS[0], computer_use._effective_mutation_max())
        click_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
