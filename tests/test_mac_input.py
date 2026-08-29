from __future__ import annotations

import unittest
from unittest import mock

from tools import _mac_input


class _FakePasteboard:
    def __init__(self) -> None:
        self.count = 10
        self.text = "original"

    def pasteboardItems(self):
        return []

    def clearContents(self):
        self.count += 1
        self.text = None
        return self.count

    def setString_forType_(self, text, _paste_type):
        self.count += 1
        self.text = text
        return True

    def changeCount(self):
        return self.count


class _FakePasteboardClass:
    def __init__(self, pasteboard: _FakePasteboard) -> None:
        self._pasteboard = pasteboard

    def generalPasteboard(self):
        return self._pasteboard


class _FakeQuartz:
    kCGHIDEventTap = 7
    kCGSessionEventTap = 8
    kCGEventSourceStateHIDSystemState = 9

    def __init__(self) -> None:
        self.sources: list[int] = []
        self.created: list[tuple[object, int, bool]] = []
        self.unicode_calls: list[tuple[dict, int, str]] = []
        self.posted: list[tuple[int, dict]] = []

    def CGEventSourceCreate(self, state: int):
        self.sources.append(state)
        return {"source_state": state}

    def CGEventCreateKeyboardEvent(self, source, keycode: int, is_down: bool):
        event = {"source": source, "keycode": keycode, "is_down": is_down}
        self.created.append((source, keycode, is_down))
        return event

    def CGEventKeyboardSetUnicodeString(self, event: dict, length: int, text: str) -> None:
        self.unicode_calls.append((event, length, text))

    def CGEventPost(self, tap: int, event: dict) -> None:
        self.posted.append((tap, event))


class MacInputTypeTextTests(unittest.TestCase):
    def test_type_text_routes_all_unicode_through_transactional_paste(self) -> None:
        with mock.patch.object(
            _mac_input,
            "_paste_text_transactionally",
            return_value="clipboard_restored",
        ) as paste:
            self.assertEqual(_mac_input.type_text("ไทย😀"), "clipboard_restored")

        paste.assert_called_once_with("ไทย😀")

    def test_type_text_empty_string_posts_nothing(self) -> None:
        quartz = _FakeQuartz()
        with mock.patch.object(_mac_input, "_quartz", quartz):
            self.assertEqual(_mac_input.type_text(""), "no_text")

        self.assertEqual(quartz.created, [])
        self.assertEqual(quartz.unicode_calls, [])
        self.assertEqual(quartz.posted, [])

    def test_terminal_routes_type_through_transactional_paste(self) -> None:
        with (
            mock.patch.object(_mac_input, "frontmost_app_name", return_value="Terminal"),
            mock.patch.object(
                _mac_input,
                "_paste_text_transactionally",
                return_value="clipboard_restored",
            ) as paste,
        ):
            result = _mac_input.type_text("ไทย😀")

        self.assertEqual(result, "clipboard_restored")
        paste.assert_called_once_with("ไทย😀")

    def test_transactional_paste_restores_clipboard_when_unchanged(self) -> None:
        pasteboard = _FakePasteboard()

        def restore(pb, snapshot):
            self.assertIs(pb, pasteboard)
            self.assertEqual(snapshot, [[("public.utf8-plain-text", b"old")]])
            pb.count += 1
            pb.text = "original"

        with (
            mock.patch.object(_mac_input, "_NSPasteboard", _FakePasteboardClass(pasteboard)),
            mock.patch.object(_mac_input, "_NSPasteboardTypeString", "public.utf8-plain-text"),
            mock.patch.object(
                _mac_input,
                "_snapshot_pasteboard",
                return_value=[[('public.utf8-plain-text', b'old')]],
            ),
            mock.patch.object(_mac_input, "_restore_pasteboard", side_effect=restore) as restore_mock,
            mock.patch.object(_mac_input, "key") as key_mock,
            mock.patch.object(_mac_input.time, "sleep"),
        ):
            result = _mac_input._paste_text_transactionally("typed")

        self.assertEqual(result, "clipboard_restored")
        key_mock.assert_called_once_with("cmd+v")
        restore_mock.assert_called_once()
        self.assertEqual(pasteboard.text, "original")

    def test_transactional_paste_does_not_overwrite_external_clipboard_change(self) -> None:
        pasteboard = _FakePasteboard()

        def external_change(_combo):
            pasteboard.count += 1
            pasteboard.text = "external"

        with (
            mock.patch.object(_mac_input, "_NSPasteboard", _FakePasteboardClass(pasteboard)),
            mock.patch.object(_mac_input, "_NSPasteboardTypeString", "public.utf8-plain-text"),
            mock.patch.object(_mac_input, "_snapshot_pasteboard", return_value=[]),
            mock.patch.object(_mac_input, "_restore_pasteboard") as restore_mock,
            mock.patch.object(_mac_input, "key", side_effect=external_change),
            mock.patch.object(_mac_input.time, "sleep"),
        ):
            result = _mac_input._paste_text_transactionally("typed")

        self.assertEqual(result, "clipboard_changed_externally")
        restore_mock.assert_not_called()
        self.assertEqual(pasteboard.text, "external")


if __name__ == "__main__":
    unittest.main()
