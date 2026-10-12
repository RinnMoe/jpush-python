# SPDX-FileCopyrightText: 2026 XMU-LocalBot contributors
# SPDX-License-Identifier: MIT
"""Conservative, receive-only decoding of TronClass rollcall hints.

IDs and kinds are hints, not authorization or proof that an activity is open.
Consumers must revalidate enrollment, type, state and attendance threshold with
their authorized school session. QR hints contain no valid QR token.
"""
from __future__ import annotations
from dataclasses import dataclass
import json

_KINDS = {'NUMBER_ROLLCALL': 'number', 'RADAR_ROLLCALL': 'radar', 'QRCODE_ROLLCALL': 'qr'}

@dataclass(frozen=True, slots=True)
class RollcallHint:
    kind: str
    course_id: int
    rollcall_id: int

def _id(value):
    if isinstance(value,bool) or not isinstance(value,(int,str)):
        return None
    if isinstance(value,str) and (not value or not value.isascii() or not value.isdigit() or len(value)>19):
        return None
    result=int(value)
    return result if 0<result<=2**63-1 else None

def decode_rollcall(content: str, *, max_bytes: int=4096) -> RollcallHint | None:
    """Decode explicit normalized/extra fields; reject conflicting identities.

    Accepts JSON objects in ``extras`` and Android ``n_extras``. No free-text
    regex, link fetching, school login, or attendance submission occurs here.
    Unknown event types are left for the caller's existing raw-push handling.
    """
    if not isinstance(content,str) or not 0<len(content.encode('utf-8'))<=max_bytes:
        return None
    try:
        root=json.loads(content)
        if not isinstance(root,dict):return None
        objects=[root]
        for name in ('extras','n_extras','message'):
            nested=root.get(name)
            if isinstance(nested,str) and nested.lstrip().startswith('{'):nested=json.loads(nested)
            if isinstance(nested,dict):objects.append(nested)
        def unique(names, convert):
            values=[convert(obj[key]) for obj in objects for key in names if key in obj]
            return values[0] if values and None not in values and all(v==values[0] for v in values) else None
        # A human notification title is not itself an event type. A recognized
        # literal title is accepted only when no explicit type was provided.
        explicit=[obj[key] for obj in objects for key in ('event_type','eventType') if key in obj]
        if explicit:
            if not all(isinstance(v,str) and v in _KINDS and v==explicit[0] for v in explicit):return None
            kind=_KINDS[explicit[0]]
        else:
            types=[obj.get('title') for obj in objects if isinstance(obj.get('title'),str) and obj.get('title') in _KINDS]
            if not types or any(t!=types[0] for t in types):return None
            kind=_KINDS[types[0]]
        course=unique(('course_id','courseId','acme-course-id'),_id)
        activity=unique(('rollcall_id','rollcallId','acme-rollcall-id'),_id)
        return RollcallHint(kind,course,activity) if course and activity else None
    except (ValueError,TypeError,RecursionError):return None
