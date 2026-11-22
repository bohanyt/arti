"""ADR 225: directory arrival is a wakeup hint, never a result or a proof.

Arm before reading the mailbox; rearm before returning to the next read. Windows
rename/create/write notifications need no mod changes or helper thread. A missing
facility falls back to the existing polling cadence.
"""
import math
import os
import time


class FileArrival:
    def __init__(self, directory):
        self.handle = None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
            api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
            api.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
            api.FindNextChangeNotification.restype = wintypes.BOOL
            api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.WaitForSingleObject.restype = wintypes.DWORD
            self.api = api
            handle = api.FindFirstChangeNotificationW(str(directory), False, 0x1 | 0x8 | 0x10)
            if handle not in (None, ctypes.c_void_p(-1).value):
                self.handle = handle

    def __enter__(self):
        return self

    def close(self):
        if self.handle is not None:
            self.api.FindCloseChangeNotification(self.handle)
            self.handle = None

    def __exit__(self, *exc):
        self.close()

    def wait(self, seconds):
        seconds = max(0., seconds)
        if self.handle is None:
            time.sleep(seconds)
            return
        outcome = self.api.WaitForSingleObject(self.handle, math.ceil(seconds * 1000))
        if outcome == 0:
            if not self.api.FindNextChangeNotification(self.handle):
                self.close()
        elif outcome != 258:  # WAIT_TIMEOUT; failure must not create a busy loop
            self.close()
            time.sleep(seconds)
