# SPDX-FileCopyrightText: 2026 XMU-LocalBot contributors
# SPDX-License-Identifier: MIT
"""Receive-only consumer hook; authentication/submission stays in the caller."""
from jpush.rollcall import decode_rollcall

def on_push(push, wake_course):
    hint=decode_rollcall(push.content)
    if hint is not None:
        # wake_course must re-read the authorized school's current activity,
        # enrollment and threshold, respect existing switches, and coalesce
        # duplicate activities. Never sign merely because decoding succeeded.
        wake_course(hint)
