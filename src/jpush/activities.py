# SPDX-FileCopyrightText: 2026 XMU-LocalBot contributors
# SPDX-License-Identifier: MIT
"""Course activity hints plus school-verified local deadline state.

Push contents never supply authoritative deadlines. A consumer resolves the
hint against its own school's API, then updates DeadlineBook with that result.
No network, account credentials, file downloads or submissions occur here.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import json

@dataclass(frozen=True,slots=True)
class ActivityHint:
    course_id: int
    activity_id: int

def _positive(value):
    if isinstance(value,bool) or not isinstance(value,(str,int)):return None
    if isinstance(value,str) and (not value.isascii() or not value.isdigit() or len(value)>19):return None
    value=int(value)
    return value if 0<value<=2**63-1 else None

def decode_activity(content: str, *, max_bytes: int=4096) -> ActivityHint | None:
    if not isinstance(content,str) or not 0<len(content.encode('utf-8'))<=max_bytes:return None
    try:
        root=json.loads(content)
        if not isinstance(root,dict):return None
        objects=[root]
        for key in ('extras','n_extras','message'):
            value=root.get(key)
            if isinstance(value,str) and value.lstrip().startswith('{'):value=json.loads(value)
            if isinstance(value,dict):objects.append(value)
        def unique(names):
            values=[_positive(row[name]) for row in objects for name in names if name in row]
            return values[0] if values and None not in values and all(v==values[0] for v in values) else None
        course=unique(('course_id','courseId','acme-course-id'))
        activity=unique(('activity_id','activityId','acme-activity-id'))
        # Do not guess an upstream HOMEWORK_PUBLISHED/EXAM_UPDATE wire enum.
        # Only explicit activity identities cause an official data refresh.
        return ActivityHint(course,activity) if course and activity else None
    except (ValueError,TypeError,RecursionError):return None

@dataclass(frozen=True,slots=True)
class VerifiedActivity:
    course_id: int
    activity_id: int
    kind: str
    title: str
    starts_at: datetime | None=None
    due_at: datetime | None=None

    def __post_init__(self):
        if _positive(self.course_id) is None or _positive(self.activity_id) is None or self.kind not in ('homework','exam'):
            raise ValueError('invalid verified school activity')
        if not isinstance(self.title,str) or len(self.title)>512:raise ValueError('invalid title')
        for at in (self.starts_at,self.due_at):
            if at is not None and (not isinstance(at,datetime) or at.tzinfo is None or at.utcoffset() is None):
                raise ValueError('school timestamps must have timezone information')
        if self.starts_at and self.due_at and self.starts_at>self.due_at:
            raise ValueError('school start time exceeds deadline')

class DeadlineBook:
    """Bounded local timer state; one book per authorized school identity.

    ``changed`` detects content/deadline edits, ``due`` replaces API polling
    inside the reminder window. Mark only after delivery succeeds. Changed
    deadlines invalidate the old timer without being marked permanently sent.
    Keep a low-frequency reconciliation for missed/disabled push deliveries.
    """
    def __init__(self,limit: int=2000):
        if not 1<=limit<=10000:raise ValueError('invalid capacity')
        self.limit=limit;self._rows={};self._sent=set()

    def update(self,activity: VerifiedActivity) -> bool:
        if not isinstance(activity,VerifiedActivity):raise TypeError('school-verified activity required')
        key=(activity.course_id,activity.activity_id)
        before=self._rows.get(key)
        if before is None and len(self._rows)>=self.limit:raise ValueError('deadline book capacity reached')
        self._rows[key]=activity
        if before!=activity:self._sent={marker for marker in self._sent if marker[:2]!=key}
        return before!=activity

    def remove(self,course_id: int,activity_id: int):
        key=(course_id,activity_id);self._rows.pop(key,None)
        self._sent={marker for marker in self._sent if marker[:2]!=key}

    def due(self,now: datetime,*,deadline_window: timedelta=timedelta(hours=24),start_window: timedelta=timedelta(minutes=30)):
        if now.tzinfo is None or now.utcoffset() is None:raise ValueError('current time must include a timezone')
        if min(deadline_window,start_window)<timedelta(0):raise ValueError('invalid window')
        results=[]
        for key,row in self._rows.items():
            for stage,at,window in [('start',row.starts_at,start_window),('due',row.due_at,deadline_window)]:
                if at and now<at<=now+window and (*key,stage,at) not in self._sent:results.append((row,stage,at))
        return results

    def mark(self,activity: VerifiedActivity,stage: str,at: datetime):
        key=(activity.course_id,activity.activity_id)
        current=self._rows.get(key)
        if current!=activity or stage not in ('start','due') or not isinstance(at,datetime) or at!=(activity.starts_at if stage=='start' else activity.due_at):
            raise ValueError('stale reminder cannot mark a newer deadline')
        self._sent.add((*key,stage,at))

    def prune(self,now: datetime):
        if now.tzinfo is None or now.utcoffset() is None:raise ValueError('current time must include a timezone')
        for key,row in list(self._rows.items()):
            at=row.due_at or row.starts_at
            if at is not None and at<now:self.remove(*key)
