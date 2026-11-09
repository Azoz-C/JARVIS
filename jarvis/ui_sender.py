"""Native Windows Codex composer adapter. No app-server resume/turn calls.

Runs in a short-lived STA helper process so UI Automation and OLE clipboard state
never leak across asyncio worker threads. All input targets an observed control.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .security import GateError


class DeliveryError(GateError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class ClipboardSnapshot:
    """Clone all clipboard formats before input; never persist clipboard data."""
    def __init__(self):
        from ctypes import wintypes
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32")
        self.user.GetClipboardData.argtypes = [wintypes.UINT]
        self.user.GetClipboardData.restype = wintypes.HANDLE
        self.user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user.SetClipboardData.restype = wintypes.HANDLE
        self.ole.OleDuplicateData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.UINT]
        self.ole.OleDuplicateData.restype = wintypes.HANDLE
        self.ole.ReleaseStgMedium.restype = None
        self.handles = []
        try:
            fmt = self.user.EnumClipboardFormats(0)
            while fmt:
                original = self.user.GetClipboardData(fmt)
                copy = self.ole.OleDuplicateData(original, fmt, 2) if original else None
                if not copy:
                    raise DeliveryError("Clipboard format cannot be safely preserved; no input was sent")
                self.handles.append((fmt, copy))
                fmt = self.user.EnumClipboardFormats(fmt)
        except BaseException:
            self.release(); raise

    def restore(self):
        import win32clipboard
        win32clipboard.EmptyClipboard()
        remaining = []
        for fmt, handle in self.handles:
            if not self.user.SetClipboardData(fmt, handle): remaining.append((fmt, handle))
        self.handles = remaining
        if remaining: raise DeliveryError("Clipboard restoration was incomplete")

    def release(self):
        from ctypes import wintypes
        class Medium(ctypes.Structure):
            _fields_ = [("tymed", wintypes.DWORD), ("data", wintypes.HANDLE), ("release", ctypes.c_void_p)]
        for fmt, handle in self.handles:
            kind = 16 if fmt in {2, 9, 130} else 32 if fmt in {3, 131} else 64 if fmt in {14, 142} else 1
            self.ole.ReleaseStgMedium(ctypes.byref(Medium(kind, handle, None)))
        self.handles = []


def process_path(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return None
    try:
        buffer = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)): return None
        return Path(buffer.value)
    finally: kernel.CloseHandle(handle)


def trusted_executable(path):
    return bool(path and path.name.casefold() in {"chatgpt.exe", "codex.exe"}
                and any(part.startswith("OpenAI.Codex_") for part in path.parts))


class NativeComposer:
    def __init__(self, cancel_path=None):
        import pythoncom
        from pywinauto import Desktop
        import win32gui
        self.ole = pythoncom
        self.desktop, self.win32 = Desktop(backend="uia"), win32gui
        candidates = []
        for window in self.desktop.windows(visible_only=True):
            try:
                if window.window_text() not in {"ChatGPT", "Codex"}: continue
                executable = process_path(window.process_id())
                if trusted_executable(executable): candidates.append((window, executable))
            except Exception: continue
        focused = [item for item in candidates if item[0].handle == win32gui.GetForegroundWindow()]
        if len(focused) == 1: candidates = focused
        if len(candidates) != 1:
            raise DeliveryError("Open one Codex/ChatGPT desktop window before sending a prompt")
        self.window, self.executable = candidates[0]
        self.clicked = False
        self.cancel_path = Path(cancel_path) if cancel_path else None
        self.window.set_focus()
        import win32api
        # An invisible clipboard owner is necessary for reliable SetClipboardData.
        self.clipboard_window = win32gui.CreateWindowEx(0, "STATIC", "JARVIS clipboard", 0, 0, 0, 0, 0,
            -3, 0, win32api.GetModuleHandle(None), None)

    def close(self):
        self.win32.DestroyWindow(self.clipboard_window)

    def open_clipboard(self):
        import win32clipboard
        deadline = time.monotonic() + 5
        while True:
            try:
                win32clipboard.OpenClipboard(self.clipboard_window)
                return
            except Exception:
                if time.monotonic() >= deadline: raise DeliveryError("Clipboard is busy; no further input was sent")
                self.ole.PumpWaitingMessages()
                time.sleep(.05)

    def foreground(self):
        if self.cancel_path and self.cancel_path.exists():
            raise DeliveryError("Frontend delivery was cancelled; inspect the chat before retrying", self.clicked)
        if self.win32.GetForegroundWindow() != self.window.handle:
            raise DeliveryError("The Codex window lost focus; no further input was sent")

    def document(self):
        self.window = self.desktop.window(handle=self.window.handle).wrapper_object()
        documents = [d for d in self.window.descendants(control_type="Document") if d.element_info.automation_id == "RootWebArea"]
        if len(documents) != 1:
            raise DeliveryError("Could not identify exactly one Codex conversation document")
        return documents[0]

    def rendered_message(self, payload):
        document = self.document()
        try:
            content = document.iface_text.DocumentRange.GetText(-1)
        except Exception:
            content = "\n".join(e.window_text() for e in document.descendants(control_type="Text"))
        transcript = content.split("Latest response", 1)[0]
        # Compare transcript occurrences before/after Send, excluding the draft.
        # Whitespace normalization accommodates accessible text paragraph breaks.
        return re.sub(r"\s+", " ", transcript).count(re.sub(r"\s+", " ", payload))

    @contextmanager
    def clipboard(self):
        import win32clipboard as clipboard
        self.open_clipboard()
        try: original = ClipboardSnapshot()
        finally: clipboard.CloseClipboard()
        owned = {"sequence": None}
        try:
            yield owned
        finally:
            # Never replace a newer clipboard entry made concurrently by the user.
            try:
                if owned["sequence"] is not None and clipboard.GetClipboardSequenceNumber() == owned["sequence"]:
                    self.open_clipboard()
                    try: original.restore()
                    finally: clipboard.CloseClipboard()
            finally: original.release()

    def current_thread(self):
        import win32clipboard as clipboard
        self.foreground()
        with self.clipboard() as owned:
            # Force a distinct clipboard value so application-level duplicate
            # write suppression cannot masquerade as a failed Copy chat link.
            self.open_clipboard()
            try:
                clipboard.EmptyClipboard(); clipboard.SetClipboardText("JARVIS verifying current chat", 13)
            finally: clipboard.CloseClipboard()
            owned["sequence"] = clipboard.GetClipboardSequenceNumber()
            before = clipboard.GetClipboardSequenceNumber()
            try: self.document().set_focus()
            except Exception: pass
            self.window.type_keys("^%l", set_foreground=False)
            deadline = time.monotonic() + 6
            repeat = time.monotonic() + 1
            while time.monotonic() < deadline:
                sequence = clipboard.GetClipboardSequenceNumber()
                if sequence != before:
                    self.open_clipboard()
                    try:
                        value = clipboard.GetClipboardData(13) if clipboard.IsClipboardFormatAvailable(13) else ""
                    finally: clipboard.CloseClipboard()
                    owned["sequence"] = sequence
                    if isinstance(value, str) and value.startswith("codex://threads/"):
                        return value.removeprefix("codex://threads/").split("?", 1)[0].strip()
                    raise DeliveryError("Copy chat link did not identify a local Codex chat")
                if time.monotonic() >= repeat:
                    self.foreground()
                    self.window.type_keys("^%l", set_foreground=False)
                    repeat = time.monotonic() + 1
                self.ole.PumpWaitingMessages()
                time.sleep(.05)
        raise DeliveryError("Codex did not expose its current chat link; no prompt was entered")

    def navigate(self, thread, title):
        try:
            if self.current_thread() == thread: return
        except DeliveryError:
            self.foreground()
        # Avoid stale protocol associations entirely. Use observed controls and
        # verify the UUID; a duplicate title can never authorize a send.
        candidates = self.window.descendants(control_type="Button", title=title)
        for candidate in candidates:
            self.foreground()
            candidate.invoke()
            time.sleep(.4)
            try:
                if self.current_thread() == thread: return
            except DeliveryError:
                self.foreground()
        self.search_chat(thread, title)

    def search_chat(self, thread, title):
        # Command search includes chats omitted from collapsed/limited sidebars.
        self.foreground()
        self.window.type_keys("^k", set_foreground=False)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            fields = self.window.descendants(control_type="ComboBox", title="Command menu")
            if fields: break
            time.sleep(.1)
        if len(fields) != 1: raise DeliveryError("Codex chat search is unavailable; no prompt was entered")
        fields[0].iface_value.SetValue(title)
        time.sleep(.8)
        # Search suggestions may also contain files/actions. Only the Chats group
        # can authorize navigation, and each opened result must match the UUID.
        groups = self.window.descendants(control_type="Group", title="Chats")
        names = [e.element_info.name for g in groups for e in g.descendants(control_type="ListItem")]
        for name in dict.fromkeys(names):
            self.foreground()
            matches = self.window.descendants(control_type="ListItem", title=name)
            if not matches: continue
            matches[0].invoke()
            time.sleep(.4)
            if self.current_thread() == thread: return
            self.window.type_keys("^k", set_foreground=False)
            time.sleep(.2)
            fields = self.window.descendants(control_type="ComboBox", title="Command menu")
            if len(fields) != 1: break
            fields[0].iface_value.SetValue(title)
            time.sleep(.5)
        raise DeliveryError("Chat search did not open the requested chat ID; no prompt was entered")

    def composer(self):
        editors = [e for e in self.document().descendants(control_type="Edit")
                   if e.element_info.name in {"Work with Codex", "Message Codex", "Message ChatGPT"} and e.is_visible()]
        if len(editors) != 1: raise DeliveryError("Could not identify one visible Codex composer")
        return editors[0]

    @staticmethod
    def value(editor):
        value = editor.get_value()
        normalized = str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()
        return "" if normalized in {"", "Work with Codex", "Message Codex", "Message ChatGPT"} else str(value)

    def paste(self, editor, payload):
        import win32clipboard as clipboard
        self.foreground()
        with self.clipboard() as owned:
            self.open_clipboard()
            try:
                clipboard.EmptyClipboard(); clipboard.SetClipboardText(payload, 13)
            finally: clipboard.CloseClipboard()
            owned["sequence"] = clipboard.GetClipboardSequenceNumber()
            editor.set_focus()
            self.window.type_keys("^v", set_foreground=False)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.value(self.composer()).replace("\r\n", "\n") == payload.replace("\r\n", "\n"): return
                time.sleep(.1)
            raise DeliveryError("Composer text could not be verified; Send was not pressed")

    def send(self, thread, title, payload, request_id):
        self.navigate(thread, title)
        before = self.rendered_message(payload)
        editor = self.composer()
        draft = self.value(editor).replace("\r\n", "\n")
        if draft.strip() and draft != payload.replace("\r\n", "\n"):
            raise DeliveryError("The target chat already has an unsent draft; JARVIS will not overwrite it")
        if not draft.strip(): self.paste(editor, payload)
        if self.current_thread() != thread:
            raise DeliveryError("Target chat changed after staging; Send was not pressed")
        buttons = [b for b in self.document().descendants(control_type="Button")
                   if b.element_info.name in {"Send", "Send message", "Send prompt", "Queue message"} and b.is_visible() and b.is_enabled()]
        if len(buttons) != 1:
            raise DeliveryError("A unique enabled Send control was not found; the prompt remains a draft")
        self.foreground()
        # Never press Stop, change permissions, resume a writer or retry the click.
        self.clicked = True
        try: buttons[0].invoke()
        except Exception as error: raise DeliveryError("Send invocation has an uncertain outcome; inspect the chat before retrying", True) from error
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if self.current_thread() != thread:
                    raise DeliveryError("The view changed after Send; inspect the target chat", True)
                if self.rendered_message(payload) > before and not self.value(self.composer()).strip():
                    from .ui_transport import prompt_digest
                    return {"sent": True, "transport": "interface", "thread": thread, "ui_echo_observed": True,
                            "request_id": request_id, "prompt_sha256": prompt_digest(payload),
                            "message": "Prompt observed in the existing chat's interface; execution is checked separately"}
            except DeliveryError as error:
                if error.uncertain: raise
            except Exception: pass
            time.sleep(.3)
        raise DeliveryError("Send was pressed, but the conversation echo was not verified; do not resend automatically", True)


def main():
    if os.name != "nt": raise GateError("Frontend composer delivery requires Windows")
    sys.coinit_flags = 2
    import pythoncom
    pythoncom.CoInitialize()
    pythoncom.OleInitialize()
    try:
        request = json.loads(sys.stdin.buffer.read(300000))
        from .frontend import chat_link
        chat_link(request["thread"])
        payload, request_id = request["payload"], request.get("request_id", "diagnostic")
        if not isinstance(payload, str) or not payload.strip() or len(payload) > 60000 or not request_id:
            raise DeliveryError("Invalid frontend prompt request")
        from .platform import instance_lock
        lock_root = Path(request["state"]) / "frontend-ui"; lock_root.mkdir(parents=True, exist_ok=True)
        with instance_lock(lock_root):
            composer = NativeComposer(request.get("cancel_file"))
            try:
                if request.get("navigate_only"):
                    composer.navigate(request["thread"], request["title"])
                    result = {"thread": composer.current_thread(), "navigated": True, "sent": False}
                elif request.get("inspect"):
                    result = {"thread": composer.current_thread(), "document": composer.document().element_info.name,
                              "prompt_occurrences": composer.rendered_message(payload),
                              "edits": [{"name": e.element_info.name, "visible": e.is_visible(), "value_length": len(composer.value(e))} for e in composer.document().descendants(control_type="Edit")]}
                else:
                    result = composer.send(request["thread"], request["title"], payload, request_id)
            finally: composer.close()
        print(json.dumps(result), flush=True)
    except Exception as error:
        if os.environ.get("JARVIS_UI_DEBUG") == "1":
            import traceback
            traceback.print_exc(file=sys.stderr)
        print(json.dumps({"sent": False, "uncertain": bool(getattr(error, "uncertain", False)) or bool(locals().get("composer") and composer.clicked),
                          "error_type": type(error).__name__, "message": str(error)[:500] if isinstance(error, GateError) else "Native frontend operation failed"}), flush=True)
    finally:
        ctypes.WinDLL("ole32").OleUninitialize()
        pythoncom.CoUninitialize()


if __name__ == "__main__": main()
